"""
Meta's side of the wire: the handshake, the signature, and the payload.

Nothing here touches the database. It turns bytes from an untrusted request into
either a refusal or a list of `InboundMessage` / `DeliveryUpdate` — which is
exactly the boundary that lets the endpoint stay four lines long and the parsing
be tested without a webhook.

**Signature verification is not optional and not clever.** Meta signs the raw
request body with the app secret as `X-Hub-Signature-256`. It has to be checked
against the bytes as received, before any parsing, and compared with
`hmac.compare_digest` — a `==` on a hex digest leaks the position of the first
wrong byte to anyone willing to send a few thousand requests.
"""
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from app.services.messaging.channels import DeliveryUpdate, InboundMessage
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHANNEL = "whatsapp"

# Meta's status vocabulary, mapped to ours. `accepted` appears on some template
# sends and means the same thing as `sent`.
_STATUS = {
    "accepted": "sent",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "deleted": "failed",
}


def verify_challenge(
    *, mode: str | None, token: str | None, challenge: str | None, expected_token: str | None
) -> str:
    """
    The GET handshake.

    Meta calls the webhook once with a token we chose and a challenge string it
    chose, and expects the challenge echoed back verbatim. Anything else and the
    subscription is not created.

    Raises `ValueError` rather than returning None on failure, so an endpoint
    cannot accidentally return a 200 with an empty body — which Meta reads as
    success and which would leave a subscription pointing at an endpoint that
    never verified anything.
    """
    if not expected_token:
        raise ValueError("no verify token is configured")
    if mode != "subscribe":
        raise ValueError(f"unexpected hub.mode {mode!r}")
    if not token or not hmac.compare_digest(token, expected_token):
        raise ValueError("verify token does not match")
    if not challenge:
        raise ValueError("no challenge to echo")
    return challenge


def verify_signature(*, body: bytes, header: str | None, app_secret: str | None) -> bool:
    """
    Meta's HMAC over the raw body.

    Deliberately returns False rather than raising for a missing secret. A
    deployment with no `WHATSAPP_APP_SECRET` must reject every webhook, not
    accept them: an unverified webhook endpoint is an open door to a system that
    creates tickets and sends messages as a hotel.
    """
    if not app_secret:
        logger.error("whatsapp webhook received but no app secret is configured")
        return False
    if not header or not header.startswith("sha256="):
        return False

    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def sign(body: bytes, app_secret: str) -> str:
    """The same computation, for the fake provider and the tests to sign with."""
    return "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()


def _ts(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def parse(payload: dict) -> tuple[list[InboundMessage], list[DeliveryUpdate]]:
    """
    Turn one webhook body into messages and status updates.

    Meta nests four levels deep and batches unrelated events together, so this
    is a loop rather than a lookup. Anything unrecognised is skipped with a log
    line rather than raising: a single unfamiliar event type must not cost us
    the other five in the same request, which Meta would then redeliver forever.
    """
    messages: list[InboundMessage] = []
    statuses: list[DeliveryUpdate] = []

    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            account_id = metadata.get("phone_number_id") or ""

            # Names arrive in a separate array from the messages themselves.
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }

            for raw in value.get("messages") or []:
                parsed = _message(raw, account_id, names)
                if parsed:
                    messages.append(parsed)

            for raw in value.get("statuses") or []:
                external_id = raw.get("id")
                if not external_id:
                    continue
                errors = raw.get("errors") or []
                # Meta's own billing dimensions. They appear on the status
                # callback and nowhere else — not on the send response — so a
                # parser that drops them leaves the invoice as the only record
                # of what we were charged for.
                conversation = raw.get("conversation") or {}
                pricing = raw.get("pricing") or {}
                statuses.append(DeliveryUpdate(
                    channel=CHANNEL,
                    external_id=external_id,
                    status=_STATUS.get(raw.get("status") or "", "sent"),
                    error_code=str(errors[0].get("code")) if errors else None,
                    error_detail=(errors[0].get("title") or errors[0].get("message"))
                                 if errors else None,
                    at=_ts(raw.get("timestamp")),
                    conversation_id_external=conversation.get("id"),
                    conversation_category=(
                        (conversation.get("origin") or {}).get("type")
                        or pricing.get("category")
                    ),
                    pricing_model=pricing.get("pricing_model"),
                    billable=pricing.get("billable"),
                    recipient_country=raw.get("recipient_id", "")[:2] or None,
                    raw=raw,
                ))

    return messages, statuses


