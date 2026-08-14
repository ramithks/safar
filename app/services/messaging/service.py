"""
The messaging service. Channel-agnostic, and the only thing that decides
whether a message may be sent.

Four responsibilities, and they are here rather than spread out for a reason:

**Property identity.** `resolve_connection()` turns a provider account id into
exactly one property, through `resolve_whatsapp_property()`. No caller anywhere
accepts a property from a request body.

**Idempotency, inbound.** `record_inbound()` is keyed on `(channel,
external_id)`. Meta redelivers; the second delivery returns the row it already
wrote, flagged `duplicate`, and the pipeline stops there. Without it one guest
message becomes two agent runs, two tickets and two replies.

**Idempotency, outbound.** `send()` writes a `queued` row with a
`client_reference` *before* the provider call, then updates it. A retry after a
timeout finds that row and does not send again.

**The window.** WhatsApp permits free-form replies only for 24 hours after the
guest's last message. `send()` refuses outside it with `MESSAGE_WINDOW_CLOSED`,
which the agent receives as a structured code it can act on. The model is never
asked whether the window is open, because a model asked that question would
sometimes say yes.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db.client import get_db, run_db
from app.services.messaging import credentials as creds_module
from app.services.messaging import templates as templates_module
from app.services.messaging.channels import (
    NO_CONNECTION,
    PROVIDER_FAILED,
    WINDOW_CLOSED,
    DeliveryUpdate,
    InboundMessage,
    MessagingError,
)

logger = logging.getLogger(__name__)


@dataclass
class RecordedInbound:
    conversation: dict
    message: dict
    duplicate: bool


@dataclass
class SentMessage:
    message: dict
    external_id: str | None
    template_name: str | None
    duplicate: bool = False


# ── connections ──────────────────────────────────────────────────────────────

def _resolve_connection(account_id: str) -> dict | None:
    rows = get_db().rpc(
        "resolve_whatsapp_property", {"p_phone_number_id": account_id}
    ).execute().data or []
    if not rows:
        return None
    row = dict(rows[0])
    # The RPC returns the connection's own id under a different name than the
    # table does. Normalised here so callers see one shape.
    row["id"] = row.pop("connection_id")
    row["phone_number_id"] = account_id
    return row


async def resolve_connection(account_id: str) -> dict:
    """
    Which property owns this provider account. The only trusted answer.

    Raises when the number is unknown, which is the correct response to a
    webhook for a number nobody has connected: we cannot say whose guest that
    is, and guessing would put a stranger's message into a hotel's inbox.
    """
    row = await run_db(_resolve_connection, account_id)
    if not row:
        raise MessagingError(
            NO_CONNECTION, f"no property is connected to WhatsApp number {account_id}"
        )
    return row


def _connection_for_property(property_id: str) -> dict | None:
    rows = (
        get_db()
        .table("whatsapp_connections")
        .select("*")
        .eq("property_id", property_id)
        .in_("status", ["connected", "pending"])
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


# ── conversations ────────────────────────────────────────────────────────────

def _upsert_conversation(
    *, property_id: str, partner_id: str, channel: str,
    external_id: str, sender_external_id: str, sender_name: str | None,
    account_id: str,
) -> dict:
    db = get_db()

    existing = (
        db.table("conversations")
        .select("*")
        .eq("property_id", property_id)
        .eq("channel", channel)
        .eq("external_id", external_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        return existing[0]

    # A returning guest is matched on the phone number the property already has
    # for them. Scoped to the property — the same number at two hotels is two
    # guest records, and joining them would be a cross-property leak dressed up
    # as a convenience.
    guest_id = None
    if sender_external_id:
        digits = sender_external_id.lstrip("+")
        found = (
            db.table("guests")
            .select("id,name")
            .eq("property_id", property_id)
            .ilike("phone", f"%{digits[-10:]}")
            .limit(1)
            .execute()
            .data
            or []
        )
        if found:
            guest_id = found[0]["id"]
            sender_name = sender_name or found[0].get("name")

    row = {
        "property_id": property_id,
        "partner_id": partner_id,
        "channel": channel,
        "external_id": external_id,
        "channel_account_id": account_id,
        "guest_phone": sender_external_id if channel == "whatsapp" else None,
        "guest_handle": sender_external_id if channel == "instagram" else None,
        "guest_name": sender_name,
        "guest_id": guest_id,
        "handler": "ai",
        "status": "open",
    }
    return db.table("conversations").insert(row).execute().data[0]


def _link_reservation(conversation: dict) -> dict:
    """
    Attach the guest's current stay, if they have one.

    This is what makes "my AC isn't working" land on the right door and what
    scopes `get_reservation` in the agent context. Only ever the guest's *own*
    stay: the lookup starts from `conversation.guest_id`, so there is no path
    from a phone number to somebody else's booking.
    """
    if conversation.get("reservation_id") or not conversation.get("guest_id"):
        return conversation

    db = get_db()
    from app.services.pms import clock

    today = clock.business_date_iso(conversation["property_id"])
    rows = (
        db.table("reservations")
        .select("id,status,check_in,check_out")
        .eq("property_id", conversation["property_id"])
        .eq("guest_id", conversation["guest_id"])
        .in_("status", ["confirmed", "checked_in"])
        .gte("check_out", today)
        .order("check_in")
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        return conversation

    updated = (
        db.table("conversations")
        .update({"reservation_id": rows[0]["id"]})
        .eq("id", conversation["id"])
        .execute()
        .data
    )
    return updated[0] if updated else conversation


# ── inbound ──────────────────────────────────────────────────────────────────

def _record_inbound(inbound: InboundMessage, connection: dict) -> RecordedInbound:
    db = get_db()

    # Idempotency first, before anything is created. A redelivered webhook must
    # not even create a conversation, because creating one would move
    # `last_inbound_at` and reopen a window that had legitimately closed.
    seen = (
        db.table("conversation_messages")
        .select("*")
        .eq("channel", inbound.channel)
        .eq("external_id", inbound.external_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if seen:
        conversation = (
            db.table("conversations").select("*")
            .eq("id", seen[0]["conversation_id"]).limit(1).execute().data[0]
        )
        return RecordedInbound(conversation=conversation, message=seen[0], duplicate=True)

    conversation = _upsert_conversation(
        property_id=connection["property_id"],
        partner_id=connection["partner_id"],
        channel=inbound.channel,
        external_id=inbound.sender_external_id,
        sender_external_id=inbound.sender_external_id,
        sender_name=inbound.sender_name,
        account_id=connection.get("phone_number_id") or "",
    )
    conversation = _link_reservation(conversation)

    sent_at = (inbound.sent_at or datetime.now(timezone.utc)).isoformat()
    message = db.table("conversation_messages").insert({
        "conversation_id": conversation["id"],
        "property_id": conversation["property_id"],
        "partner_id": conversation["partner_id"],
        "channel": inbound.channel,
        "direction": "inbound",
        "author": "guest",
        "author_name": inbound.sender_name or conversation.get("guest_name"),
        "sender_external_id": inbound.sender_external_id,
        "message_type": inbound.message_type,
        "body": inbound.body,
        "media_urls": list(inbound.media_urls),
        "external_id": inbound.external_id,
        # Inbound messages have no delivery lifecycle of their own: it is here,
        # which is as delivered as it gets.
        "delivery_status": "delivered",
        "metadata": inbound.raw,
        "sent_at": sent_at,
    }).execute().data[0]

    # The window opens here. Everything about whether we may reply freely for
    # the next 24 hours is this column.
    db.table("conversations").update({
        "last_inbound_at": sent_at,
        "last_message_at": sent_at,
        "last_message": (inbound.body or f"[{inbound.message_type}]")[:200],
        "unread_count": (conversation.get("unread_count") or 0) + 1,
    }).eq("id", conversation["id"]).execute()

    db.table("whatsapp_connections").update(
        {"last_inbound_at": sent_at}
    ).eq("id", connection["id"]).execute()

    conversation = (
        db.table("conversations").select("*")
        .eq("id", conversation["id"]).limit(1).execute().data[0]
    )
    return RecordedInbound(conversation=conversation, message=message, duplicate=False)


async def record_inbound(inbound: InboundMessage, connection: dict) -> RecordedInbound:
    return await run_db(_record_inbound, inbound, connection)


# ── the window ───────────────────────────────────────────────────────────────

def window_open(conversation: dict, *, now: datetime | None = None) -> bool:
    """
    Whether free-form outbound is permitted.

    Meta's rule, not ours: 24 hours from the guest's last inbound message. A
    conversation the guest has never written to has no window at all — which is
    correct, and is why a hotel cannot use this to message people who did not
    contact them.
    """
    last = conversation.get("last_inbound_at")
    if not last:
        return False
    if isinstance(last, str):
        last = datetime.fromisoformat(last.replace("Z", "+00:00"))
    hours = settings.whatsapp_service_window_hours
    return (now or datetime.now(timezone.utc)) - last < timedelta(hours=hours)


def window_closes_at(conversation: dict) -> datetime | None:
    last = conversation.get("last_inbound_at")
    if not last:
        return None
    if isinstance(last, str):
        last = datetime.fromisoformat(last.replace("Z", "+00:00"))
    return last + timedelta(hours=settings.whatsapp_service_window_hours)


# ── outbound ─────────────────────────────────────────────────────────────────

def _queue_outbound(conversation: dict, spec: dict) -> tuple[dict, bool]:
    """
    `spec` is a dict rather than keyword arguments because `run_db` forwards
    positionally only. Worth the small awkwardness: the alternative is a
    partial() at every call site or a second wrapper nobody would keep in sync.
    """
    body = spec["body"]
    template = spec["template"]
    client_reference = spec["client_reference"]
    db = get_db()

    # The retry path. A `client_reference` we have already written means this
    # send has been attempted; whether it reached Meta is what the row says.
    seen = (
        db.table("conversation_messages")
        .select("*")
        .eq("property_id", conversation["property_id"])
        .eq("client_reference", client_reference)
        .limit(1)
        .execute()
        .data
        or []
    )
    if seen:
        return seen[0], True

    row = db.table("conversation_messages").insert({
        "conversation_id": conversation["id"],
        "property_id": conversation["property_id"],
        "partner_id": conversation["partner_id"],
        "channel": conversation["channel"],
        "direction": "outbound",
        "author": spec["author"],
        "author_name": spec["author_name"],
        "author_member": spec["author_member"],
        "message_type": "text",
        "body": body,
        "template_name": template["provider_name"] if template else None,
        "client_reference": client_reference,
        "agent_action_id": spec["agent_action_id"],
        # Written before the provider call, so a crash between the two leaves a
        # message that is visibly unsent rather than no evidence at all.
        "delivery_status": "queued",
    }).execute().data[0]
    return row, False


def _settle_outbound(message_id: str, patch: dict) -> dict:
    return (
        get_db().table("conversation_messages")
        .update(patch).eq("id", message_id).execute().data[0]
    )


def _touch_outbound(conversation_id: str, preview: str, at: str) -> None:
    get_db().table("conversations").update({
        "last_outbound_at": at,
        "last_message_at": at,
        "last_message": preview[:200],
    }).eq("id", conversation_id).execute()


async def send(
    conversation: dict,
    *,
    body: str | None = None,
    purpose: str | None = None,
    template_values: dict | None = None,
    language: str = "en",
    author: str = "agent",
    author_name: str | None = None,
    author_member: str | None = None,
    agent_action_id: str | None = None,
    client_reference: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> SentMessage:
    """
    Send free text, or an approved template when the window has closed.

    `body` and `purpose` are alternatives. Give `body` for a normal reply;
    give `purpose` to send a template deliberately. Giving `body` outside the
    window raises `MESSAGE_WINDOW_CLOSED` rather than quietly falling back to a
    template — the caller has to decide, because a template costs money and says
    something different from what the caller wrote.
    """
    connection = await run_db(_connection_for_property, conversation["property_id"])
    if not connection:
        raise MessagingError(
            NO_CONNECTION,
            f"property {conversation['property_id']} has no connected "
            f"{conversation['channel']} number",
        )

    template = None
    if purpose:
        template = await templates_module.resolve(
            conversation["property_id"], purpose=purpose, language=language,
            channel=conversation["channel"],
            allow_test=bool(connection.get("is_sandbox")),
        )
    elif not window_open(conversation):
        closes = window_closes_at(conversation)
        raise MessagingError(
            WINDOW_CLOSED,
            "the 24-hour service window closed at "
            + (closes.isoformat() if closes else "an unknown time")
            + "; free-form messages are not permitted until the guest writes again",
        )

    if body is None and template is None:
        raise MessagingError(PROVIDER_FAILED, "nothing to send")

    reference = client_reference or f"msg:{uuid.uuid4()}"
    message, replayed = await run_db(_queue_outbound, conversation, {
        "body": body, "template": template, "author": author,
        "author_name": author_name, "author_member": author_member,
        "agent_action_id": agent_action_id, "client_reference": reference,
    })

    # Already sent. A retry after a provider timeout lands here, which is the
    # whole reason the reference is chosen before the request leaves.
    if replayed and message.get("delivery_status") in ("sent", "delivered", "read"):
        return SentMessage(
            message=message, external_id=message.get("external_id"),
            template_name=message.get("template_name"), duplicate=True,
        )

    adapter = creds_module.adapter_for(connection)
    try:
        if template is not None:
            result = await adapter.send_template(
                to=conversation.get("guest_phone") or conversation.get("guest_handle") or "",
                template_name=template["provider_name"],
                language=template["language"],
                parameters=templates_module.bind(template, template_values or {}),
                client_reference=reference,
            )
        else:
            result = await adapter.send_text(
                to=conversation.get("guest_phone") or conversation.get("guest_handle") or "",
                body=body or "",
                client_reference=reference,
            )
    except MessagingError as exc:
        # The intent survives the failure. A failed row is what makes a retry
        # possible and what stops the inbox claiming a guest was answered.
        await run_db(_settle_outbound, message["id"], {
            "delivery_status": "failed",
            "failure_reason": exc.detail[:400],
            "error_code": exc.code,
        })
        raise

    now = datetime.now(timezone.utc).isoformat()
    message = await run_db(_settle_outbound, message["id"], {
        "delivery_status": "sent",
        "external_id": result.external_id,
        "sent_at": now,
        "metadata": result.raw,
    })
    await run_db(_touch_outbound, conversation["id"], body or (template or {}).get("body_preview") or "", now)

    # Metered here rather than in the agent's tool, because this is the one
    # choke point every outbound message passes through — the same argument as
    # the window rule. Metering in the tool left a receptionist's reply and the
    # pipeline's own fallback send unbilled, which is the kind of gap that is
    # only ever found by reconciling against a provider invoice.
    #
    # Idempotent on the message id, so the retry path above — which returns
    # without reaching here — cannot double-count either.
    try:
        from app.services.ai import usage as usage_mod

        await usage_mod.record_message(
            message=message,
            template_name=template["provider_name"] if template else None,
            agent_id=agent_id,
            run_id=run_id,
            action_id=agent_action_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not meter outbound message %s", message["id"], exc_info=True)

    return SentMessage(
        message=message, external_id=result.external_id,
        template_name=template["provider_name"] if template else None,
    )


# ── delivery callbacks ───────────────────────────────────────────────────────

def _apply_status(update: DeliveryUpdate) -> dict | None:
    db = get_db()
    rows = (
        db.table("conversation_messages")
        .select("id,delivery_status,metadata")
        .eq("channel", update.channel)
        .eq("external_id", update.external_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not rows:
        # A status for a message we never sent. Not an error worth failing the
        # webhook over — Meta will keep redelivering it — but worth a line.
        logger.info("delivery status for unknown message %s", update.external_id)
        return None

    row = rows[0]
    history = list((row.get("metadata") or {}).get("statuses") or [])
    history.append({"status": update.status, "at": (update.at or datetime.now(timezone.utc)).isoformat()})

    metadata = {**(row.get("metadata") or {}), "statuses": history[-10:]}

    # Meta's billing dimensions, kept beside the message they describe. A rate
    # card will read these; nothing here turns them into money, because nobody
    # has agreed a price yet and a plausible-looking number now would be a
    # decision made by accident.
    billing = {
        k: v for k, v in {
            "conversationId": update.conversation_id_external,
            "category": update.conversation_category,
            "pricingModel": update.pricing_model,
            "billable": update.billable,
        }.items() if v is not None
    }
    if billing:
        metadata["providerBilling"] = {**(metadata.get("providerBilling") or {}), **billing}

    patch = {
        "delivery_status": update.status,
        "metadata": metadata,
    }
    if update.status == "failed":
        patch["failure_reason"] = (update.error_detail or "")[:400] or None
        patch["error_code"] = update.error_code

    # The database refuses a backwards move — see guard_delivery_status in
    # migration 11 — so a late `delivered` after a `failed` updates the history
    # and leaves the status alone.
    return db.table("conversation_messages").update(patch).eq("id", row["id"]).execute().data[0]


async def apply_delivery_status(update: DeliveryUpdate) -> dict | None:
    return await run_db(_apply_status, update)


# ── human takeover ───────────────────────────────────────────────────────────

def _set_handler(conversation_id: str, property_id: str, patch: dict) -> dict | None:
    rows = (
        get_db().table("conversations").update(patch)
        .eq("id", conversation_id).eq("property_id", property_id).execute().data
    )
    return rows[0] if rows else None


async def request_human(conversation_id: str, property_id: str, reason: str | None = None) -> dict | None:
    """
    The agent stepping back. Nobody has picked it up yet, and the inbox has to
    be able to show that gap — it is real and it lasts minutes.
    """
    patch = {"handler": "human_requested"}
    if reason in ("complaint", "refund", "policy_conflict"):
        patch["intent"] = "complaint" if reason == "complaint" else "support"
    return await run_db(_set_handler, conversation_id, property_id, patch)


async def take_over(conversation_id: str, property_id: str, member) -> dict | None:
    """A person picking it up. Stops the agent until somebody explicitly resumes."""
    return await run_db(_set_handler, conversation_id, property_id, {
        "handler": "human",
        "assignee_member": member.member_id,
        "taken_over_by": member.profile_id,
        "taken_over_at": datetime.now(timezone.utc).isoformat(),
        "unread_count": 0,
    })


async def resume_ai(conversation_id: str, property_id: str) -> dict | None:
    """
    Handing it back. Deliberately explicit: a conversation does not drift back
    to the AI because a human stopped typing.
    """
    return await run_db(_set_handler, conversation_id, property_id, {
        "handler": "ai",
        "ai_enabled": True,
        "taken_over_by": None,
        "taken_over_at": None,
    })


def agent_may_answer(conversation: dict) -> bool:
    """
    **Human takeover stops autonomous replies**, as a column rather than a
    convention. Checked before a run is opened, so a taken-over conversation
    costs nothing and leaves no trace.
    """
    return (
        conversation.get("ai_enabled", True) is not False
        and (conversation.get("handler") or "ai") == "ai"
    )
