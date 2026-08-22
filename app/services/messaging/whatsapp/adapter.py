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
    NOT_CONFIGURED,
    PROVIDER_FAILED,
    PROVIDER_UNAVAILABLE,
    TEMPLATE_NOT_APPROVED,
    WINDOW_CLOSED,
    MessagingError,
    SendResult,
)

logger = logging.getLogger(__name__)


# Meta's error codes, translated into ours plus a sentence saying what to do.
#
# Written after a real send returned `131030` and the log said nothing except
# the number. A code alone sends whoever is on call to a search engine; the
# advice is the difference between a five-minute fix and an afternoon. Only
# codes we have actually seen or that have an unambiguous operator action are
# listed — inventing advice for a code nobody has hit would be worse than the
# raw number.
_META_ERRORS: dict[int, tuple[str, str]] = {
    131005: (NOT_CONFIGURED,
             " — the token is valid but is not authorised to act on the WABA "
             "that owns this number. Mint a fresh one from Meta → WhatsApp → "
             "API Setup (issued already bound to the WABA), or assign the WABA "
             "to a System User and use its token. `debug_token` showing "
             "granular_scopes with no target_ids is the tell."),
    131030: (NOT_CONFIGURED,
             " — this number is not on the test app's allowed list. Add it in "
             "Meta → WhatsApp → API Setup → 'To', or move off a test number."),
    131047: (WINDOW_CLOSED,
             " — more than 24 hours since the guest wrote. Send an approved "
             "template instead."),
    131026: (PROVIDER_FAILED,
             " — the recipient cannot receive WhatsApp messages on that number."),
    131051: (PROVIDER_FAILED, " — this message type is not supported here."),
    132000: (TEMPLATE_NOT_APPROVED,
             " — the template's parameter count does not match what Meta "
             "approved. Check `message_templates.parameters`."),
    132001: (TEMPLATE_NOT_APPROVED,
             " — no template with that name and language exists on this WABA."),
    132005: (TEMPLATE_NOT_APPROVED, " — the approved template text has changed."),
    133010: (NOT_CONFIGURED, " — the phone number is not registered."),
    190:    (NOT_CONFIGURED,
             " — the access token has expired or been revoked. Replace "
             "WHATSAPP_ACCESS_TOKEN in .env."),
    4:      (PROVIDER_UNAVAILABLE, " — Meta is rate limiting this app."),
    80007:  (PROVIDER_UNAVAILABLE, " — this WABA has hit its rate limit."),
}


def _classify(code: int | None, detail: str) -> tuple[str, str]:
    """Meta's code → (our structured code, an operator-facing sentence)."""
    if code is not None:
        try:
            mapped = _META_ERRORS.get(int(code))
        except (TypeError, ValueError):
            mapped = None
        if mapped:
            return mapped
    return PROVIDER_FAILED, ""


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
            detail, code, subcode = "", None, None
            try:
                error = (resp.json().get("error") or {})
                detail = str(error.get("message", ""))[:200]
                code = error.get("code")
                subcode = (error.get("error_data") or {}).get("details")
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]

            # 5xx and 429 are worth retrying; a 400 means the message itself is
            # wrong and retrying it produces the same 400 forever.
            retryable = resp.status_code >= 500 or resp.status_code == 429
            structured, advice = _classify(code, detail)
            raise MessagingError(
                structured,
                f"WhatsApp returned {resp.status_code} ({code}): "
                f"{subcode or detail}{advice}",
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

    async def send_image(self, *, to: str, image_url: str, caption: str | None,
                         client_reference: str) -> SendResult:
        """
        A photograph by link. Meta fetches it, so the bucket must serve it
        publicly and nothing here uploads bytes.

        Unlike Instagram, WhatsApp carries a caption on the image itself, which
        is one message rather than two and reads better in a thread.
        """
        image: dict = {"link": image_url}
        if caption:
            # Meta's limit is 1024; anything longer is a paragraph that belongs
            # in its own message rather than under a picture.
            image["caption"] = caption[:1024]
        return await self._post({
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": image,
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
