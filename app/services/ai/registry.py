"""
The tool registry, and the one path through it.

Every tool call in the system goes through `execute()`. Nothing calls a tool
function directly — not the runtime, not the API, not a test — because
`execute()` is where permission, autonomy, approval, validation and the audit
record all happen, and a second entrance would be a way to skip all five.

    execute()
      ├─ resolve the tool          unknown name → refused, told to the model
      ├─ permissions.check()       grant + scope
      ├─ validate arguments        pydantic, the same schema the model was shown
      ├─ precheck()                the property's own rules, before anything queues
      ├─ autonomy.decide()         execute · approve · refuse
      ├─ revalidate()              approval path only: is it still true?
      ├─ run the function          which does the work and nothing else
      └─ return a ToolOutcome      which the caller writes to agent_actions

Tool bodies contain no security logic at all. That is the point: a tool author
cannot forget a check that lives somewhere they never edit.
"""
import inspect
import logging
import time
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from app.services.ai import autonomy, permissions, toolsets
from app.services.ai.context import AgentContext
from app.services.ai.errors import (
    AgentError,
    ApprovalRequired,
    ToolInputInvalid,
    ToolNotFound,
    ToolUnavailable,
)
from app.services.ai.schemas import ToolOutcome, ToolSpec

logger = logging.getLogger(__name__)

REGISTRY: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    permission: str | None,
    autonomy_floor: int,
    mutating: bool,
    usage_type: str,
    proposal: bool = False,
    result_type: str = "none",
    creates_result: bool = False,
    always_requires_approval: bool = False,
    scoped_to_conversation: bool = False,
    when_deferred: str | None = None,
    precheck: Callable | None = None,
    revalidate: Callable | None = None,
) -> Callable:
    """
    Declare a tool.

    Every field is required rather than defaulted, `usage_type` included. A tool
    that ships without a metering dimension is one somebody has to retro-fit
    later against a rate card that already exists, and the retro-fit is where
    the billing gap comes from.
    """

    def decorator(fn):
        if name in REGISTRY:
            raise RuntimeError(f"tool {name} is already registered")
        REGISTRY[name] = ToolSpec(
            name=name,
            description=description,
            input_model=input_model,
            permission=permission,
            autonomy_floor=autonomy_floor,
            mutating=mutating,
            proposal=proposal,
            usage_type=usage_type,
            fn=fn,
            result_type=result_type,
            creates_result=creates_result,
            always_requires_approval=always_requires_approval,
            scoped_to_conversation=scoped_to_conversation,
            when_deferred=when_deferred,
            precheck=precheck,
            revalidate=revalidate,
        )
        return fn

    return decorator


def get(name: str) -> ToolSpec:
    spec = REGISTRY.get(name)
    if spec is None:
        raise ToolNotFound(f"no tool named {name!r}")
    return spec


def available_for(ctx: AgentContext) -> list[ToolSpec]:
    """
    The tools this agent may call, in registration order.

    This is also what the model is shown. An agent is not told about a tool it
    has no grant for — describing a capability and then refusing it wastes a
    turn and teaches the model to keep trying.
    """
    return [
        spec for nm, spec in REGISTRY.items()
        if nm in ctx.tool_grants and ctx.tool_grants[nm].get("enabled", True)
    ]


def describe_for_provider(ctx: AgentContext, task: str | None = None) -> list[dict]:
    """
    The tool list in the shape a chat-completions API expects.

    `task` narrows *what is described*, never what is permitted — see
    services/ai/toolsets.py. Left out, everything granted is described, which
    is what every caller did before the narrowing existed and what the
    unclassifiable case still gets.
    """
    specs = available_for(ctx)
    if task:
        shown = toolsets.for_task(task, frozenset(s.name for s in specs))
        specs = [s for s in specs if s.name in shown]
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.json_schema(),
            },
        }
        for spec in specs
    ]


