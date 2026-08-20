"""
The AI employee on the hotel's own website.

    visitor types  ──►  website conversation  ──►  run_turn  ──►  reply
                              │                       │
                              │                       └─ agent_actions → PMS
                              └─ inbox row the hotel can read and take over

**This is a transport, not a new agent.** The same Guest agent that answers on
WhatsApp and Instagram answers here, with the same tools, the same autonomy and
the same property scope — `agents.channels` gained `website` and that is the
whole of the wiring. A second "web assistant" with its own prompt would drift
from the one the hotel actually configured within a week.

Modelled on `services/ai/console.py` rather than on the messaging pipeline, and
the difference is the reason this file exists. The pipeline is built for
webhooks: persist, acknowledge, answer on a worker, deliver through a provider.
A visitor on a booking page is *waiting*, with a cursor blinking, so the answer
has to come back on the same request — and there is no provider to deliver
through, because delivery is the HTTP response.

**Who a visitor is.** Nobody, and that is the hard part. There is no account, no
phone number and no login, so identity is a signed session token this service
issues on the first message and the browser sends back on every later one.
Signed, not merely random, because the token names a property and a
conversation: an unsigned one would let anyone paste another visitor's id in and
read the hotel's replies to a stranger.

**Why it lives in `services/messaging` and not `services/pms`.** It gates on
the hotel's credit balance, and `services/pms` is forbidden from importing
billing — "running out of credits must never stop a hotel from running the
hotel", enforced by `tests/test_billing.py`. Answering a guest is the AI
product; checking somebody in is the subscription. This is the first, so it
belongs beside the pipeline that makes the same call for WhatsApp.

**What stops it being a free Gemini endpoint.** Four things, in order:
`resolve_published` (no hotel, no chat), the session signature, a per-session
message ceiling, and the hotel's own credit balance through `billing.allowed` —
the same gate the WhatsApp pipeline uses, so an exhausted hotel degrades to
"leave a message" here exactly as it degrades to the inbox there.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.config import settings
from app.db.client import get_db, run_db
from app.services import billing
from app.services.ai import context as ai_context
from app.services.ai import routing, runtime
from app.services.ai.errors import AgentNotConfigured
from app.services.messaging import service as messaging
from app.services.pms import public_booking

logger = logging.getLogger(__name__)

CHANNEL = "website"

# How much of the thread the model is given. Longer than the front desk's six —
# a booking conversation refers back ("the second one", "that rate") in a way
# an operational question does not.
HISTORY_TURNS = 12

# One visitor, one session, this many messages. A real booking conversation is
# a dozen turns; this is generous for a guest and cheap to hit for a script,
# and the failure is a polite handoff rather than an error.
MAX_SESSION_MESSAGES = 40

# A session token older than this is refused and the widget starts a new one.
# Long enough to survive a visitor going away to check their dates and coming
# back; short enough that a token leaked into a shared browser is not a key to
# the conversation a week later.
SESSION_TTL_SECONDS = 12 * 60 * 60

MAX_MESSAGE_CHARS = 1000

# What the widget says when the agent cannot answer. Not an error: a hotel whose
# credits have run out, or whose agent has been paused, still wants the enquiry.
FALLBACK_REPLY = (
    "I've passed this to the hotel team — someone will reply here shortly. "
    "If it's urgent, the hotel's phone number is on this page."
)


class WebChatRefused(Exception):
    """The turn did not happen. The caller answers with `reply`, not an error."""

    def __init__(self, reason: str, reply: str = FALLBACK_REPLY):
        super().__init__(reason)
        self.reason = reason
        self.reply = reply


@dataclass(frozen=True)
class ChatTurn:
    reply: str
    session: str
    conversation_id: str
    # True when a person now owns this thread. The widget stops promising an
    # instant answer and says the hotel will reply, which is the truth.
    handed_off: bool = False


# ── the session token ───────────────────────────────────────────────────────
#
# Same construction as the OAuth states next door: base64 payload, HMAC, an
# issued-at that expires. Reused deliberately — one way of signing a short-lived
# opaque token in this codebase, not three.


def _key() -> bytes:
    # The service key never leaves the server and already gates every other
    # secret here. A dedicated secret would be better hygiene and worse
    # operations: one more variable that can be forgotten on a deploy, and
    # forgetting it fails open into unsigned sessions.
    secret = settings.supabase_service_key or ""
    if not secret:
        # Unreachable on any deployment that can read the database at all, and
        # a loud failure rather than an unsigned token if it ever is reached.
        raise WebChatRefused("no_signing_key")
    return hashlib.sha256(f"webchat:{secret}".encode()).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_session(*, property_id: str, conversation_id: str) -> str:
    payload = json.dumps(
        {"p": property_id, "c": conversation_id, "t": int(time.time())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = _b64e(payload)
    sig = _b64e(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str | None, *, property_id: str) -> str | None:
    """
    The conversation id this token names, or None to start a new one.

    Returns None rather than raising on every failure mode — expired, forged,
    malformed, or issued for a different hotel. A visitor whose token has gone
    stale should get a working chat box, not an error, and the cost of being
    wrong is one new conversation row.
    """
    if not token or "." not in token:
        return None

    body, _, sig = token.partition(".")
    expected = _b64e(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        logger.info("website chat session signature did not verify")
        return None

    try:
        claims = json.loads(_b64d(body))
    except Exception:  # noqa: BLE001
        return None

    if claims.get("p") != property_id:
        # A token minted on one hotel's site, replayed on another's. The
        # signature is ours and still valid, which is exactly why this check
        # cannot be left to the signature.
        logger.warning("website chat session replayed across properties")
        return None

    if int(time.time()) - int(claims.get("t") or 0) > SESSION_TTL_SECONDS:
        return None

    return str(claims.get("c") or "") or None


# ── the conversation ────────────────────────────────────────────────────────


def _open_conversation(property_id: str, partner_id: str) -> dict:
    external_id = f"web:{uuid.uuid4()}"
    return (
        get_db()
        .table("conversations")
        .insert(
            {
                "property_id": property_id,
                "partner_id": partner_id,
                "channel": CHANNEL,
                "external_id": external_id,
                "handler": "ai",
                "status": "open",
                # No guest_id: nobody has said who they are yet. The agent's own
                # booking tools create a guest when one is actually needed, which
                # is the only moment we know anything true about this person.
            }
        )
        .execute()
        .data[0]
    )


def _load_conversation(conversation_id: str, property_id: str) -> dict | None:
    rows = (
        get_db()
        .table("conversations")
        .select("*")
        .eq("id", conversation_id)
        .eq("property_id", property_id)
        .eq("channel", CHANNEL)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _count_inbound(conversation_id: str) -> int:
    result = (
        get_db()
        .table("conversation_messages")
        .select("id", count="exact")
        .eq("conversation_id", conversation_id)
        .eq("direction", "inbound")
        .execute()
    )
    return int(result.count or 0)


def _write_message(conversation: dict, spec: dict) -> dict:
    """
    Persist one side of the exchange.

    `delivery_status='delivered'` with no provider id, for the same reason the
    staff console does it: delivery on this channel *is* the HTTP response.
    Marking it queued would leave every website answer showing in the hotel's
    inbox as permanently unsent.
    """
    now = datetime.now(timezone.utc).isoformat()
    direction = spec["direction"]
    body = spec["body"]

    db = get_db()
    row = (
        db.table("conversation_messages")
        .insert(
            {
                "conversation_id": conversation["id"],
                "property_id": conversation["property_id"],
                "partner_id": conversation["partner_id"],
                "channel": CHANNEL,
                "direction": direction,
                "author": spec["author"],
                "author_name": spec.get("author_name"),
                "message_type": "text",
                "body": body,
                "client_reference": f"web:{uuid.uuid4()}",
                "agent_run_id": spec.get("run_id"),
                "delivery_status": "delivered",
                "sent_at": now,
            }
        )
        .execute()
        .data[0]
    )

    patch = {"last_message": body[:200], "last_message_at": now}
    patch["last_inbound_at" if direction == "inbound" else "last_outbound_at"] = now
    if direction == "inbound":
        # So the hotel's inbox shows an unanswered website enquiry the same way
        # it shows an unanswered DM.
        patch["unread_count"] = int(conversation.get("unread_count") or 0) + 1
    db.table("conversations").update(patch).eq("id", conversation["id"]).execute()
    return row


def _history(conversation_id: str, before: str, limit: int) -> list[dict]:
    rows = (
        get_db()
        .table("conversation_messages")
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


def _mark_answered(message_id: str, run_id: str) -> None:
    get_db().table("conversation_messages").update({"agent_run_id": run_id}).eq(
        "id", message_id
    ).execute()


def _reload(conversation_id: str) -> dict | None:
    rows = (
        get_db().table("conversations").select("*").eq("id", conversation_id).limit(1).execute().data
        or []
    )
    return rows[0] if rows else None


# ── the turn ────────────────────────────────────────────────────────────────


def opening_line(property_name: str) -> str:
    return (
        f"Hi! I'm the AI employee for {property_name}. "
        "Ask me about rooms, rates, or anything else — I can take a booking too."
    )


async def ask(
    *,
    slug: str,
    message: str,
    session: str | None,
    provider=None,
    classifier=None,
) -> ChatTurn:
    """
    One website turn. Never raises for an ordinary failure.

    Everything that can go wrong on a public page — the hotel is out of credits,
    a person has taken the thread over, the agent is misconfigured — comes back
    as a sentence a guest can read, with the message safely persisted so the
    hotel sees it. The only exception raised is a 404 for a hotel that is not
    published, which `resolve_published` owns.
    """
    text = (message or "").strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Say something first.")
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS]

    prop = await public_booking.resolve_published(slug)

    conversation_id = read_session(session, property_id=prop.id)
    conversation = (
        await run_db(_load_conversation, conversation_id, prop.id) if conversation_id else None
    )
    if conversation is None:
        conversation = await run_db(_open_conversation, prop.id, prop.partner_id)

    token = issue_session(property_id=prop.id, conversation_id=conversation["id"])

    # Persisted before anything can refuse it, for the same reason the WhatsApp
    # webhook persists before it processes: a question the hotel never sees is
    # worse than a question nobody answered.
    inbound = await run_db(
        _write_message,
        conversation,
        {"direction": "inbound", "body": text, "author": "guest", "author_name": None, "run_id": None},
    )

    turn = ChatTurn(reply="", session=token, conversation_id=conversation["id"])

    used = await run_db(_count_inbound, conversation["id"])
    if used > MAX_SESSION_MESSAGES:
        await _say(conversation, FALLBACK_REPLY)
        return _with(turn, FALLBACK_REPLY, handed_off=True)

    # A person has the thread. Same column, same meaning, as WhatsApp.
    if not messaging.agent_may_answer(conversation):
        reply = "Someone from the hotel is reading this — they'll reply here shortly."
        await _say(conversation, reply)
        return _with(turn, reply, handed_off=True)

    routed = await routing.route(conversation)
    if routed is None:
        logger.info("no agent on the website channel for property %s", prop.id)
        await messaging.request_human(conversation["id"], prop.id)
        await _say(conversation, FALLBACK_REPLY)
        return _with(turn, FALLBACK_REPLY, handed_off=True)
    agent_type, actor = routed

    # The same credit gate the pipeline uses, and it fails open on a Supabase
    # outage for the same reason: a hotel reading as "out of credits" because a
    # database was slow is worse than one free turn.
    decision = await billing.allowed(
        usage_type=billing.usage_type_for_agent(agent_type), property_id=prop.id
    )
    if not decision.allowed:
        logger.info("website agent paused on %s: %s", conversation["id"], decision.reason)
        await messaging.request_human(conversation["id"], prop.id)
        await _say(conversation, FALLBACK_REPLY)
        return _with(turn, FALLBACK_REPLY, handed_off=True)

    try:
        ctx = await ai_context.load_context(
            property_id=prop.id,
            partner_id=prop.partner_id,
            agent_type=agent_type,
            conversation_id=conversation["id"],
            channel=CHANNEL,
            actor=actor,
        )
    except AgentNotConfigured:
        logger.warning("website chat: no usable agent for property %s", prop.id)
        await messaging.request_human(conversation["id"], prop.id)
        await _say(conversation, FALLBACK_REPLY)
        return _with(turn, FALLBACK_REPLY, handed_off=True)

    history = await run_db(_history, conversation["id"], inbound["sent_at"], HISTORY_TURNS)

    try:
        result = await runtime.run_turn(
            ctx,
            text,
            history=history,
            # One of the five `agent_runs.trigger` values. A website turn is a
            # message from a person, the same as a DM; there is no separate
            # trigger for it and inventing one fails the check constraint.
            trigger="message",
            provider=provider,
            summary=text[:120],
            classifier=classifier,
        )
    except Exception:  # noqa: BLE001
        # A model or tool failure must not show a stranger a 500 on a hotel's
        # booking page. The enquiry is already saved; hand it to a person.
        logger.exception("website chat turn failed on property %s", prop.id)
        await messaging.request_human(conversation["id"], prop.id)
        await _say(conversation, FALLBACK_REPLY)
        return _with(turn, FALLBACK_REPLY, handed_off=True)

    await run_db(_mark_answered, inbound["id"], result.run_id)

    reply = (result.reply or "").strip() or FALLBACK_REPLY
    await _say(conversation, reply, run_id=result.run_id, author_name=ctx.principal.display_name)

    fresh = await run_db(_reload, conversation["id"]) or conversation
    return _with(turn, reply, handed_off=not messaging.agent_may_answer(fresh))


async def _say(conversation: dict, body: str, *, run_id: str | None = None,
               author_name: str | None = None) -> None:
    await run_db(
        _write_message,
        conversation,
        {
            "direction": "outbound",
            "body": body,
            "author": "agent" if run_id else "system",
            "author_name": author_name,
            "run_id": run_id,
        },
    )


def _with(turn: ChatTurn, reply: str, *, handed_off: bool = False) -> ChatTurn:
    return ChatTurn(
        reply=reply,
        session=turn.session,
        conversation_id=turn.conversation_id,
        handed_off=handed_off,
    )
