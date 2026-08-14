"""
Instagram's side of the wire.

Same three jobs as the WhatsApp parser — handshake, signature, payload — and a
completely different payload, which is why this is a separate module rather than
a branch in that one.

**The envelope is Messenger's, not WhatsApp's:**

    WhatsApp   entry[].changes[].value.messages[]      contacts[] alongside
    Instagram  entry[].messaging[]                     sender.id / recipient.id

That difference matters more than it looks. On WhatsApp, `value.metadata
.phone_number_id` names our end and the sender is a separate array. On
Instagram, *every* event carries both ends inline, and which one is "us" depends
on the direction — `recipient.id` for an inbound message, `sender.id` for an
echo of our own. Reading the wrong one maps a guest's IGSID to a property and
resolves nothing, or worse, resolves something.

**Echoes are the delivery signal.** Instagram does not send WhatsApp's
sent/delivered/read status objects for messages we originate. It sends an echo
of our own message back with `message.is_echo`, plus separate `read` events.
So `parse` produces a `DeliveryUpdate` from an echo — correlated on the mid we
already stored — and from a read receipt, and nothing else. There is no
`conversation` block and no pricing category, because Meta does not bill
Instagram messaging per conversation; the billing fields on `DeliveryUpdate`
stay `None` here rather than being filled with plausible-looking values.

Nothing in this module touches the database.
"""
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from app.services.messaging.channels import DeliveryUpdate, InboundMessage

logger = logging.getLogger(__name__)

CHANNEL = "instagram"


def verify_challenge(
    *, mode: str | None, token: str | None, challenge: str | None, expected_token: str | None
) -> str:
    """
    The GET handshake. Identical in shape to WhatsApp's and deliberately not
    shared: the two subscriptions have different verify tokens, and a helper
    taking both would invite passing the wrong one.
    """
    if not expected_token:
        raise ValueError("no Instagram verify token is configured")
    if mode != "subscribe":
        raise ValueError(f"unexpected hub.mode {mode!r}")
    if not token or not hmac.compare_digest(token, expected_token):
        raise ValueError("verify token does not match")
    if not challenge:
        raise ValueError("no challenge to echo")
    return challenge


def verify_signature(*, body: bytes, header: str | None, app_secret: str | None) -> bool:
    """
    Meta's HMAC over the raw body, compared with `compare_digest`.

    Returns False rather than raising when no secret is configured, for the same
    reason the WhatsApp one does: a deployment with no app secret must reject
    every webhook rather than accept them all. An unverified endpoint into a
    system that creates tickets and messages guests as a hotel is an open door.
    """
    if not app_secret:
        logger.error("instagram webhook received but no app secret is configured")
        return False
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.split("=", 1)[1], expected)


def _at(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _attachments(message: dict) -> tuple[str, tuple[str, ...]]:
    """
    What kind of thing arrived, and where it lives.

    Instagram sends a great deal we cannot act on: stories, reels, shares,
    reactions. Each is still persisted and still answered — the guest is told we
    cannot open it — because silently dropping an inbound event is the one
    behaviour a hotel can never explain to someone asking why nobody replied.
    """
    atts = message.get("attachments") or []
    if not atts:
        return "text", ()
    kinds = {a.get("type") for a in atts if a.get("type")}
    urls = tuple(
        u for u in ((a.get("payload") or {}).get("url") for a in atts) if u
    )
    # `story_mention` and `share` arrive as attachment types and are the two a
    # hotel actually sees often — someone tagging the hotel in a story, or
    # forwarding a post and asking "do you have this room?".
    kind = next(iter(kinds)) if len(kinds) == 1 else "mixed"
    return str(kind), urls


def parse(payload: dict) -> tuple[list[InboundMessage], list[DeliveryUpdate]]:
    """
    One webhook body → guest messages and delivery updates.

    Anything unrecognised is skipped with a log line rather than raising: a
    single unfamiliar event type must not cost us the other five in the same
    request, which Meta would then redeliver forever.
    """
    messages: list[InboundMessage] = []
    statuses: list[DeliveryUpdate] = []

    for entry in payload.get("entry") or []:
        # Meta puts Instagram events under `messaging`; some app configurations
        # send `standby` for handover-protocol conversations we do not own.
        # Reading standby as if it were ours would have the agent answer a
        # conversation another app is driving.
        for event in (entry.get("messaging") or []):
            sender = (event.get("sender") or {}).get("id")
            recipient = (event.get("recipient") or {}).get("id")
            message = event.get("message") or {}

            if message.get("is_echo"):
                # Our own message, reflected. This is Instagram's "sent"
                # confirmation and the only place the provider tells us the mid
                # it assigned. No billing fields: Meta does not bill Instagram
                # per conversation, and inventing a category here would put a
                # fabricated cost dimension into the ledger.
                mid = message.get("mid")
                if mid:
                    statuses.append(DeliveryUpdate(
                        channel=CHANNEL, external_id=mid, status="sent",
                        at=_at(event.get("timestamp")), raw=event,
                    ))
                continue

            if event.get("read"):
                # A read receipt names a watermark, not a message id. Correlated
                # by the service against messages sent before it.
                statuses.append(DeliveryUpdate(
                    channel=CHANNEL,
                    external_id=str((event.get("read") or {}).get("mid") or ""),
                    status="read", at=_at(event.get("timestamp")), raw=event,
                ))
                continue

            if event.get("delivery"):
                for mid in ((event.get("delivery") or {}).get("mids") or []):
                    statuses.append(DeliveryUpdate(
                        channel=CHANNEL, external_id=mid, status="delivered",
                        at=_at(event.get("timestamp")), raw=event,
                    ))
                continue

            if not message:
                logger.debug("instagram event with no message: %s", list(event))
                continue

            mid = message.get("mid")
            if not mid or not sender:
                logger.info("instagram message with no mid or sender, skipped")
                continue

            kind, urls = _attachments(message)
            messages.append(InboundMessage(
                channel=CHANNEL,
                external_id=mid,
                sender_external_id=sender,
                # **Our** end of the conversation, and the only field that may
                # resolve a property. On an inbound message that is `recipient`.
                account_id=str(recipient or entry.get("id") or ""),
                message_type=kind,
                body=message.get("text"),
                media_urls=urls,
                sent_at=_at(event.get("timestamp")),
                raw={"mid": mid, "sender": sender, "recipient": recipient},
            ))

        if entry.get("standby"):
            # Logged, never processed. Another app holds the thread.
            logger.info("instagram standby event ignored (%d)", len(entry["standby"]))

    return messages, statuses
