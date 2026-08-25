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
from app.services.ai import (
    classify, model_router, prompts, recovery, registry, required_actions,
    usage as usage_mod,
)
from app.services.ai.context import AgentContext
from app.services.ai.errors import AgentError, ProviderError
from app.services.ai import guardrails
from app.services.ai.providers import ModelStep, Provider, for_name
from app.services.ai.schemas import RunResult, ToolOutcome
from app.services.messaging import script

logger = logging.getLogger(__name__)

registry.load_all()


# ── the run and action rows ──────────────────────────────────────────────────

def _open_run(ctx: AgentContext, trigger: str, summary: str | None,
              provider: str, model: str, task: str, tier: str) -> str:
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
            # The routing decision, kept. It is what explains a turn that ran on
            # Pro and, for the sales agent, a turn that was billed at 20 credits
            # rather than 8 — see migration 16.
            "task": task,
            "model_tier": tier,
        }
        return get_db().table("agent_runs").insert(row).execute().data[0]["id"]

    return _write()


def _write_action(ctx: AgentContext, run_id: str, seq: int, outcome: ToolOutcome,
                  decision_source: str = "model", intent: str | None = None) -> str | None:
    """
    One row per tool call, whatever happened.

    `output` is truncated rather than dropped when a tool returns something
    large: an availability response over sixty nights is not evidence, but the
    shape of it is, and a row with no output at all cannot be reviewed.

    `decision_source` is the question the replay could not answer: the model
    calling `negotiate_rate` and the application calling it because the model
    did not produce identical rows, and the difference between them is the
    metric this whole reliability pass is measured by. See migration
    2026-08-24_a_required_action_is_not_optional.
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
            "decision_source": decision_source,
            "autonomy_level": ctx.principal.autonomy_level,
            "intent": intent,
            # The queue is the approval, and it is already visible in `status`.
            # This column answers the same question without the reader having to
            # know that 'awaiting_approval' is the string that means it.
            "human_approval_required": outcome.status == "awaiting_approval",
            # What the next attempt at this same guest message looks itself up
            # by. Null for reads, for staff turns and for the approval queue —
            # see services/ai/registry.execute.
            "idempotency_key": outcome.idempotency_key,
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
    "task": "tasks",                 # migration 5, FK added in 10
    "folio_entry": "folio_entries",  # migration 15
    "quote": "quotes",               # migration 16
    "reservation": "reservations",   # migration 16
    "recommendation": "revenue_recommendations",  # migration 18
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


def _intent_for(requirements: list, tool: str) -> str | None:
    """
    Which detected intent this call answers, if any.

    So a model call and a forced call for the same reason are filed under the
    same intent, and "how often does the model do this by itself" is one group
    by away rather than a join against the message text.
    """
    for requirement in requirements or []:
        if tool in requirement.satisfied_by:
            return requirement.intent
    return None


# ── the turn ─────────────────────────────────────────────────────────────────

async def run_turn(
    ctx: AgentContext,
    message: str,
    *,
    history: list[dict] | None = None,
    trigger: str = "message",
    provider: Provider | None = None,
    summary: str | None = None,
    task: str | None = None,
    classifier=None,
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

    **`task=None` means "work it out".** A caller that already knows the
    capability — the staff console, where a receptionist picked one — passes it.
    A caller that does not, which is every inbound message, leaves it and the
    classifier decides on the cheapest tier there is. That used to be a constant,
    with the consequence that the router's whole escalation table was
    unreachable in production: every guest turn in the system was routed as
    `guest_conversation` whatever it said.
    """
    started = time.monotonic()
    llm = provider or for_name(ctx.provider)

    # Before the run is opened, because the run row records what was decided.
    routed = None
    if task is None:
        routed = await (classifier or classify.for_provider(provider)).classify(
            agent_type=ctx.principal.agent_type, message=message, history=history,
        )
        task = routed.task

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
    turn_usage = usage_mod.TurnUsage(provider=llm.name, model=choice.model, tier=choice.tier)

    # The classification's own tokens. Counted on this turn rather than billed
    # separately: `ai.classification` is priced at zero on purpose — routing a
    # message well is plumbing, and charging for plumbing teaches the runtime to
    # skip it — but the tokens were spent and the margin report should see them.
    if routed is not None and routed.source == "model":
        turn_usage.add_tokens(routed.tokens_in, routed.tokens_out)
        turn_usage.used_model(routed.model)
        turn_usage.count("ai.classification")

    run_id = await run_db(_open_run, ctx, trigger, summary, llm.name, choice.model,
                          task, choice.tier)
    ctx.run_id = run_id

    messages: list[dict] = [{"role": "system", "content": prompts.build_system_prompt(ctx)}]
    messages += history or []
    messages.append({"role": "user", "content": message})

    # Narrowed by what the classifier already decided this message is about.
    #
    # The classification happens above whatever else it is used for, and until
    # now it only picked a model tier. The catalogue is about seventy per cent
    # of the base prompt and every step re-sends it, so letting one decision do
    # both jobs is the cheapest optimisation available here. It cannot widen a
    # permission: see services/ai/toolsets.py.
    tools = registry.describe_for_provider(
        ctx, task=task, opening=bool(routed is not None and routed.opening),
    )

    # Declared ahead of the rest of the turn's bookkeeping, because `ctx.turn`
    # is given *this* list object rather than a copy of it. A snapshot of what
    # has run so far is exactly the thing that must not go stale mid-turn: the
    # arbiter reads it from inside `registry.execute`, while the turn is still
    # filling it.
    outcomes: list[ToolOutcome] = []

    # What this message *requires*, decided before the model sees it.
    #
    # Guest turns only. A receptionist typing into the console is not a guest
    # asking for five rooms, and forcing a pricing tool onto a staff question
    # would be the system second-guessing the person it works for.
    #
    # Detection is regex over the guest's own words plus the structured session
    # — no model call, no round trip. See services/ai/required_actions.py for
    # why the two actions a hotel's revenue depends on stopped being the
    # model's decision.
    requirements = (
        required_actions.detect(message, ctx.session) if ctx.guest_scoped else []
    )

    # Published to the turn, so the layers that run *inside* `registry.execute`
    # can see what the turn is about. One does today: the arbiter, which decides
    # whether a handoff the model asked for is supported by the conversation it
    # claims to describe — a question whose evidence lives in the turn rather
    # than in the tool's arguments. See services/ai/arbiter.py and
    # services/ai/context.py:TurnState.
    ctx.turn.live = True
    # Cleared, not appended to. A context reused across turns — which the
    # console does — would otherwise report the previous turn's rule scopes
    # against this one's run.
    ctx.turn.telemetry = {}
    ctx.turn.message = message
    ctx.turn.requirements = tuple(requirements)
    ctx.turn.outcomes = outcomes
    ctx.turn.history = tuple(history or [])

    forced: list[str] = []
    # Devanagari words that had to be romanised for a Hinglish guest. Counted
    # rather than merely fixed: a language fix whose effect cannot be measured
    # is one nobody can tell has regressed. See services/messaging/script.py.
    language_slips: list[str] = []
    if requirements:
        logger.info("run %s requires %s", run_id,
                    [f"{r.intent}->{r.tool}" for r in requirements if r.must_execute])

    # usage_type -> the action rows that produced it, so a usage event can point
    # at the action rather than only at the run.
    action_ids: dict[str, list[str]] = {}
    seq = 0
    reply: str | None = None
    handoff = False
    status = "succeeded"
    error: str | None = None

    # The wall clock the guest is actually watching.
    #
    # `choice.timeout` bounds one request to Vertex; nothing bounded the turn.
    # Six tool rounds at forty-five seconds each is four and a half minutes, and
    # a measured turn during this work took 107 seconds before failing — by
    # which time the guest has closed Instagram. See `model_router.budget`.
    #
    # **Spending it is not an error.** It stops the loop asking for *another*
    # round of tools; the model still gets its turn to answer with what it
    # already has, which is the difference between a slower reply and no reply.
    # A turn that has spent its budget and has nothing to say hands over to a
    # person, which is the same ending as running out of tool turns.
    deadline = started + choice.budget
    out_of_time = False

    # ── the degradation ladder ───────────────────────────────────────────────
    #
    # `choice.fallbacks` is `3.6-flash → 3.5-flash → 2.5-flash`, and it ends on
    # the model that has been serving production all along. Not the escalation
    # ladder, which goes the other way: escalation is "that answer was not good
    # enough", this is "that model is not answering and a guest is waiting".
    #
    # It is here rather than in the provider because the provider does not know
    # what a turn is. `vertex.py` already retries transport failures across the
    # locations a model has; when it gives up, the model itself is the problem,
    # and the only thing left to change is which model.
    #
    # **Mid-turn is a fair place to switch.** The message list is provider shape,
    # not model shape, so a turn that has already run two tools carries them into
    # the fallback and continues rather than starting again.
    model_in_use = choice.model
    fallback_count = 0
    # Every provider-level attempt on this turn, successful or not. Separate
    # from `recovery_log`, which records the *ladder's* decisions: this is the
    # transport underneath it, where a model can be tried and fail without the
    # ladder ever being consulted.
    model_attempts: list[dict] = []
    tool_failures = 0
    # (tool, arguments) already executed on this turn. Two sets, because a
    # repeated read and a repeated write are different problems — see the loop.
    called_reads: set[tuple[str, str]] = set()
    called_writes: set[tuple[str, str]] = set()

    async def _step(*, messages: list[dict], tools: list[dict]) -> ModelStep:
        nonlocal model_in_use, fallback_count
        candidates = [model_in_use] + [
            m for m in choice.fallbacks if m != model_in_use
        ]
        last: Exception | None = None
        for i, model in enumerate(candidates):
            started = time.monotonic()
            try:
                out = await llm.step(
                    model=model, messages=messages, tools=tools, params=params
                )
            except ProviderError as exc:
                last = exc
                # **Recorded whether or not there is another rung.** The last
                # candidate failing is the turn ending, which is the case an
                # operator most wants to see in the row — and it was the one
                # case that logged nothing, because the record was written
                # inside the "there is somewhere else to go" branch.
                model_attempts.append(recovery.Attempt(
                    failure=exc.reason or "provider_error",
                    model=model,
                    method="fallback" if i + 1 < len(candidates) else "exhausted",
                    error_class=getattr(exc, "error_class", None),
                    latency_ms=int((time.monotonic() - started) * 1000),
                    provider_retries=getattr(exc, "provider_retries", 0),
                    provider_status=getattr(exc, "provider_status", None),
                ).as_dict())
                if i + 1 < len(candidates):
                    fallback_count += 1
                    logger.warning(
                        "run %s: %s failed (%s) — falling back to %s",
                        run_id, model, exc.reason, candidates[i + 1],
                    )
                continue
            # **A successful call that cost three region rotations is still
            # information.** Recorded whether or not anything failed, because
            # the question "was the provider under pressure on this turn" has
            # to be answerable on the turns that succeeded — otherwise a report
            # sees only the turns that broke and concludes the rest were fine.
            if getattr(out, "provider_retries", 0):
                model_attempts.append(recovery.Attempt(
                    failure="provider_retry",
                    model=model,
                    method="succeeded",
                    latency_ms=int((time.monotonic() - started) * 1000),
                    provider_retries=out.provider_retries,
                    provider_status=getattr(out, "provider_status", None),
                ).as_dict())

            if model != model_in_use:
                # Stick with it for the rest of the turn. Flapping back to a
                # model that just failed would spend the budget rediscovering it.
                logger.info("run %s: continuing on %s", run_id, model)
                model_in_use = model
                turn_usage.used_model(model)
            return out
        assert last is not None
        raise last

    # ── the recovery ladder ──────────────────────────────────────────────────
    #
    # **"The model produced no reply" was the largest reliability signal in the
    # system, and it was four different failures under one error string.**
    # Traced run by run across ninety days: 80 of 94 were `provider = 'echo'`,
    # our own e2e scripts whose scripted provider had run out; of the 14 real
    # ones, 5 had already answered the guest through `send_message`. The nine
    # that remain are a malformed tool call (3), a blank candidate (3), and a
    # turn whose tools did real work and whose model then said nothing (3).
    #
    # Each wants a different answer, which is why "no reply → handoff" was the
    # wrong shape. This climbs: ask again, ask a different model, say what the
    # tools established, and only then fetch a person.
    #
    # **Retrying the model is side-effect safe by construction.** A retry
    # re-issues the *model* call; the tool results already in `messages` are not
    # re-executed. If the retried model asks for a tool it already called,
    # `called_writes` refuses it for the rest of the turn, and across turns the
    # message-scoped idempotency key in `registry.execute` refuses it again. See
    # tests/test_silent_recovery.py, which asserts both.
    recovery_log: list[dict] = []

    async def _answering_step(*, messages: list[dict], tools: list[dict]) -> ModelStep:
        """One model step, and the ladder behind it if nothing usable comes back."""
        nonlocal model_in_use

        step = await _step(messages=messages, tools=tools)
        turn_usage.add_tokens(step.tokens_in, step.tokens_out, step.tokens_thought)
        turn_usage.used_model(model_in_use)

        failure = recovery.classify(step)
        if failure == recovery.USABLE:
            return step

        # **Recovery costs time, and out of time is the one state where it must
        # not be spent.**
        #
        # The budget path below already tells the model plainly that the guest
        # is waiting and gives it one last chance to say something true with
        # what it has. If *that* comes back empty, climbing a ladder on top of
        # it makes a slow turn into a timed-out one — which is the failure the
        # budget exists to prevent, arrived at by the machinery meant to prevent
        # a different one. Past the deadline the honest move is the handoff.
        if time.monotonic() > deadline:
            logger.info("run %s: %s, but the budget is spent — not recovering",
                        run_id, failure)
            recovery_log.append(
                recovery.Attempt(failure, model_in_use, "out of budget").as_dict())
            return step

        if failure not in recovery.RETRYABLE:
            # A filtered completion is not a bad sample. The same prompt filtered
            # once will filter again, and asking a second time only spends the
            # guest's patience on the same answer.
            logger.warning("run %s: %s — not retrying", run_id, failure)
            recovery_log.append(
                recovery.Attempt(failure, model_in_use, "not retryable").as_dict())
            return step

        # Rung one: the same model, once. What failed was a sample rather than a
        # system, and asking again is the cheapest thing on this ladder.
        #
        # **Immediately, and that is deliberate rather than an omission.** §2
        # asks for exponential backoff with jitter, and it belongs one layer
        # down: `vertex.py` already spaces its retries and rotates regions when
        # the *transport* fails, which is the case backoff is for — a service
        # under load, where waiting is what helps. This is not that case. Vertex
        # returned 200, a candidate and a finish reason; the service is healthy
        # and the sample was bad. Sleeping here would add latency to a guest's
        # turn and change nothing about the next draw.
        logger.info("run %s: %s on %s — asking again", run_id, failure, model_in_use)
        recovery_log.append(recovery.Attempt(failure, model_in_use, "retry").as_dict())
        retried = await _step(messages=messages, tools=tools)
        turn_usage.add_tokens(retried.tokens_in, retried.tokens_out, retried.tokens_thought)
        if recovery.classify(retried) == recovery.USABLE:
            return retried

        # Rung two: a different model, and exactly one of them.
        #
        # **The most established, not the adjacent one.** `choice.fallbacks` is
        # the degradation ladder — 3.6-flash → 3.5-flash → 2.5-flash — and it is
        # walked in order by `_step` when the *transport* fails, because there
        # the question is "which region will answer". Here the question is
        # different: this model has now returned nothing twice, and what is
        # wanted is the model least likely to do the same. That is the end of the
        # list, the one that served production for a year, not the sibling that
        # shares its generation.
        #
        # One, because each rung costs a full model call carrying the whole
        # catalogue. Walking the list would put five model calls between a blank
        # answer and a person, and the guest is already waiting.
        #
        # Not for a business failure, an authorisation failure or a tool
        # rejection: none of those reach here. Only a model that would not
        # answer gets a second model.
        proven = next((m for m in reversed(choice.fallbacks) if m != model_in_use), None)
        if proven and time.monotonic() <= deadline:
            logger.warning("run %s: %s twice on %s — falling back to %s",
                           run_id, failure, model_in_use, proven)
            recovery_log.append(recovery.Attempt(failure, proven, "fallback").as_dict())
            model_in_use = proven
            turn_usage.used_model(proven)
            try:
                fell_back = await llm.step(
                    model=proven, messages=messages, tools=tools, params=params
                )
            except ProviderError as exc:
                logger.warning("run %s: fallback %s also failed (%s)",
                               run_id, proven, exc.reason)
            else:
                turn_usage.add_tokens(fell_back.tokens_in, fell_back.tokens_out,
                                      fell_back.tokens_thought)
                retried = fell_back
                if recovery.classify(fell_back) == recovery.USABLE:
                    return fell_back
        elif proven:
            logger.warning("run %s: out of budget before falling back", run_id)

        return retried

    try:
        for _turn in range(settings.agent_max_tool_turns):
            step: ModelStep = await _answering_step(messages=messages, tools=tools)

            if not step.wants_tools:
                reply = (step.reply or "").strip() or None
                break

            if time.monotonic() > deadline:
                # Out of time with tools still pending. Rather than run them and
                # keep the guest waiting, the model is told plainly and given
                # one last step to say something true with what it has.
                out_of_time = True
                logger.warning(
                    "run %s spent its %.0fs budget with tools outstanding",
                    run_id, choice.budget,
                )
                messages.append(step.raw or {"role": "assistant", "content": None})
                for call in step.tool_calls:
                    messages.append(llm.tool_result_message(call, json.dumps({
                        "refused": True,
                        # **This used to end "…or say you are checking and call
                        # request_human_handoff", and the step below is given no
                        # tools at all.**
                        #
                        # The model did what it was told, as far as it could. On
                        # a live eval turn it answered a ten-room enquiry with
                        # *"I am checking the group rates with our team right
                        # now. One of our team members will get back to you
                        # shortly"* — and called nothing, because it had nothing
                        # to call. `handoff` stayed false, the conversation
                        # stayed with the AI, and the guest was left waiting for
                        # a person nobody had asked for.
                        #
                        # An instruction that names an action the model cannot
                        # take is worse than no instruction: it produces the
                        # *words* of the action without the action. So the
                        # sentence no longer mentions a tool, and the runtime
                        # takes responsibility for the handoff below — which is
                        # where it belonged, because running out of time is the
                        # runtime's fact about the turn, not the model's.
                        "reason": (
                            "This turn has taken too long and the guest is "
                            "waiting. You have no tools left. Answer with what "
                            "you already have. If that is not enough to answer "
                            "them, say plainly that you are checking — do not "
                            "promise a time, and do not say the booking or the "
                            "price is settled."
                        ),
                    })))
                final = await _answering_step(messages=messages, tools=[])
                reply = (final.reply or "").strip() or None

                # **Out of time with nothing to show for it goes to a person.**
                #
                # Deterministic, because the condition is a fact the runtime
                # owns: did any tool on this turn actually succeed? If some did,
                # the model has real results to summarise and its answer stands.
                # If none did, whatever it just wrote is a holding sentence — and
                # a holding sentence with nobody behind it is the one outcome a
                # hotel cannot explain.
                if not any(o.ok for o in outcomes):
                    logger.warning(
                        "run %s: out of time with no successful tool — handing over",
                        run_id,
                    )
                    handoff = True
                break

            messages.append(step.raw or {"role": "assistant", "content": None})

            for call in step.tool_calls:
                # **The same call twice is not two calls.**
                #
                # A model that has been handed a result it did not expect will
                # often ask again with identical arguments, and for a read that
                # is merely wasteful. For a mutation it is a second charge on a
                # bill, a second ticket, a second attempt at a check-in — and
                # the tool-turn ceiling is six, so there is room for it to
                # happen more than once inside one turn.
                #
                # Answered from the first result rather than refused: the model
                # asked a question it already has the answer to, and telling it
                # so plainly is what stops the loop. The repeat is not recorded
                # as an action because no action occurred.
                spec_ahead = registry.REGISTRY.get(call.name)
                mutates = bool(spec_ahead and spec_ahead.mutating)
                fingerprint = (call.name, json.dumps(call.arguments, sort_keys=True,
                                                     default=str))

                # **Reads and writes are deduplicated on opposite rules, and
                # getting that backwards breaks the thing above it.**
                #
                # A repeated *write* is never run again for the rest of the
                # turn. That is the whole safety property: two identical
                # `post_folio_charge` calls are one charge.
                #
                # A repeated *read* is only suppressed while nothing has
                # changed. The first version of this suppressed it
                # unconditionally, which quietly disabled verification — the
                # correct shape for a mutation is `get_folio → post_charge →
                # get_folio`, and the second read has the same arguments as the
                # first by definition. Deduplicating it would have handed the
                # model the pre-charge folio and called it proof.
                repeated = (fingerprint in called_writes
                            or (not mutates and fingerprint in called_reads))
                if repeated:
                    logger.info("run %s: %s repeated with identical arguments",
                                run_id, call.name)
                    messages.append(llm.tool_result_message(call, json.dumps({
                        "repeated": True,
                        "reason": (
                            "You already called this with exactly these "
                            "arguments on this turn and it was NOT run again. "
                            "Use the result you were given above."
                        ) if not mutates else (
                            "You already did this on this turn and it was NOT "
                            "done twice. It is done. Do not call it again — "
                            "answer with the result you were given above."
                        ),
                    })))
                    continue

                outcome = await registry.execute(call.name, call.arguments, ctx)
                seq += 1
                outcomes.append(outcome)
                if not outcome.ok:
                    tool_failures += 1

                if mutates:
                    if outcome.ok:
                        # Nailed down for the turn, and the reads reopened: what
                        # anything read before this is now potentially stale,
                        # which is exactly what a verification step exists to
                        # catch.
                        called_writes.add(fingerprint)
                        called_reads.clear()
                else:
                    called_reads.add(fingerprint)

                spec = registry.REGISTRY.get(call.name)
                if spec:
                    turn_usage.count(spec.usage_type)

                action_id = await run_db(
                    _write_action, ctx, run_id, seq, outcome,
                    "model", _intent_for(requirements, outcome.tool),
                )
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

        # ── the required action, when the model did not make it ─────────────
        #
        # **This is the reliability fix.** Everything above is the model's turn
        # and it keeps it: on the runs where it calls `get_group_rates` for five
        # rooms, nothing here does anything at all. What changed is what happens
        # on the runs where it does not — which was, until now, that a hotel
        # lost a ten-person enquiry to a handoff at eleven at night.
        #
        # Three conditions, all of them about not talking over something that
        # already decided:
        #
        #   * not after a handoff — the conversation has gone to a person and
        #     an extra tool call is the AI carrying on past its own exit;
        #   * not out of time — the guest is already waiting too long, and the
        #     honest answer to that is the one the budget path just gave;
        #   * not a tool the model already tried — a `negotiate_rate` that
        #     failed is a system problem, and running it again inside the same
        #     turn takes a second hold on the same rooms.
        #
        # Nothing here bypasses a gate. `registry.execute` is the same door:
        # permission, autonomy, argument validation and the property's own
        # rules all run, so an agent at level 2 gets its negotiation queued for
        # approval exactly as it would have had the model asked. See §20 — the
        # deterministic layer decides *that* a business tool runs, never what it
        # is allowed to answer.
        if (requirements and not handoff and not out_of_time
                and required_actions.enforcement_enabled()):
            attempted = {o.tool for o in outcomes}
            pending = [
                r for r in required_actions.unmet(requirements, outcomes)
                if r.forceable and r.tool not in attempted and r.tool in ctx.tool_grants
            ]
            for requirement in pending[:required_actions.MAX_FORCED]:
                logger.info("run %s forcing %s for %s (%s)", run_id, requirement.tool,
                            requirement.intent, requirement.detail)
                outcome = await registry.execute(
                    requirement.tool, dict(requirement.arguments or {}), ctx
                )
                # Stamped on the outcome, not only on the row that records it.
                # `session.patch_from` reads the outcomes and has to know which
                # of them are the guest speaking and which are the application
                # repeating itself.
                outcome.decision_source = "application"
                seq += 1
                outcomes.append(outcome)
                forced.append(requirement.tool)

                # **A forced stop is still a stop.**
                #
                # `handoff` was set only inside the model's own tool loop, so a
                # handoff the *application* ran left the flag false: the tool
                # moved `conversations.handler` to 'human_requested', and then
                # `_ensure_human` never ran, the run recorded no handoff, and
                # `messaging/pipeline` went on to deliver the reply and look for
                # the next message as though nothing had happened. One line,
                # and it is the difference between a guest who asked for a
                # person reaching one and a guest who asked for a person being
                # answered by the AI on the very turn it agreed to stop.
                if outcome.tool == "request_human_handoff" and outcome.ok:
                    handoff = True

                spec = registry.REGISTRY.get(requirement.tool)
                if spec:
                    turn_usage.count(spec.usage_type)
                action_id = await run_db(
                    _write_action, ctx, run_id, seq, outcome,
                    outcome.decision_source, requirement.intent,
                )
                await run_db(link_result, outcome, action_id)
                if spec and action_id:
                    action_ids.setdefault(spec.usage_type, []).append(action_id)

            if forced:
                # The result goes back to the model to *say*, which is the whole
                # division of labour: the application decided the price question
                # had to be put to the pricing engine, and the model writes the
                # sentence — in this guest's language, in this hotel's voice,
                # and with whatever else the turn owed them.
                #
                # A plain user turn rather than a tool result, because there was
                # no tool *call* in the assistant's history for it to answer and
                # every provider rejects an orphaned function response.
                #
                # `tools=[]`: this step exists to produce prose. Letting it open
                # another round of calls after the budget has already been spent
                # is how a forced action turns a slow turn into a timed-out one.
                messages.append({"role": "user", "content": json.dumps({
                    "system_note": (
                        "You did not call the tool this message required, so the "
                        "system called it for you. These are its real results. "
                        "Answer the guest with them now — use only the figures "
                        "below, follow each result's own instruction, and do not "
                        "mention that any of this happened."
                    ),
                    "results": [
                        {
                            "tool": o.tool,
                            "ok": o.ok,
                            "output": o.output if o.ok else None,
                            "refused": None if o.ok else o.model_message,
                        }
                        for o in outcomes[-len(forced):]
                    ],
                }, default=str)})
                final = await _answering_step(messages=messages, tools=[])
                spoken_now = (final.reply or "").strip()
                if spoken_now:
                    reply = spoken_now
                elif reply is None:
                    # Forced the tool, and the model still said nothing. The
                    # ordinary silent-turn path below picks this up and hands
                    # over — a tool result nobody communicated is not an answer.
                    logger.warning("run %s: forced %s and got no reply", run_id, forced)

        # The alphabet the guest asked to be written to in.
        #
        # A guest asked for Hinglish and got "Aapke लिए best rate ₹4,500
        # rahega" — one Devanagari word in an otherwise perfectly romanised
        # sentence, a few times in a hundred turns. The prompt already forbids
        # it, which is the evidence that a fourth prompt line was not the fix.
        # Which alphabet a string is in is decidable by looking at it.
        #
        # Before the guardrail, deliberately: the guardrail should check the
        # text the guest will actually receive. Transliteration cannot move a
        # figure — digits, `₹` and Latin runs are outside every Devanagari run
        # by construction — so the two orderings are equivalent for money and
        # only this one is equivalent for the reply.
        if reply and ctx.guest_scoped:
            reply, romanised = script.normalise(
                reply, preference=ctx.language, guest_message=message,
            )
            if romanised:
                language_slips = romanised
                logger.info("run %s: romanised %s for a Hinglish guest",
                            run_id, romanised[:5])

        # Everything the agent has said, checked against everything it did.
        #
        # Last thing inside the try, so a fabricated reply is caught whatever
        # produced it, and before any caller can deliver it. See
        # services/ai/guardrails — this is where an invented payment link stops
        # being a thing a guest can tap.
        # **Every reply, including the ones that end in a handoff.**
        #
        # This read `if reply and not handoff`, and the exclusion had no stated
        # reason. It was harmless while a handoff reply came from
        # `prompts.handoff_reply` — a fixed sentence with no figures in it — and
        # stopped being harmless the moment a handoff could follow the model
        # speaking: the handoff tool's own instruction is "tell the guest a
        # member of the team is taking over", the model writes that sentence
        # itself, and on a turn that had already priced something it can write a
        # price into it. A rupee figure in the last thing a guest reads before a
        # human arrives is exactly the figure that gets honoured.
        #
        # Failing here is benign in the way that matters: the reply is replaced
        # with `handoff_reply`, `handoff` is already true, and the conversation
        # was going to a person regardless. The check can only remove a number
        # nothing accounts for; it cannot stop the handoff.
        if reply:
            verdict = guardrails.check_reply(
                reply,
                tool_outputs=outcomes,
                trusted_hosts=await _trusted_hosts(ctx),
                history=history or [],
            )
            if not verdict.ok:
                logger.error(
                    "run %s refused by guardrails: %s | reply=%r",
                    run_id, verdict.reason, reply[:300],
                )
                # **Kept, because the log line is not a record.**
                #
                # Thirty guardrail blocks in production and not one of them
                # retains what was actually refused — the row holds the phrase
                # that matched and nothing else. So the largest failure class on
                # product traffic (35%) cannot be diagnosed after the fact, and
                # §17's question — *why did StayOS say this?* — has no answer
                # without a log search against a run id nobody has.
                #
                # It matters most where the rule is subtle. Several blocks
                # matched the phrase 'booking confirm', which the mood rule now
                # correctly *allows* in "booking confirm karne ke liye pay
                # kijiye" and correctly refuses in "booking confirm ho gayi".
                # Which of those each run was is unknowable from the row, so the
                # effect of that fix on real traffic cannot be measured.
                #
                # Internal only: this never reaches a guest, and the reply is
                # our own text about their booking, already stored in
                # `conversation_messages` whenever it is delivered. A refused
                # one is the only kind that leaves no trace at all.
                ctx.turn.telemetry.setdefault("guardrail", []).append({
                    "reason": verdict.reason[:200],
                    "refused_reply": reply[:400],
                })
                reply = prompts.handoff_reply(ctx)
                handoff = True
                error = f"guardrail: {verdict.reason}"

        if out_of_time and reply is None:
            # Nothing usable came back inside the budget. A person, rather than
            # silence — which is the one outcome a hotel cannot explain.
            handoff = True
            reply = prompts.handoff_reply(ctx)
            error = f"turn exceeded its {choice.budget:.0f}s budget"

        if handoff:
            status = "escalated"
        if reply is None and not handoff:
            status = "failed"
            # With the one fact that tells the two causes apart.
            #
            # This class is 86 runs in thirty days and splits cleanly in the
            # data: on the conversation tier it is 76 runs averaging one output
            # token in 347ms — a blank candidate, back too fast to have been
            # generated — and on the reasoning tier it is 5 runs averaging 32
            # seconds of thinking and no reply. Same message, opposite causes,
            # and the error string said neither. `finishReason` is on the
            # response and was being read and dropped.
            # getattr, because a diagnostic must not be the thing that breaks
            # the turn it is describing: any provider may hand back a step
            # object without these.
            finish = getattr(step, "finish_reason", None)
            thought = getattr(step, "tokens_thought", 0) or 0
            detail = ", ".join(
                part for part in (
                    f"finishReason={finish}" if finish else "",
                    f"{thought:,} thinking tokens" if thought else "",
                ) if part
            )
            error = "the model produced no reply" + (f" ({detail})" if detail else "")

            # **The guest was left in silence, 78 times in thirty days.**
            #
            # Every other failure path below produces `handoff_reply` and sets
            # `handoff`, so the guest is told something true and a person picks
            # it up. This one — the model succeeded and returned nothing, twice,
            # since the empty-completion retry above already ran — set neither.
            # `pipeline` then skipped delivery on `if result.reply` and skipped
            # `_ensure_human` on `if handoff`, and the conversation sat with the
            # AI still nominally on it and nothing sent.
            #
            # Found by counting `agent_runs.error` rather than by reading the
            # code: 'the model produced no reply' was the second most common
            # failure in production and had no guest-visible symptom anybody had
            # connected to it.
            #
            # A tool that already spoke is not silence. `send_message` returning
            # ok means the guest has their answer and the missing prose is the
            # model correctly declining to say the same thing twice.
            spoke = any(o.tool == "send_message" and o.ok for o in outcomes)
            if not spoke:
                # **Rung three: say what the tools established.**
                #
                # The model has been asked twice and asked on a second model,
                # and it still has nothing to say — but the turn is not empty.
                # Three runs in ninety days look like this: a hotelier asked how
                # last month went, `compare_with_previous_period` and
                # `create_revenue_recommendation` both ran, and the answer to
                # their question was computed and then never spoken.
                #
                # Built only from display strings a tool returned, and put
                # through the same guardrail as any model reply below — so a
                # sentence carrying a figure no tool result contains is refused
                # by the rule that refuses an invented one. It cannot fabricate
                # business truth by construction rather than by review.
                salvaged = recovery.deterministic_reply(
                    outcomes, staff=not ctx.guest_scoped
                )
                # **Checked here, and not by the block above.**
                #
                # The reply guardrail runs earlier in this function, on the
                # model's own prose, and this rung produces its reply *after*
                # that — so a salvaged sentence would otherwise be the one thing
                # in the system that reaches a guest unchecked. Running it
                # explicitly keeps the guarantee local and visible rather than
                # dependent on the order of two blocks a hundred lines apart.
                #
                # It should never fail: `deterministic_reply` only quotes
                # display strings the tools returned. If it ever does, that is
                # the check earning its place, and the turn takes the handoff it
                # would have taken anyway.
                if salvaged:
                    checked = guardrails.check_reply(
                        salvaged,
                        tool_outputs=outcomes,
                        trusted_hosts=await _trusted_hosts(ctx),
                        history=history or [],
                    )
                    if not checked.ok:
                        logger.error(
                            "run %s: the salvaged reply did not pass the guardrail "
                            "(%s) — handing over instead", run_id, checked.reason,
                        )
                        salvaged = None
                if salvaged:
                    logger.info("run %s: model said nothing; answering from %d "
                                "verified tool results", run_id, len(outcomes))
                    recovery_log.append(recovery.Attempt(
                        "no_prose_after_tools", model_in_use, "deterministic").as_dict())
                    reply = salvaged
                    status = "succeeded"
                    error = ("the model produced no reply; answered from verified "
                             "tool results" + (f" ({detail})" if detail else ""))
                else:
                    recovery_log.append(recovery.Attempt(
                        "no_reply", model_in_use, "handoff").as_dict())
                    reply = prompts.handoff_reply(ctx)
                    handoff = True
                    status = "escalated"
            else:
                # **And it is not a failure either.**
                #
                # `status` was set to 'failed' above before anything had looked
                # at whether the guest was answered, and the `spoke` branch only
                # ever moved it further down, to 'escalated'. So a turn where the
                # model replied through `send_message` and then correctly
                # declined to repeat itself was recorded as a failure with the
                # error "the model produced no reply".
                #
                # Nothing behaved badly — the guest had their answer and the
                # conversation stayed with the AI. What was wrong was the
                # record, and the record is what every comparison is made of.
                #
                # It is not evenly wrong, which is why it matters. A model that
                # answers by calling the tool books these; a model that answers
                # in prose never does. Production on 2026-08-24 shows exactly
                # that: 3.x replies through `send_message`, 2.5 returns prose,
                # and any dashboard counting status='failed' would have read the
                # difference in *style* as a difference in reliability. That is
                # the same bias that produced a model regression which was not
                # real, moved from the fixture into the metric.
                status = "succeeded"
                error = (
                    "answered through send_message with no closing prose"
                    + (f" ({detail})" if detail else "")
                )

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

    # What the turn owed and whether it paid it, on the run row.
    #
    # Written whatever the outcome, including the failures: a turn that crashed
    # before it could negotiate is exactly the turn somebody investigating a
    # lost sale needs to find, and a summary written only on success would hide
    # it. §21 — every question about a turn answerable from the two tables.
    # ── a promise of a person has to be true ─────────────────────────────────
    #
    # **§12's invariant, enforced rather than intended.** Two ways it was
    # breakable, and both had happened.
    #
    # The model can write the promise itself: a live evaluation turn produced
    # *"one of our team members will get back to you shortly"* having called
    # nothing, with `handoff` false — the words of a handoff without the
    # handoff. So a reply that promises somebody *becomes* a handoff, rather
    # than being refused. A guest told a person is coming should get one;
    # taking the sentence away and leaving them with nothing is the worse
    # repair.
    if reply and not handoff and recovery.promises_a_person(reply):
        logger.warning("run %s: the reply promised a person and nothing had "
                       "handed over — creating the handoff", run_id)
        handoff = True
        if status == "succeeded":
            status = "escalated"
        recovery_log.append(
            recovery.Attempt("promised_a_person", model_in_use, "handoff").as_dict())

    # **A turn that hands off must not leave a promise standing.**
    #
    # `promises_a_person` above covers the reply that says a human is coming.
    # This is the same failure one step over: a reply that undertakes to send a
    # *thing* — a link, a price, a confirmation — on a turn that then hands the
    # conversation to somebody. The guest is left waiting for something nobody
    # has undertaken to send.
    #
    # Found on turn 8 of the first-guest journey: "I am checking on the payment
    # link for you right now. I will share it with you as soon as it is ready."
    # The turn escalated, the promise stood, and the guest's next three messages
    # got the handoff sentence — so from their side a link was coming and then
    # the conversation went quiet.
    #
    # Only on a handing-off turn, which is what keeps this from touching the
    # ordinary case: a promise the agent goes on to keep in the same turn never
    # reaches here.
    if handoff and reply and recovery.promises_an_action(reply):
        logger.warning("run %s: the reply promised an action and the turn is "
                       "handing over — replacing it", run_id)
        reply = prompts.handoff_reply(ctx)
        recovery_log.append(
            recovery.Attempt("promised_an_action", model_in_use, "handoff").as_dict())

    if handoff and ctx.conversation_id:
        # And the other way: the sentence is composed before this runs, and this
        # used to swallow its own failure — a database blip left the guest
        # promised and the conversation still on `ai`, where the sweep never
        # looks. If it cannot be made true, the promise is withdrawn.
        marked = await run_db(_ensure_human, ctx)
        if not marked and reply and recovery.promises_a_person(reply):
            logger.error("run %s: could not create the handoff a reply promised "
                         "— withdrawing the promise", run_id)
            reply = prompts.no_handoff_reply(ctx)
            error = (error or "") + " | handoff could not be recorded"

    summary_of_requirements = required_actions.summarise(requirements, outcomes, forced)
    if language_slips:
        # Folded into the same jsonb rather than given a column of its own: it
        # is the turn's story, it is absent on virtually every turn, and a
        # column that is null 99% of the time is a column nobody queries.
        summary_of_requirements["language_slips"] = language_slips[:10]
    # §10 asks for `tools_available` per turn. One integer rather than the list:
    # the names are derivable from the agent's grants and the task, and what
    # nobody could answer before is "how big was the menu on this turn" — which
    # is the number the greeting narrowing moves and the number that explains a
    # turn's input tokens.
    summary_of_requirements["tools_available"] = len(tools)
    if ctx.turn.telemetry:
        # How the turn's business facts were arrived at — which negotiation rule
        # scope applied, and whether any of it was a fallback because a read
        # failed. §4: a safe fallback has to be observable, and this is the
        # column somebody queries to see one.
        summary_of_requirements["provenance"] = ctx.turn.telemetry
    if recovery_log:
        # Every rung climbed this turn, and what came of it. §10 asks for
        # `attempt`, `fallback_used`, `failure_class` and `recovery_method` to
        # be answerable per turn; this is all four, in the same jsonb the rest
        # of the turn's story lives in. Absent on the turns that worked, which
        # is nearly all of them.
        summary_of_requirements["recovery"] = recovery_log[:8]
    if ctx.turn.handoff_rejections:
        # Every handoff the model asked for this turn and what the arbiter made
        # of it — accepted and rejected alike. §21: a handoff that was turned
        # down is exactly as much of an event as one that went through, and a
        # record that only kept the rejections would make the layer look busier
        # than it is. This is also the only way to answer "how often does the
        # arbiter fire", which is the number that decides whether it is helping.
        summary_of_requirements["handoff_judgements"] = ctx.turn.handoff_rejections[:6]

    # **The turn's model record, on every turn rather than on the odd ones.**
    #
    # §17 asks that a turn answer, without a log search: which model was
    # configured, which one served it, how many fallbacks fired and why, how
    # many attempts it took, how long they cost, and what class of provider
    # error caused each. The log line below carries most of that and only on
    # turns that went wrong, which makes the denominator unknowable — a
    # fallback rate needs the turns that did *not* fall back as much as the
    # ones that did.
    #
    # Written unconditionally for that reason. It is six small fields on a
    # jsonb that already exists, and it is what makes PRIMARY_ONLY /
    # FALLBACK_USED / FALLBACK_ONLY computable from the rows rather than
    # inferred from an absence.
    summary_of_requirements["model"] = {
        "configured": choice.model,
        "served_by": list(turn_usage.models),
        "finished_on": model_in_use,
        "fallback_count": fallback_count,
        # Provider-level attempts, each with its own error class and latency.
        # Trimmed, because a pathological turn should not write an unbounded
        # array into a row every dashboard reads.
        "attempts": model_attempts[:8],
        "fallback_reasons": sorted({
            a["failure"] for a in model_attempts if a.get("failure")}),
    }

    # **Which model actually answered, not which one was chosen.**
    #
    # The run row records the router's decision, and once a turn can degrade
    # mid-flight those are different facts. A turn that opened on 3.6-flash and
    # finished on 2.5-flash looked, in the data, exactly like a turn that ran on
    # 3.6-flash throughout — so a fallback firing on every request would have
    # been invisible while the invoice and the latency both moved.
    #
    # Logged rather than added to the run row's columns: `model` there is the
    # routing decision and changing its meaning would rewrite the history of
    # every row already written. Structured, so it aggregates.
    if fallback_count or tool_failures or model_in_use != choice.model:
        logger.info(
            "[agent-run] %s",
            {
                "run_id": run_id,
                "task": task,
                "tier": choice.tier,
                "model_selected": choice.model,
                "model_served": model_in_use,
                "fallbacks": fallback_count,
                "tool_calls": seq,
                "tool_failures": tool_failures,
                "latency_ms": latency,
                "status": status,
            },
        )

    await run_db(_close_run, run_id, {
        "status": status,
        "tokens_in": turn_usage.tokens_in,
        "tokens_out": turn_usage.tokens_out,
        "latency_ms": latency,
        "error": error,
        "intent": (requirements[0].intent if requirements else None),
        "required_actions": summary_of_requirements,
        "required_action_status": required_actions.status_of(summary_of_requirements),
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
            # What the hotel is billed by. `ctx.channel` is the guest's channel
            # for a guest-facing run and 'internal' for a staff one, and
            # `usage_type_for_channel` returns nothing for the second — a
            # receptionist's question is not a guest conversation.
            channel=ctx.channel,
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not record usage for run %s", run_id, exc_info=True)

    # A handoff always leaves the conversation with a human, even when the
    # handoff came from a crash rather than from the tool. The rule is that an
    # unattended conversation is never left believing an agent is still on it.
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
        requirements=summary_of_requirements,
        required_action_status=required_actions.status_of(summary_of_requirements),
        served_by=list(turn_usage.models),
    )


def _ensure_human(ctx: AgentContext) -> bool:
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
        return True
    except Exception:  # noqa: BLE001
        # **Escalated from a warning, because the guest has already been told.**
        #
        # The handoff sentence is composed before this runs, so a failure here
        # leaves a guest who has been promised a person on a conversation the
        # sweep will never pick up — `waiting.reason_waiting` reads the handler.
        # The caller repairs the promise; this makes sure somebody is paged
        # about why it had to.
        logger.error("could not mark conversation %s for a human — a guest may "
                     "have been told somebody is coming", ctx.conversation_id,
                     exc_info=True)
        return False


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


# ── whose links an agent may send ────────────────────────────────────────────

_TRUSTED_HOST_CACHE: dict[str, set[str]] = {}


def _read_trusted_hosts(property_id: str) -> set[str]:
    """
    The domains this hotel is allowed to point a guest at.

    Three sources, all of them ours or the hotel's own: the app every payment
    and check-in link lives on, whatever custom domain the hotel has connected
    to its website, and the website it told Google about. Nothing a model or a
    guest can influence at request time is in here — that is the point.
    """
    from urllib.parse import urlparse

    hosts: set[str] = set()

    for base in (settings.pms_app_url, getattr(settings, "site_url", None)):
        host = (urlparse(str(base or "")).hostname or "").lower()
        if host:
            hosts.add(host)

    db = get_db()
    site = (
        db.table("hotel_websites").select("domain")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    for row in site:
        host = (urlparse(f"https://{row.get('domain')}").hostname or "").lower()
        if host:
            hosts.add(host)

    try:
        google = (
            db.table("google_connections").select("website_uri")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        for row in google:
            host = (urlparse(str(row.get("website_uri") or "")).hostname or "").lower()
            if host:
                hosts.add(host)
    except Exception:  # noqa: BLE001
        # A deployment without the Google table is not a reason to refuse every
        # link a hotel sends.
        pass

    return hosts


async def _trusted_hosts(ctx: AgentContext) -> set[str]:
    cached = _TRUSTED_HOST_CACHE.get(ctx.property_id)
    if cached is not None:
        return cached
    hosts = await run_db(_read_trusted_hosts, ctx.property_id)
    _TRUSTED_HOST_CACHE[ctx.property_id] = hosts
    return hosts
