"""
The staff console — one front desk turn, from a typed question to an answer.

    staff message  ──►  internal conversation  ──►  run_turn  ──►  reply
                              │                        │
                              │                        └─ agent_actions → PMS
                              └─ handler, takeover, audit, meter

**A staff thread is a conversation, deliberately.** It would have been less code
to make this a stateless request/response endpoint — the receptionist types, the
model answers, nothing persists but the run. Every property of the messaging
model would then have had to be rebuilt for staff: a handler column so a manager
can stop the agent mid-thread, a message history so the second question knows
about the first, an inbox row so a shift handover can see what was asked, a
conversation id for the audit trail to hang off. All four already exist on
`conversations`, and the only thing standing in the way was a check constraint
that did not list `internal`. Migration 15 lists it.

**There is no provider on this channel and nothing is sent.** `messaging.send`
resolves a connection, respects a 24-hour window and calls Meta; none of those
concepts exists here, and reusing that path would have meant a fake connection
row and a window that is always open — a provider abstraction pretending to
carry something that never leaves the process. So the two message rows are
written directly, marked delivered, and the Front Desk agent is not granted
`send_message` at all. What it says, it returns.

**Human takeover works exactly as it does on WhatsApp**, because it is the same
column. A manager who takes the thread over stops the agent for every subsequent
question; resuming is explicit. That is `messaging.agent_may_answer`, checked
here before a run is opened, so a taken-over thread costs nothing.
"""
import logging
import uuid
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services.ai import context as ai_context
from app.services.ai import routing, runtime
from app.services.ai.schemas import RunResult
from app.services.messaging import service as messaging

logger = logging.getLogger(__name__)

CHANNEL = "internal"

# What the router is asked for on a front desk turn when nobody says.
#
# A capability, not a model — see services/ai/model_router. The caller may name
# a harder task (`operational_analysis`, `multi_reservation`) and get the
# reasoning tier; nothing here infers difficulty from how much somebody typed,
# because a receptionist writing three lines about a group arrival wants the
# same lookup as one writing four words.
#
# `task=None` hands the decision to `services/ai/classify` instead, which is the
# API's default: a receptionist should not have to pick a capability from a
# dropdown to get the right model. An explicit task still wins, because a caller
# that knows should not be second-guessed by a cheap model.
DEFAULT_TASK = "frontdesk_operations"

# How much of the thread the model is given. Shorter than a guest conversation's
# on purpose: front desk turns are mostly independent — "who's arriving", then
# "check in TB-9F2K" — and a long history is context the model will try to
# reconcile rather than ignore.
HISTORY_TURNS = 6


def _external_id(member) -> str:
    """
    One internal thread per person per property.

    Keyed on the profile rather than the membership: `member_id` is null for an
    owner who has no `partner_members` row, and an owner asking the front desk
    agent a question is the most likely person to be in that state.
    """
    return f"staff:{member.profile_id}"


def _find_or_open(property_id: str, partner_id: str, external_id: str,
                  display_name: str | None) -> dict:
    db = get_db()
    existing = (
        db.table("conversations").select("*")
        .eq("property_id", property_id).eq("channel", CHANNEL)
        .eq("external_id", external_id).limit(1).execute().data or []
    )
    if existing:
        return existing[0]

    return db.table("conversations").insert({
        "property_id": property_id,
        "partner_id": partner_id,
        "channel": CHANNEL,
        "external_id": external_id,
        "guest_name": display_name,
        "handler": "ai",
        "status": "open",
        # No guest, no phone, no handle. A staff thread has none of those and
        # writing the member's name into `guest_name` is only so the inbox has
        # something to show; nothing reads it as a guest identity, and
        # `guest_id` stays null so no guest-scoped lookup can resolve through it.
    }).execute().data[0]


def _write_message(conversation: dict, spec: dict) -> dict:
    """
    Persist one side of the exchange.

    `spec` is a dict rather than keyword arguments because `run_db` forwards
    positionally only — the same shape, and for the same reason, as
    `messaging.service._queue_outbound`.

    `delivery_status='delivered'` with no provider id, because delivery on this
    channel is the HTTP response — there is no queue, no callback and no failure
    mode between writing the row and the receptionist reading it. Marking it
    'queued' like an outbound WhatsApp message would leave the inbox showing
    staff answers as permanently unsent.
    """
    now = datetime.now(timezone.utc).isoformat()
    direction = spec["direction"]
    body = spec["body"]
    db = get_db()
    row = db.table("conversation_messages").insert({
        "conversation_id": conversation["id"],
        "property_id": conversation["property_id"],
        "partner_id": conversation["partner_id"],
        "channel": CHANNEL,
        "direction": direction,
        "author": spec["author"],
        "author_name": spec.get("author_name"),
        "author_member": spec.get("author_member"),
        "message_type": "text",
        "body": body,
        "client_reference": f"internal:{uuid.uuid4()}",
        "agent_run_id": spec.get("run_id"),
        "delivery_status": "delivered",
        "sent_at": now,
    }).execute().data[0]

    patch = {"last_message": body[:200], "last_message_at": now}
    patch["last_inbound_at" if direction == "inbound" else "last_outbound_at"] = now
    db.table("conversations").update(patch).eq("id", conversation["id"]).execute()
    return row


