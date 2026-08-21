"""
Instagram, on the wire.

Meta's Instagram Messaging API is Messenger's, not WhatsApp's, and the
differences are not cosmetic:

    WhatsApp                          Instagram
    ────────────────────────────      ──────────────────────────────────
    POST /{phone_number_id}/messages  POST /{ig_account_id}/messages
    to: a phone number                to: {"id": IGSID}
    approved templates reopen a       nothing reopens a closed window
      closed window
    per-conversation billing,         no conversation billing at all
      category on the status callback
    delivery: sent/delivered/read     delivery: an echo, and read receipts

So this is a separate adapter rather than a subclass with overrides. The two
share the `ChannelAdapter` protocol and nothing else — which is the point of
that protocol being two methods wide.

`send_template` exists only to satisfy the protocol and refuses. It is not an
oversight and must not be "fixed" by falling back to free text: a caller asking
for a template on Instagram has made a mistake about the channel, and answering
it with a different message than the one requested hides that mistake behind a
successful-looking send.
"""
import logging

import httpx

from app.config import settings
from app.services.messaging.channels import (
    NOT_CONFIGURED,
    PROVIDER_FAILED,
    PROVIDER_UNAVAILABLE,
    TEMPLATE_NOT_FOUND,
    WINDOW_CLOSED,
    MessagingError,
    SendResult,
)

logger = logging.getLogger(__name__)

# Instagram's own error codes, mapped to the vocabulary the agent branches on.
# The prose is the remedy, not a restatement of the code — the person reading
# this in a log at 2am needs to know what to do, and the agent needs the code.
_IG_ERRORS: dict[int, tuple[str, str]] = {
    # The 24-hour standard messaging window has closed and no tag applies.
    10: (WINDOW_CLOSED,
         " — outside the standard messaging window. Instagram has no template "
         "mechanism: nothing can reopen this until the guest writes again. If a "
         "person has taken the conversation over, the human-agent allowance gives "
         "seven days, which policy.py applies from conversations.handler."),
    # Same family, raised as a subcode on some accounts.
    2534022: (WINDOW_CLOSED,
              " — message sent outside the allowed window."),
    # The app is not authorised on this Instagram account.
    200: (NOT_CONFIGURED,
          " — the token is valid but lacks instagram_manage_messages on this "
          "account, or the professional account is not linked to the Page. Meta → "
          "App → Instagram → API setup; a debug_token showing no target_ids for "
          "the account is the tell."),
    190: (NOT_CONFIGURED, " — the access token has expired or been invalidated."),
    # The recipient cannot be messaged: blocked, deleted, or never messaged us.
    551: (PROVIDER_FAILED,
          " — this person cannot currently receive messages from the account."),
    100: (PROVIDER_FAILED,
          " — Instagram rejected the recipient or the payload shape. An IGSID is "
          "scoped to one app: an id from another app's webhook will fail here."),
    4:  (PROVIDER_UNAVAILABLE, " — rate limited by Meta; retry with backoff."),
    613: (PROVIDER_UNAVAILABLE, " — calls to this endpoint are being throttled."),
}


def _classify(code: int | None, message: str) -> tuple[str, str]:
    mapped, hint = _IG_ERRORS.get(code or -1, (PROVIDER_FAILED, ""))
    return mapped, f"{message}{hint}"


class InstagramAdapter:
    """Talks to Meta's Instagram Messaging API. Holds a token; reads no database."""

    channel = "instagram"
    is_sandbox = False

    def __init__(self, *, instagram_account_id: str, access_token: str,
                 api_version: str | None = None):
        self.instagram_account_id = instagram_account_id
        self._token = access_token
        self._version = api_version or settings.instagram_api_version

    @property
    def _host(self) -> str:
        """
        Which Meta host will accept this token.

        **There are two Instagram messaging APIs and they are not
        interchangeable.** `oauth.py` says so in its own docstring and this file
        did not listen:

            Instagram API with *Facebook* Login   EAAB… token → graph.facebook.com
            Instagram API with *Instagram* Login  IGAA… token → graph.instagram.com

        We use Instagram Business Login — the consent screen at
        `www.instagram.com/oauth/authorize` — so every token we hold is an
        `IGAA…` one. Posting it to graph.facebook.com produced, in production,
        on the first real guest message a real hotel ever received:

            400  Invalid OAuth access token - Cannot parse access token

        Facebook was right: it is not a Facebook token. The guest saw nothing,
        the reply sat in the hotel's inbox marked failed, and the connection
        looked perfectly healthy on every screen.

        Decided from the token itself rather than from configuration, because
        the token is the thing that actually determines the answer — and a
        deployment that later adds Facebook Login must not need a second
        setting to keep working.
        """
        if (self._token or "").startswith("IG"):
            return "https://graph.instagram.com"
        return "https://graph.facebook.com"

    @property
    def _url(self) -> str:
        return (f"{self._host}/{self._version}"
                f"/{self.instagram_account_id}/messages")

    async def _post(self, payload: dict, *, client_reference: str) -> SendResult:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(
                    self._url,
                    params={"access_token": self._token},
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            # Retryable, and the caller already holds a queued row keyed on
            # client_reference — so a retry cannot become a second message.
            raise MessagingError(
                PROVIDER_UNAVAILABLE, f"Instagram timed out: {exc}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise MessagingError(
                PROVIDER_UNAVAILABLE, f"Instagram unreachable: {exc}", retryable=True
            ) from exc

        if r.status_code >= 400:
            try:
                err = (r.json().get("error") or {})
            except Exception:  # noqa: BLE001
                err = {}
            code, mapped = err.get("code"), None
            mapped, detail = _classify(
                code, str(err.get("message") or r.text)[:300]
            )
            logger.warning("instagram send failed (%s ref=%s): %s",
                           r.status_code, client_reference, detail)
            raise MessagingError(
                mapped, detail, retryable=(mapped == PROVIDER_UNAVAILABLE)
            )

        body = r.json()
        # Instagram returns message_id; Messenger returns mid. Accept either
        # rather than assuming, because the shape differs by account type and a
        # missing external id breaks delivery correlation silently.
        external = body.get("message_id") or body.get("mid")
        return SendResult(external_id=external, status="sent", raw=body)

    async def send_text(self, *, to: str, body: str, client_reference: str) -> SendResult:
        """
        `to` is an IGSID — an Instagram-scoped user id, meaningful only to this
        app. It is never a username: handles change, and a handle is chosen by
        the person we are messaging.
        """
        return await self._post(
            {"recipient": {"id": to}, "message": {"text": body}},
            client_reference=client_reference,
        )

    async def send_template(self, *, to: str, template_name: str, language: str,
                            parameters: list[str], client_reference: str) -> SendResult:
        raise MessagingError(
            TEMPLATE_NOT_FOUND,
            "Instagram has no approved-template mechanism. A closed Instagram "
            "window cannot be reopened by us; the guest has to write again.",
        )
