"""
Sending, over the Meta Cloud API.

One POST per message. Over `httpx` rather than a Meta SDK for the same reason
the model provider is: the surface used here is two request shapes, and an SDK
would bring a second retry policy and a set of exception types that would end up
leaking into the messaging service's control flow.

**This module does not decide whether a message may be sent.** The 24-hour
window, template approval and idempotency are all `service.py`'s job. By the
time a method here is called, that has been settled — which is why an adapter
can be swapped for a fake without any of those rules going missing.
"""
import logging

import httpx

from app.config import settings
from app.services.messaging.channels import (
    PROVIDER_FAILED,
    PROVIDER_UNAVAILABLE,
    MessagingError,
    SendResult,
)

logger = logging.getLogger(__name__)


class WhatsAppAdapter:
    channel = "whatsapp"
    is_sandbox = False

    def __init__(self, *, phone_number_id: str, access_token: str, base_url: str | None = None):
        self.phone_number_id = phone_number_id
        self.access_token = access_token
        self.base_url = (base_url or settings.whatsapp_api_base).rstrip("/")

    # ── the wire ─────────────────────────────────────────────────────────────

    async def _post(self, payload: dict) -> SendResult:
        url = f"{self.base_url}/{self.phone_number_id}/messages"
        try:
            async with httpx.AsyncClient(timeout=settings.whatsapp_timeout_seconds) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            # Retryable, and the distinction matters: the caller has already
            # written a `queued` row with a client_reference, so a retry finds
            # that row rather than sending a second message.
            raise MessagingError(
                PROVIDER_UNAVAILABLE, f"could not reach WhatsApp: {exc}", retryable=True
            ) from exc

        if resp.status_code >= 400:
            detail, code = "", None
            try:
                error = (resp.json().get("error") or {})
                detail = str(error.get("message", ""))[:200]
                code = error.get("code")
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            # 5xx and 429 are worth retrying; a 400 means the message itself is
            # wrong and retrying it produces the same 400 forever.
            retryable = resp.status_code >= 500 or resp.status_code == 429
            raise MessagingError(
                PROVIDER_FAILED,
                f"WhatsApp returned {resp.status_code} ({code}): {detail}",
                retryable=retryable,
            )

        data = resp.json()
        sent = (data.get("messages") or [{}])[0]
        return SendResult(external_id=sent.get("id"), status="sent", raw=data)

    # ── the two things every channel must do ─────────────────────────────────

    async def send_text(self, *, to: str, body: str, client_reference: str) -> SendResult:
        return await self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            # Link previews off. A guest asking about breakfast does not need
            # the hotel's own website unfurled underneath the answer.
            "text": {"preview_url": False, "body": body},
        })

    async def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language: str,
        parameters: list[str],
        client_reference: str,
    ) -> SendResult:
        components = []
        if parameters:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in parameters],
            })

        return await self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
                **({"components": components} if components else {}),
            },
        })