async def execute(
    name: str,
    arguments: dict[str, Any],
    ctx: AgentContext,
    *,
    approved: bool = False,
) -> ToolOutcome:
    """
    The only way to run a tool. Never raises for an expected refusal — a refusal
    is an outcome the model must see and answer, not an exception that ends the
    turn.

    `approved=True` means a human with the 'agents' permission has stood in for
    the autonomy level on this specific action. It skips **only** the autonomy
    gate. Permission, grant, argument validation, the property's own business
    rules and the tool's revalidation all still run — a manager can lend their
    judgement, not their access, and cannot approve a tool the agent was never
    granted or a price the property never authorised.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    def refusal(exc: AgentError, tool_name: str = name) -> ToolOutcome:
        return ToolOutcome(
            tool=tool_name,
            status=exc.action_status,
            input=arguments,
            refusal_reason=exc.reason,
            model_message=exc.reason,
            guest_message=exc.guest_message,
            latency_ms=elapsed(),
            error=exc.reason if exc.action_status == "failed" else None,
        )

    try:
        spec = get(name)
    except AgentError as exc:
        return refusal(exc)

    try:
        permissions.check(spec, ctx)
    except AgentError as exc:
        return refusal(exc)

    # Validated against the same model the provider was shown, so an argument
    # the model hallucinated fails here rather than three layers down as a 500.
    try:
        args = spec.input_model(**(arguments or {}))
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:4]
        )
        return refusal(ToolInputInvalid(f"{spec.name} arguments rejected — {detail}"))

    # Business validation, before the autonomy gate and therefore before
    # `approved` can matter. A discount above what the property authorises is
    # not something a manager should be offered the chance to approve — it is
    # something the agent must be told it cannot ask for, while the guest is
    # still in the conversation.
    if spec.precheck is not None:
        try:
            await spec.precheck(args, ctx)
        except AgentError as exc:
            return refusal(exc)

    verdict = autonomy.EXECUTE if approved else autonomy.decide(spec, ctx)
    if verdict == autonomy.REFUSE:
        return refusal(
            AgentError(
                autonomy.explain(spec, ctx),
                "That's not something I can do here — let me pass you to the team.",
            )
        )
    if verdict == autonomy.APPROVE:
        # Recorded, queued, and reported honestly to both the model and the
        # guest. Nothing has happened yet and nobody is told it has.
        #
        # The audit row and the model are told different things, deliberately.
        # `refusal_reason` stays the system's own sentence, because that is what
        # a person reviewing the queue needs. `model_message` carries the
        # tool's `when_deferred` instead, because the model's only useful
        # response to "this is queued" is to take the other road — and told the
        # internal rule instead, it apologises to the guest for it.
        exc = ApprovalRequired(autonomy.explain(spec, ctx))
        out = refusal(exc)
        out.input = args.model_dump(mode="json")
        out.model_message = spec.when_deferred or (
            "Not done yet — someone from the hotel has to approve this, which "
            "is not instant. Do not apologise and do not say it is done. Tell "
            "the guest it has gone to the team, and offer anything else you "
            "can do right now."
        )
        return out

    # Between the proposal and the approval, the hotel moved. Checked here
    # rather than inside the tool so a tool author cannot forget it, and only on
    # the approval path — a live turn was never stale to begin with.
    if approved and spec.revalidate is not None:
        try:
            await spec.revalidate(args, ctx)
        except AgentError as exc:
            return refusal(exc)

    try:
        result = spec.fn(args, ctx)
        if inspect.isawaitable(result):
            result = await result
    except AgentError as exc:
        return refusal(exc)
    except Exception as exc:  # noqa: BLE001 — the boundary is the point
        # A tool that breaks must never look like a tool that answered. The
        # model is told the system is unavailable, which routes to a human,
        # rather than being handed nothing and left to guess.
        logger.exception("tool %s failed", spec.name)
        return refusal(ToolUnavailable(f"{spec.name} failed: {type(exc).__name__}: {exc}"))

    # A tool may return (payload, result_id) when it created something the trace
    # should point at.
    result_id: str | None = None
    if isinstance(result, tuple) and len(result) == 2:
        result, result_id = result

    return ToolOutcome(
        tool=spec.name,
        status="executed",
        input=args.model_dump(mode="json"),
        output=result,
        result_type=spec.result_type,
        result_id=str(result_id) if result_id else None,
        model_message="",
        latency_ms=elapsed(),
    )


def load_all() -> None:
    """
    Import the tool modules so their decorators run.

    Explicit rather than a package scan: the registry is a security surface and
    what is in it should be a list somebody reads, not whatever happened to be
    on the import path.
    """
    from app.services.ai.tools import (  # noqa: F401
        booking,
        communication,
        frontdesk,
        marketing,
        operations,
        owner,
        pricing,
        read,
        sales,
        stay,
    )
