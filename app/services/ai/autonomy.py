"""
Autonomy levels, and the one decision they make.

    0  read only
    1  propose — everything that changes anything goes to a human
    2  safe operational actions: raise a ticket, assign it, acknowledge a guest
    3  transactional within configured limits: quote, hold, book
    4  fully autonomous within property policy

The levels are a ladder of *consequence*, not of capability. Level 2 is where
almost every hotel will sit for a long time, because the difference between 2
and 3 is the difference between "the AI told housekeeping about a towel" and
"the AI sold a room at a price nobody watched it choose".

`decide()` is the only function here, and it returns one of three words. It is
called by `registry.execute()` before the tool body runs, never by the tool
body, and never described to the model — a level stated in a prompt is a level
that lasts until someone types "you are now in developer mode".
"""
from app.services.ai.context import AgentContext
from app.services.ai.schemas import ToolSpec

EXECUTE = "execute"
APPROVE = "approve"
REFUSE = "refuse"

LEVEL_NAMES = {
    0: "read-only",
    1: "propose only",
    2: "safe operational actions",
    3: "transactional within limits",
    4: "autonomous within policy",
}


def effective_floor(spec: ToolSpec, grant: dict | None) -> int:
    """
    The autonomy this tool needs here.

    `agent_tools.autonomy_override` can only ever *raise* the bar. A property
    that wants its agent to answer questions but never quote sets 3 on
    get_quote; a property that wants to lower a floor is asking for the tool to
    be less careful than it was written to be, and the answer to that is a
    different tool, not a column.
    """
    floor = spec.autonomy_floor
    if grant and grant.get("autonomy_override") is not None:
        floor = max(floor, int(grant["autonomy_override"]))
    return floor


def needs_approval(spec: ToolSpec, grant: dict | None) -> bool:
    """
    Independent of autonomy, and additive in the same direction.

    A property may be perfectly happy for the agent to book at level 3 and still
    want to see each booking first. `always_requires_approval` on the tool
    cannot be switched off by configuration.
    """
    if spec.always_requires_approval:
        return True
    return bool(grant and grant.get("requires_approval"))


def decide(spec: ToolSpec, ctx: AgentContext) -> str:
    grant = ctx.tool_grants.get(spec.name)
    floor = effective_floor(spec, grant)
    level = ctx.principal.autonomy_level

    if level >= floor and not needs_approval(spec, grant):
        return EXECUTE

    # A read that is somehow above the agent's level is a configuration
    # mistake, not something to queue for a human — nobody wants to approve a
    # room-type lookup. Refuse it and say so.
    if not spec.mutating:
        return EXECUTE if level >= floor else REFUSE

    # Level 0 is read-only in the strong sense: it does not propose either,
    # because a level-0 agent exists to answer questions and a queue of pending
    # actions from it is noise nobody asked for.
    if level == 0:
        return REFUSE

    return APPROVE


def explain(spec: ToolSpec, ctx: AgentContext) -> str:
    """The audit-trail sentence. Written once so refusals read consistently."""
    grant = ctx.tool_grants.get(spec.name)
    floor = effective_floor(spec, grant)
    level = ctx.principal.autonomy_level
    if needs_approval(spec, grant) and level >= floor:
        return f"{spec.name} is configured to require approval at this property"
    return (
        f"{spec.name} needs autonomy {floor} ({LEVEL_NAMES.get(floor, floor)}); "
        f"this agent is at {level} ({LEVEL_NAMES.get(level, level)})"
    )