def _message(raw: dict, account_id: str, names: dict) -> InboundMessage | None:
    external_id = raw.get("id")
    sender = raw.get("from")
    if not external_id or not sender:
        logger.warning("whatsapp message with no id or sender, skipping")
        return None

    kind = raw.get("type") or "unsupported"
    body: str | None = None
    media: tuple[str, ...] = ()

    if kind == "text":
        body = (raw.get("text") or {}).get("body")
    elif kind in ("image", "video", "audio", "document", "sticker"):
        blob = raw.get(kind) or {}
        # The caption, when there is one, is the guest talking — "the AC in this
        # photo isn't working" — and is worth as much as a text message. The
        # media id is kept as a reference; downloading it is a later slice.
        body = blob.get("caption")
        if blob.get("id"):
            media = (f"wa:{blob['id']}",)
    elif kind == "location":
        loc = raw.get("location") or {}
        body = loc.get("name") or loc.get("address")
    elif kind == "button":
        body = (raw.get("button") or {}).get("text")
    elif kind == "interactive":
        block = raw.get("interactive") or {}
        inner = block.get("button_reply") or block.get("list_reply") or {}
        body = inner.get("title")
        kind = "interactive"
    elif kind == "reaction":
        body = (raw.get("reaction") or {}).get("emoji")

    return InboundMessage(
        channel=CHANNEL,
        external_id=external_id,
        sender_external_id=sender,
        account_id=account_id,
        message_type=kind if kind in (
            "text", "image", "video", "audio", "document", "location",
            "contacts", "sticker", "interactive", "button", "reaction",
        ) else "unsupported",
        body=body,
        media_urls=media,
        sender_name=names.get(sender),
        sent_at=_ts(raw.get("timestamp")),
        # Only what is useful for debugging. The body text is already stored in
        # its own column and does not need a second copy in a jsonb blob that
        # ends up in every log line.
        raw={k: v for k, v in raw.items() if k in ("type", "timestamp", "context")},
    )


# ── everything that is not a message ─────────────────────────────────────────
#
# `parse` above handles the two fields a single-tenant integration ever sees:
# `messages` and `statuses`. A **Tech Provider** sees more, because it holds
# accounts it does not own — a hotel can remove us, Meta can restrict a hotel,
# and a template we submitted gets approved or rejected hours later. All of it
# arrives on the same signed webhook, in the same `entry[].changes[]` shape,
# distinguished by `change.field`.
#
# Parsed separately rather than folded into `parse` because the consequences are
# different in kind: a message becomes a conversation and an agent run, while
# these become a status on a row. Returning them from one function would mean
# every caller had to know which of three lists it was looking at.


@dataclass(frozen=True)
class AccountEvent:
    """One thing Meta says happened to an account we hold."""

    # 'account_update' | 'message_template_status_update' | 'phone_number_quality_update'
    field: str
    # PARTNER_ADDED, APPROVED, and so on. Meta's vocabulary, not ours.
    event: str
    waba_id: str | None = None
    owner_business_id: str | None = None
    phone_number_id: str | None = None
    display_number: str | None = None
    # message_template_status_update only.
    template_id: str | None = None
    template_name: str | None = None
    template_language: str | None = None
    reason: str | None = None
    raw: dict = field(default_factory=dict)


def parse_account_events(payload: dict) -> list[AccountEvent]:
    """
    Turn one webhook body into the account-level things it reports.

    Unknown fields are skipped rather than raising, for the same reason `parse`
    skips unknown message types: Meta batches unrelated events into one request
    and adds new ones without notice, and a single unfamiliar event must not
    cost us the other five — which Meta would then redeliver forever.
    """
    events: list[AccountEvent] = []

    for entry in payload.get("entry") or []:
        # For account-level events the WABA id is the entry id, not something
        # inside `value`. It is the only place it appears on several of them.
        entry_waba = str(entry.get("id") or "") or None

        for change in entry.get("changes") or []:
            name = change.get("field") or ""
            value = change.get("value") or {}

            if name == "account_update":
                info = value.get("waba_info") or {}
                events.append(AccountEvent(
                    field=name,
                    event=str(value.get("event") or ""),
                    waba_id=str(info.get("waba_id") or entry_waba or "") or None,
                    owner_business_id=str(info.get("owner_business_id") or "") or None,
                    phone_number_id=str(value.get("phone_number_id") or "") or None,
                    display_number=value.get("display_phone_number"),
                    # Restrictions come as a list and violations as a dict; both
                    # are worth keeping verbatim, because what a hotel needs to
                    # be told is Meta's own wording.
                    reason=_account_reason(value),
                    raw=value,
                ))

            elif name == "message_template_status_update":
                events.append(AccountEvent(
                    field=name,
                    event=str(value.get("event") or ""),
                    waba_id=entry_waba,
                    template_id=str(value.get("message_template_id") or "") or None,
                    template_name=value.get("message_template_name"),
                    template_language=value.get("message_template_language"),
                    reason=value.get("reason") or value.get("disable_info"),
                    raw=value,
                ))

            elif name == "phone_number_quality_update":
                events.append(AccountEvent(
                    field=name,
                    event=str(value.get("event") or ""),
                    waba_id=entry_waba,
                    display_number=value.get("display_phone_number"),
                    reason=value.get("current_limit"),
                    raw=value,
                ))

            elif name == "phone_number_name_update":
                events.append(AccountEvent(
                    field=name,
                    event=str(value.get("decision") or ""),
                    waba_id=entry_waba,
                    display_number=value.get("display_phone_number"),
                    reason=value.get("requested_verified_name"),
                    raw=value,
                ))

            else:
                # `messages` and `statuses` land here and are handled by
                # `parse`. Anything else is genuinely new.
                if name not in ("messages", "statuses"):
                    logger.info("unhandled whatsapp webhook field %r", name)

    return events


def _account_reason(value: dict) -> str | None:
    """Meta's own words for why an account changed, wherever it put them."""
    violation = value.get("violation_info") or {}
    if violation.get("violation_type"):
        return str(violation["violation_type"])

    restrictions = value.get("restriction_info") or []
    if restrictions:
        return ", ".join(
            str(r.get("restriction_type")) for r in restrictions if r.get("restriction_type")
        ) or None

    ban = value.get("ban_info") or {}
    if ban.get("waba_ban_state"):
        return str(ban["waba_ban_state"])
    return None
