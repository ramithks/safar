"""
The agent runtime.

One function does the work — `run_turn` — and its shape is the architecture:

    agent_run                       opened before anything else happens
      │
      ├─ model step                 provider asks: speak, or call tools?
      │    └─ registry.execute()    permission → autonomy → validate → run
      │         └─ agent_action     written for every call, including refusals
      │
      ├─ model step                 the model sees the tool results and continues
      └─ agent_run finished         status, tokens, latency, and a reply

Three properties are worth stating because they are what make this auditable
rather than merely logged.

**The run row is opened first.** A crash mid-turn leaves a run marked `running`
with the actions it managed, which is a diagnosable state. Writing the run at
the end would lose exactly the turns worth investigating.

**Refusals are recorded.** A tool the agent was not allowed to call produces an
`agent_actions` row with status `refused` and the reason. A blocked action that
leaves no trace is indistinguishable from an action nobody attempted, and the
first question after a bad AI interaction is always "what did it try".

**The model never sees a tool it cannot call.** `registry.available_for` filters
by grant, so an agent without `get_quote` is not told get_quote exists. Refusal
is the backstop, not the mechanism.
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from app.config import settings
from app.db.client import get_db, run_db
from app.services.ai import model_router, prompts, registry, usage as usage_mod
from app.services.ai.context import AgentContext
from app.services.ai.errors import AgentError, ProviderError
from app.services.ai.providers import ModelStep, Provider, for_name
from app.services.ai.schemas import RunResult, ToolOutcome

logger = logging.getLogger(__name__)

registry.load_all()


# ── the run and action rows ──────────────────────────────────────────────────

def _open_run(ctx: AgentContext, trigger: str, summary: str | None,
              provider: str, model: str) -> str:
    def _write():
        row = {
            "agent_id": ctx.principal.agent_id,
            "property_id": ctx.property_id,
            "partner_id": ctx.partner_id,
            "config_id": ctx.config_id,
            "config_version": ctx.config_version,
            "conversation_id": ctx.conversation_id,
            "member_id": ctx.member_id,
            "trigger": trigger,
            "summary": summary,
            "status": "running",
            "provider": provider,
            "model": model,
        }
        return get_db().table("agent_runs").insert(row).execute().data[0]["id"]

    return _write()


def _write_action(ctx: AgentContext, run_id: str, seq: int, outcome: ToolOutcome) -> str | None:
    """
    One row per tool call, whatever happened.

    `output` is truncated rather than dropped when a tool returns something
    large: an availability response over sixty nights is not evidence, but the
    shape of it is, and a row with no output at all cannot be reviewed.
    """
    def _write():
        payload = outcome.output
        if payload is not None:
            encoded = json.dumps(payload, default=str)
            if len(encoded) > 8000:
                payload = {"truncated": True, "preview": encoded[:2000]}

        row = {
            "run_id": run_id,
            "agent_id": ctx.principal.agent_id,
            "property_id": ctx.property_id,
            "partner_id": ctx.partner_id,
            "seq": seq,
            "tool": outcome.tool,
            "input": outcome.input or {},
            "output": payload,
            "result_type": outcome.result_type,
            "result_id": outcome.result_id,
            "status": outcome.status,
            "refusal_reason": outcome.refusal_reason,
            "latency_ms": outcome.latency_ms,
            "error": outcome.error,
        }
        return get_db().table("agent_actions").insert(row).execute().data[0]["id"]

    try:
        return _write()
    except Exception:  # noqa: BLE001
        # A failure to write the audit row must not take the guest's answer with
        # it. Logged loudly: this is the one thing here that should page someone.
        logger.exception("could not record agent action %s for run %s", outcome.tool, run_id)
        return None


# Which table holds the provenance column, by what the tool says it created.
#
# A table per result type rather than a branch: adding the Front Desk agent's
# folio charge meant one entry here, not another `if` in a function whose whole
# job is one UPDATE. Result types with no provenance column — a quote, a hold, a
# room status — are simply absent, and absence means "nothing to stamp".
_PROVENANCE_TABLES = {
    "task": "tasks",              # migration 5, FK added in 10
    "folio_entry": "folio_entries",  # migration 15
}


def link_result(outcome: ToolOutcome, action_id: str | None) -> None:
    """
    Point the created thing back at the action that created it.

    This is what closes the loop from a sentence somebody typed to a job on a
    board or a line on a bill: the row knows which AI action produced it, and
    the action knows which row it produced. An owner reading a ₹1,200 laundry
    charge six months later can get from the charge to the conversation.

    Guarded on `creates_result`, not on `result_type`. `assign_task` reports the
    same result type as `create_task` without being the ticket's author, and
    linking on type alone made the ticket point at whichever action touched it
    last — which is provenance that answers the wrong question.
    """
    if not action_id or not outcome.result_id:
        return
    spec = registry.REGISTRY.get(outcome.tool)
    if not spec or not spec.creates_result:
        return
    table = _PROVENANCE_TABLES.get(spec.result_type)
    if not table:
        return

    def _write():
        get_db().table(table).update({"agent_action_id": action_id}).eq(
            "id", outcome.result_id
        ).execute()

    try:
        _write()
    except Exception:  # noqa: BLE001
        logger.warning("could not link %s %s to action %s",
                       table, outcome.result_id, action_id)


def _stamp_message_action(message_id: str, action_id: str) -> None:
    """
    Point an outbound message at the action that sent it.

    The mirror of `link_result`, and the same ordering problem: the tool
    returns before its action row exists, so the stamp has to happen here.
    Together they make the trace run both ways — an auditor reading a message a
    guest received can find the run that decided to send it.
    """
    try:
        get_db().table("conversation_messages").update(
            {"agent_action_id": action_id}
        ).eq("id", message_id).execute()
    except Exception:  # noqa: BLE001
        logger.warning("could not link message %s to action %s", message_id, action_id)


def _close_run(run_id: str, fields: dict) -> None:
    def _write():
        get_db().table("agent_runs").update(
            {"finished_at": datetime.now(timezone.utc).isoformat(), **fields}
        ).eq("id", run_id).execute()

    try:
        _write()
    except Exception:  # noqa: BLE001
        logger.exception("could not close agent run %s", run_id)


# ── the turn ─────────────────────────────────────────────────────────────────

async def run_turn(
    ctx: AgentContext,
    message: str,
    *,
    history: list[dict] | None = None,
    trigger: str = "message",
    provider: Provider | None = None,
    summary: str | None = None,
    task: str = "guest_conversation",
) -> RunResult:
    """
    One guest turn, from a message to a reply.

    `history` is prior turns in provider shape — the transport owns the thread,
    because it is the transport that knows which messages were actually
    delivered. `provider` is injectable so the tests can script the model and
    then assert on the database rather than on prose.

    `task` is a *capability*, not a model. `model_router` turns it into a model,
    a thinking budget and a token ceiling, so no model id appears in this file
    — see services/ai/model_router.py for why that indirection earns its keep.
    """
    started = time.monotonic()
    llm = provider or for_name(ctx.provider)

    # `agent_configs.model` is the exception, not the rule. Empty means "let the
    # router decide", which is what almost every property wants — a model id
    # pinned on a config row is one that has to be edited per hotel when a tier
    # moves. A property given a specific model for a reason still keeps it, and
    # the tier still decides how hard it thinks and how much it may say.
    choice = model_router.select(
        agent_type=ctx.principal.agent_type, task=task, override_model=ctx.model or None
    )
    params = {
        "temperature": ctx.params.get("temperature", choice.temperature),
        "max_output_tokens": ctx.params.get("max_output_tokens", choice.max_output_tokens),
        "thinking_budget": ctx.params.get("thinking_budget", choice.thinking_budget),
    }
    turn_usage = usage_mod.TurnUsage(provider=llm.name, model=choice.model)

    run_id = await run_db(_open_run, ctx, trigger, summary, llm.name, choice.model)
    ctx.run_id = run_id

    messages: list[dict] = [{"role": "system", "content": prompts.build_system_prompt(ctx)}]
    messages += history or []
    messages.append({"role": "user", "content": message})

    tools = registry.describe_for_provider(ctx)
    outcomes: list[ToolOutcome] = []
    # usage_type -> the action rows that produced it, so a usage event can point
    # at the action rather than only at the run.
    action_ids: dict[str, list[str]] = {}
    seq = 0
    reply: str | None = None
    handoff = False
    status = "succeeded"
    error: str | None = None

    try:
        for _turn in range(settings.agent_max_tool_turns):
            step: ModelStep = await llm.step(
                model=choice.model, messages=messages, tools=tools, params=params
            )
            turn_usage.add_tokens(step.tokens_in, step.tokens_out, step.tokens_thought)
            turn_usage.used_model(choice.model)

            if not step.wants_tools:
                reply = (step.reply or "").strip() or None
                break

            messages.append(step.raw or {"role": "assistant", "content": None})

            for call in step.tool_calls:
                outcome = await registry.execute(call.name, call.arguments, ctx)
                seq += 1
                outcomes.append(outcome)

                spec = registry.REGISTRY.get(call.name)
                if spec:
                    turn_usage.count(spec.usage_type)

                action_id = await run_db(_write_action, ctx, run_id, seq, outcome)
                await run_db(link_result, outcome, action_id)
                if spec and action_id:
                    action_ids.setdefault(spec.usage_type, []).append(action_id)
                    # The tool needs the action id to stamp on whatever it
                    # produced — an outbound message has to know which action
                    # sent it. Only send_message uses this today; passing it
                    # after the fact is the only order that works, because the
                    # action row does not exist until the tool has returned.
                    if outcome.tool == "send_message" and outcome.result_id:
                        await run_db(_stamp_message_action, outcome.result_id, action_id)

                if outcome.tool == "request_human_handoff" and outcome.ok:
                    handoff = True

                # What the model is shown. A refusal goes back as text it can
                # act on, not as an empty result it will retry — that is the
                # difference between an agent that says "let me get someone"
                # and one that tries the same forbidden thing four times.
                content = (
                    json.dumps(outcome.output, default=str)
                    if outcome.ok
                    else json.dumps({"refused": True, "reason": outcome.model_message})
                )
                messages.append(llm.tool_result_message(call, content))
        else:
            # Ran out of turns without an answer. Rare, and a handoff rather
            # than a truncated guess.
            logger.warning("run %s hit the tool-turn ceiling", run_id)
            handoff = True

        if handoff:
            status = "escalated"
        if reply is None and not handoff:
            reply = None
            status = "failed"
            error = "the model produced no reply"

    except ProviderError as exc:
        # The model is gone. The conversation goes to a human, and the guest is
        # told something true rather than nothing.
        logger.warning("provider failed on run %s: %s", run_id, exc.reason)
        status, error, handoff = "failed", exc.reason, True
        reply = prompts.handoff_reply(ctx)
    except AgentError as exc:
        status, error, handoff = "failed", exc.reason, True
        reply = exc.guest_message
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent run %s failed", run_id)
        status, error, handoff = "failed", f"{type(exc).__name__}: {exc}", True
        reply = prompts.handoff_reply(ctx)

    latency = int((time.monotonic() - started) * 1000)
    turn_usage.count("ai.conversation_turn")
    usage_mod.record(run_id, turn_usage)

    await run_db(_close_run, run_id, {
        "status": status,
        "tokens_in": turn_usage.tokens_in,
        "tokens_out": turn_usage.tokens_out,
        "latency_ms": latency,
        "error": error,
    })

    # Charged after the run, when the real token counts are known — never
    # before, and never as a two-phase commit on a conversational turn. The
    # gate is `billing.allowed()`, consulted by the caller before any of this
    # happened; by the time we are here the work is done and the ledger records
    # it whatever the balance says.
    #
    # Swallowed on failure inside billing.charge(): a meter that can take down
    # a guest conversation is a worse bug than a meter with a gap in it.
    try:
        await usage_mod.record_run(
            run_id=run_id,
            property_id=ctx.property_id,
            partner_id=ctx.partner_id,
            agent_id=ctx.principal.agent_id,
            agent_type=ctx.principal.agent_type,
            conversation_id=ctx.conversation_id,
            usage=turn_usage,
            action_ids=action_ids,
            latency_ms=latency,
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not record usage for run %s", run_id, exc_info=True)

    # A handoff always leaves the conversation with a human, even when the
    # handoff came from a crash rather than from the tool. The rule is that an
    # unattended conversation is never left believing an agent is still on it.
    if handoff and ctx.conversation_id:
        await run_db(_ensure_human, ctx)

    return RunResult(
        run_id=run_id,
        status=status,
        reply=reply,
        actions=outcomes,
        handoff=handoff,
        tokens_in=turn_usage.tokens_in,
        tokens_out=turn_usage.tokens_out,
        latency_ms=latency,
        error=error,
    )


def _ensure_human(ctx: AgentContext) -> None:
    """
    Stop the agent answering, whatever the reason it stopped.

    'human_requested' rather than 'human': nobody has picked it up. Guarded on
    the current handler so a crash mid-turn on a conversation a receptionist had
    *already taken over* does not demote it back to the queue.
    """
    try:
        get_db().table("conversations").update(
            {"handler": "human_requested"}
        ).eq("id", ctx.conversation_id).eq("property_id", ctx.property_id).eq(
            "handler", "ai"
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("could not mark conversation %s for a human", ctx.conversation_id)


def agent_is_handling(conversation: dict) -> bool:
    """
    Whether the runtime may answer at all.

    **Human takeover stops autonomous replies.** A member who takes a
    conversation over owns it until they explicitly hand it back, and this is
    the check that makes that true rather than customary. Called by the
    transport before a run is even opened, so a taken-over conversation costs
    nothing and leaves no trace.

    Delegates to `messaging.service.agent_may_answer`, which also honours the
    per-conversation `ai_enabled` switch — a receptionist can turn the AI off
    for one difficult guest and have it stay off through every later handover.
    """
    from app.services.messaging import service as messaging

    return messaging.agent_may_answer(conversation)


def new_conversation_id() -> str:
    return str(uuid.uuid4())
