"""
Inbound: from a persisted message to a reply.

    persist  ──►  lock the conversation  ──►  drain unanswered messages in order
                                                     │
                                                     ├─ load agent context
                                                     ├─ run_turn
                                                     └─ send_message (by the agent)

Persistence happens in the webhook handler, before this runs. That ordering is
the point of the whole file: **a provider retry must never create an untracked
agent run**, so the idempotent write has to be the first thing that happens and
the model call the last.

**Ordering per conversation.** Two messages from one guest arriving a second
apart must not become two concurrent runs that answer out of order — "yes we
have a room" landing after "let me check". So processing takes a Redis lock
keyed on the conversation and then drains *every* unanswered inbound message in
`sent_at` order. A second webhook that arrives while the lock is held returns
immediately; its message is already persisted, and the holder will pick it up.

That is deliberately the smallest thing that works. A per-conversation queue,
partitioned consumers and offset tracking would all be more correct in the
abstract and would be a distributed system to debug at a hotel with forty rooms.

When Redis is not reachable the lock degrades to a no-op with a warning rather
than failing the message. A duplicated reply is bad; an unanswered guest is
worse, and a hotel running without Redis is a hotel where the alternative is
nothing being processed at all.
"""
import logging
from contextlib import contextmanager

from app.config import settings
from app.db.client import get_db, run_db
from app.services.ai import context as ai_context
from app.services.ai import routing as ai_routing
from app.services.ai import runtime
from app.services.ai.errors import AgentError
from app.services import billing
from app.services.messaging import service as messaging

logger = logging.getLogger(__name__)

# Long enough for a slow model call plus a send, short enough that a crashed
# worker does not wedge a guest's conversation for the rest of the day.
LOCK_TTL_SECONDS = 90
# How many messages one drain will answer before giving up its turn. A guest
# firing off nine messages gets them handled; a loop does not run forever.
MAX_DRAIN = 8