def _reload(conversation_id: str) -> dict | None:
    rows = (
        get_db().table("conversations").select("*")
        .eq("id", conversation_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _history(conversation_id: str, before: str, limit: int) -> list[dict]:
    rows = (
        get_db().table("conversation_messages")
        .select("direction,body,sent_at")
        .eq("conversation_id", conversation_id)
        .lt("sent_at", before)
        .order("sent_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return [
        {"role": "user" if r["direction"] == "inbound" else "assistant", "content": r["body"]}
        for r in reversed(rows)
        if r.get("body")
    ]


class ConsoleRefused(Exception):
    """The turn did not happen, and the caller must say why rather than answer."""

    def __init__(self, code: str, detail: str, conversation: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.conversation = conversation


async def ask(
    *,
    property_id: str,
    member,
    message: str,
    task: str | None = None,
    provider=None,
    classifier=None,
) -> tuple[RunResult, dict]:
    """
    One front desk turn. Returns the run and the conversation it happened on.

    `member` is the authenticated staff member from `require_member` — the whole
    property scope derives from it, and no argument to this function or to any
    tool the agent calls can name a different one.

    `provider` is injectable so the E2E suite can script the model and then
    assert on the database rather than on prose. Everything else is real.
    """
    conversation = await run_db(
        _find_or_open, property_id, member.partner_id,
        _external_id(member), member.display_name,
    )

    # Persisted before the run, for the same reason the WhatsApp webhook
    # persists before it processes: a crash in the model call must leave the
    # question visible to a person, not lost.
    inbound = await run_db(_write_message, conversation, {
        "direction": "inbound", "body": message, "author": "staff",
        "author_name": member.display_name, "author_member": member.member_id,
        "run_id": None,
    })

    # Human takeover, and it is the same column WhatsApp uses. Checked after
    # persisting so a manager can still read what was asked while they were
    # holding the thread.
    if not messaging.agent_may_answer(conversation):
        raise ConsoleRefused(
            "HANDLER_IS_HUMAN",
            f"This thread is being handled by a person ({conversation.get('handler')}). "
            "Hand it back to the agent to ask it again.",
            conversation,
        )

    routed = await routing.route(conversation)
    if routed is None:
        raise ConsoleRefused(
            "NO_AGENT",
            "No agent is configured for the staff console at this property.",
            conversation,
        )
    agent_type, actor = routed

    ctx = await ai_context.load_context(
        property_id=property_id,
        partner_id=member.partner_id,
        agent_type=agent_type,
        conversation_id=conversation["id"],
        channel=CHANNEL,
        # The two that make this a staff run. `actor` comes from the agent's
        # type via `agents.actor_for`, never from a literal here — the point of
        # services/ai/routing existing at all.
        actor=actor,
        member_id=member.member_id,
        member_name=member.display_name,
    )

    history = await run_db(_history, conversation["id"], inbound["sent_at"], HISTORY_TURNS)
    result = await runtime.run_turn(
        ctx, message,
        history=history,
        trigger="manual",
        provider=provider,
        summary=message[:120],
        task=task,
        classifier=classifier,
    )

    # Stamp the question with the run that answered it, so an unanswered message
    # is visible as one. Same column, same meaning, as the WhatsApp pipeline.
    await run_db(_mark_answered, inbound["id"], result.run_id)

    if result.reply:
        await run_db(_write_message, conversation, {
            "direction": "outbound", "body": result.reply, "author": "agent",
            "author_name": ctx.principal.display_name, "author_member": None,
            "run_id": result.run_id,
        })

    return result, (await run_db(_reload, conversation["id"]) or conversation)


def _mark_answered(message_id: str, run_id: str) -> None:
    get_db().table("conversation_messages").update(
        {"agent_run_id": run_id}
    ).eq("id", message_id).execute()
