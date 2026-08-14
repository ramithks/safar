"""
May this agent call this tool at all?

Three gates, in order, before autonomy is even considered:

    1. Is the tool registered?          — a name the model invented
    2. Does this agent have a grant?     — `agent_tools`, a closed list
    3. Does it hold the tool's scope?    — `agent_permissions`, same vocabulary
                                           as a human's role

Each is a row in a table, checked in Python. None of them is a sentence in a
system prompt, and that is the entire security argument: an instruction inside a
guest's message can change what the model *wants* to do and cannot change what
`registry.execute()` *lets* it do.

The order matters for what gets logged. "Tool not enabled" and "permission
denied" are different operational problems — the first is a configuration
someone should widen, the second is a configuration someone should not.
"""
import logging

from app.services.ai.context import AgentContext
from app.services.ai.errors import PermissionDenied, ToolNotEnabled
from app.services.ai.schemas import ToolSpec

logger = logging.getLogger(__name__)


def check(spec: ToolSpec, ctx: AgentContext) -> None:
    """Raise if the agent may not call this tool. Silent if it may."""

    grant = ctx.tool_grants.get(spec.name)
    if grant is None:
        raise ToolNotEnabled(
            f"agent '{ctx.principal.display_name}' has no grant for {spec.name}",
        )
    if grant.get("enabled") is False:
        raise ToolNotEnabled(f"{spec.name} is disabled for this agent")

    if spec.permission and not ctx.principal.can(spec.permission):
        raise PermissionDenied(
            f"{spec.name} requires the '{spec.permission}' permission, "
            f"which agent '{ctx.principal.display_name}' does not hold",
        )


def channel_allowed(ctx: AgentContext, channels: list[str] | None) -> bool:
    """
    A Guest agent has no business in the internal console and a Front Desk agent
    none on WhatsApp. Checked where a run starts rather than per tool, because
    it is a property of the conversation, not of the action.
    """
    if not channels:
        return True
    return ctx.channel in channels