@contextmanager
def conversation_lock(conversation_id: str):
    """
    One worker per conversation. Yields False when somebody else holds it.

    `set(nx=True, ex=TTL)` rather than a Redlock: this is one Redis, the
    critical section is short, and the failure mode of losing the lock is a
    duplicated reply rather than a double booking. The stronger primitive would
    be protecting the wrong thing — inventory is already protected in Postgres.
    """
    client = None
    key = f"tb:conv:lock:{conversation_id}"
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_timeout=2)
        acquired = bool(client.set(key, "1", nx=True, ex=LOCK_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        logger.warning("no Redis for conversation ordering; processing unlocked")
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired:
            try:
                client.delete(key)
            except Exception:  # noqa: BLE001
                pass  # The TTL will clear it.


# ── what still needs answering ───────────────────────────────────────────────

def _pending(conversation_id: str) -> list[dict]:
    """
    Inbound messages nobody has answered.

    `agent_run_id is null` — the message says so itself. The first version
    derived this by comparing each message's `sent_at` against the last run's
    `started_at`, which reads as elegant and is wrong at one-second
    granularity: two messages from the same guest inside one second left the
    second one permanently unanswered. Timestamps are a proxy for "has this been
    handled"; the column is the thing itself.
    """
    return (
        get_db()
        .table("conversation_messages")
        .select("*")
        .eq("conversation_id", conversation_id)
        .eq("direction", "inbound")
        .is_("agent_run_id", "null")
        .order("sent_at")
        .limit(MAX_DRAIN)
        .execute()
        .data
        or []
    )


def _mark_answered(message_id: str, run_id: str) -> None:
    get_db().table("conversation_messages").update(
        {"agent_run_id": run_id}
    ).eq("id", message_id).execute()


def _conversation(conversation_id: str) -> dict | None:
    rows = (
        get_db().table("conversations").select("*")
        .eq("id", conversation_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _history(conversation_id: str, before: str, limit: int = 10) -> list[dict]:
    """
    Recent turns, in provider shape.

    Only what was actually delivered — a queued or failed outbound message is
    something the guest never saw, and replaying it as context would have the
    agent answer as if it had already said something it never said.
    """
    rows = (
        get_db().table("conversation_messages")
        .select("direction,body,delivery_status,sent_at")
        .eq("conversation_id", conversation_id)
        .lt("sent_at", before)
        .order("sent_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    out = []
    for row in reversed(rows):
        if not row.get("body"):
            continue
        if row["direction"] == "outbound" and row.get("delivery_status") in ("queued", "failed"):
            continue
        out.append({
            "role": "user" if row["direction"] == "inbound" else "assistant",
            "content": row["body"],
        })
    return out


# ── the turn ─────────────────────────────────────────────────────────────────

def _agent_text(message: dict) -> str:
    """
    What the agent is given.

    An unsupported type is described rather than dropped. The guest sent a
    voice note and deserves "I can't listen to that, let me get someone" — not
    silence, which is the one outcome a hotel cannot explain.
    """
    if message.get("body"):
        return message["body"]
    kind = message.get("message_type") or "unsupported"
    return (
        f"[The guest sent a {kind} message which you cannot open. "
        "Tell them you cannot read it and offer to put them through to the team.]"
    )


async def process_conversation(conversation_id: str, *, provider=None) -> dict:
    """
    Answer everything outstanding on one conversation.

    Safe to call more than once: a second call with nothing pending does
    nothing, which is what makes it safe to fire from both a webhook and a
    retry.
    """
    conversation = await run_db(_conversation, conversation_id)
    if not conversation:
        return {"processed": 0, "reason": "conversation not found"}

    if not messaging.agent_may_answer(conversation):
        # Checked before a run is opened, so a taken-over conversation costs
        # nothing and leaves no trace. This is the whole of "human takeover
        # stops autonomous replies".
        return {"processed": 0, "reason": f"handler is {conversation.get('handler')}"}

    # Which agent, from the channel and the property's own configuration.
    #
    # This used to be the literal string "guest", in three places on this path:
    # the agent type, the actor, and the usage type the credit gate priced. That
    # was right while one agent existed and would have silently routed a staff
    # conversation to the Guest agent — guest-scoped, so unable to read the
    # arrivals list, and billed under the wrong line. See services/ai/routing.
    routed = await ai_routing.route(conversation)
    if routed is None:
        logger.info("no agent configured for %s on property %s",
                    conversation.get("channel"), conversation["property_id"])
        await messaging.request_human(conversation["id"], conversation["property_id"])
        return {"processed": 0, "reason": "no agent for this channel"}
    agent_type, actor = routed

    # The credit gate, and the only one on this path.
    #
    # Checked before the run rather than after it, because a turn that has
    # already happened is charged regardless — the ledger records consumption,
    # not intent. At zero the agent stops and the conversation goes to the
    # inbox, which is §8.9: AI and messaging degrade, and nothing else does.
    #
    # A person resumes it. That is deliberate rather than a limitation: an
    # agent silently reappearing mid-thread after a top-up, several messages
    # into a conversation a human has been holding, is worse than asking the
    # hotel to hand it back.
    #
    # `billing.allowed()` fails open, so a Supabase outage does not read to a
    # hotelier as being cut off for non-payment.
    decision = await billing.allowed(
        usage_type=billing.usage_type_for_agent(agent_type),
        property_id=conversation["property_id"],
    )
    if not decision.allowed:
        logger.info("agent paused on conversation %s: %s (balance %s)",
                    conversation_id, decision.reason, decision.balance)
        await messaging.request_human(conversation["id"], conversation["property_id"])
        return {"processed": 0, "reason": decision.reason, "tier": decision.tier}

    processed = 0
    runs: list[str] = []

    # Re-queried each pass rather than snapshotted once.
    #
    # The bug this shape fixes: a guest's second message arriving *while* this
    # worker held the lock was never answered. Its own task found the lock held
    # and returned; this worker had already computed its work list before that
    # message existed. Nobody came back for it, and the guest sat unanswered
    # with no error anywhere. Caught by scripts/celery_ordering_e2e.py.
    while processed < MAX_DRAIN:
        pending = await run_db(_pending, conversation_id)
        if not pending:
            break

        message = pending[0]
        try:
            ctx = await ai_context.load_context(
                property_id=conversation["property_id"],
                partner_id=conversation["partner_id"],
                agent_type=agent_type,
                conversation_id=conversation["id"],
                guest_id=conversation.get("guest_id"),
                guest_name=conversation.get("guest_name"),
                guest_phone=conversation.get("guest_phone"),
                reservation_id=conversation.get("reservation_id"),
                channel=conversation["channel"],
                actor=actor,
            )
        except AgentError as exc:
            # No agent configured for this property. Not a failure of this
            # message — the hotel simply has no AI — so it goes to the inbox
            # and a person answers it.
            logger.info("no agent for property %s: %s", conversation["property_id"], exc.reason)
            await messaging.request_human(conversation["id"], conversation["property_id"])
            return {"processed": 0, "reason": "no agent configured"}

        history = await run_db(_history, conversation_id, message["sent_at"])
        result = await runtime.run_turn(
            ctx,
            _agent_text(message),
            history=history,
            trigger="webhook",
            provider=provider,
            summary=(message.get("body") or message.get("message_type") or "")[:120],
        )
        # Stamped before anything else can fail. A crash after the run but
        # before the mark would have the next drain answer the same message
        # again — two replies to one guest sentence, which is exactly the
        # failure the idempotency work exists to prevent.
        await run_db(_mark_answered, message["id"], result.run_id)
        runs.append(result.run_id)
        processed += 1

        # The agent replies with `send_message`. When it produced prose but
        # never called the tool — or the run failed — the reply still has to
        # reach the guest, so the pipeline sends it. Skipped after a handoff:
        # the handoff tool already said its piece, and a second message would
        # be the AI talking over the human it just called for.
        already_sent = any(a.tool == "send_message" and a.ok for a in result.actions)
        if result.reply and not already_sent:
            try:
                await _deliver(conversation, result)
            except Exception:  # noqa: BLE001
                logger.warning("could not deliver reply for run %s", result.run_id,
                               exc_info=True)

        if result.handoff:
            break

        conversation = await run_db(_conversation, conversation_id) or conversation
        if not messaging.agent_may_answer(conversation):
            break

    if not processed:
        return {"processed": 0, "reason": "nothing pending"}
    return {"processed": processed, "runs": runs}


async def _deliver(conversation: dict, result) -> None:
    from app.services.messaging.channels import MessagingError

    try:
        # `client_reference` keyed on the run, so a retried task cannot send the
        # same reply twice. Metering happens inside `send`.
        await messaging.send(
            conversation, body=result.reply, author="agent", author_name="Agent",
            client_reference=f"run:{result.run_id}", run_id=result.run_id,
        )
    except MessagingError as exc:
        # The window closed between the guest writing and us answering, or the
        # provider is down. Either way this goes to a person rather than
        # disappearing.
        logger.info("could not send agent reply: %s", exc.code)
        await messaging.request_human(conversation["id"], conversation["property_id"])


async def dispatch(conversation_id: str, *, provider=None) -> None:
    """
    Hand the conversation to a worker, or do it here.

    The webhook must acknowledge Meta quickly — Meta retries anything slow, and
    a retry that arrives while the model is still thinking is exactly the
    duplicate the persistence step exists to absorb. So the default is Celery.

    Inline is for development and the E2E suite, and it is a setting rather than
    an inference so nothing can accidentally block a production webhook on a
    model call.

    Async because the webhook handler that calls it is: awaiting the work is
    what the inline path means, and `asyncio.run` inside a running loop is a
    `RuntimeError`, not a shortcut.
    """
    if settings.messaging_inline_processing:
        await process_conversation(conversation_id, provider=provider)
        return

    try:
        from app.workers.messaging_tasks import process_conversation_task

        process_conversation_task.delay(conversation_id)
    except Exception:  # noqa: BLE001
        # No broker. The message is already persisted and the conversation is
        # already in the inbox, so a person can still answer it — which is why
        # this is a warning and not a 500 back to Meta.
        logger.warning(
            "could not enqueue conversation %s; it is persisted and visible in "
            "the inbox but the agent will not answer it", conversation_id,
            exc_info=True,
        )
