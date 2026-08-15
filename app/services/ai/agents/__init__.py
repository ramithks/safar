"""
The five agents.

Each module here is a **definition, not a system**: a default tool list, a
permission set, an autonomy level, a persona and the channels it belongs on.
There is no code in any of them, because there is nothing for an agent to do
that the runtime does not already do — the difference between the Guest agent
and the Owner Copilot is which rows get written when the agent is provisioned.

That is the load-bearing claim of this whole slice. If activating the Front Desk
agent in slice 8 requires a new runtime, a new tool dispatcher or a new audit
path, the architecture was wrong. It should require inserting a row.

Only the Guest agent is provisioned today. The other four are here so their
shape is decided while the constraints are fresh, and so that "we will work out
the permissions later" never becomes the thing that ships.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentDefinition:
    agent_type: str
    name: str
    description: str

    # Autonomy the agent is *provisioned* at. A property may lower it; raising
    # it is a deliberate act by someone with the 'agents' permission.
    autonomy_level: int

    # Scopes from `agent_permissions`, using the same vocabulary as a human's
    # role. Deliberately minimal — an agent that holds 'folio' can be argued
    # into looking at money, so it holds it only if it needs to.
    permissions: tuple[str, ...]

    tools: tuple[str, ...]
    channels: tuple[str, ...]

    system_prompt: str
    persona: dict = field(default_factory=dict)
    knowledge_scope: tuple[str, ...] = ()
    escalation_rules: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)

    # False until the slice that turns it on. Provisioning refuses to create an
    # agent that nothing has been built to supervise.
    available: bool = False


# Which agents talk to guests, and therefore which runs are guest-scoped.
#
# `AgentContext.actor` drives `require_reservation` and `require_task`: a
# guest-scoped run may only read the subject of its own conversation, a
# staff-scoped run is bounded by the property instead. Getting this wrong in
# either direction is a real defect — too tight and the Front Desk agent cannot
# read the arrivals list, too loose and a guest on WhatsApp can ask about
# somebody else's booking.
#
# So it is derived from the agent type in one place rather than passed as an
# argument at each call site. It was passed as an argument, and
# `approvals.execute_approved` had it hardcoded to 'guest' — which would have
# re-imposed guest scoping on a Front Desk action the moment a manager approved
# one.
# **Sales is on this list and it was not.** The set was written when the Guest
# agent was the only one that ever met a member of the public, and "guest" read
# as the name of the audience rather than as one entry in a list. Activating the
# Sales agent made that wrong: it answers strangers on WhatsApp and Instagram,
# and `actor_for('sales')` was returning 'member' — so a person who had never
# stayed at the hotel would have had a run whose `require_task` was bounded by
# the property rather than by their own conversation.
#
# Caught by `scripts/sales_e2e.py` asserting on the routing tuple rather than on
# the agent type alone, which is why the two are returned together.
#
# The rule this encodes: an agent goes on this list if a stranger can reach it.
# Not whether it sells, not whether it is new — whether the person on the other
# end is somebody the hotel has authenticated. Nobody on WhatsApp is.
#
# **Marketing was on this list and came off it.** Phase D added it on a
# prediction — a marketing agent sounded like something that talks to the
# public. It does not: it answers *staff* on the `internal` console and plans
# campaigns, and what reaches a guest is a campaign rather than a conversation.
# Nobody outside the building can reach it, so it is member-scoped, and leaving
# it guest-scoped would have meant an agent that could not read its own hotel's
# tickets in order to protect a guest who is never on the other end.
GUEST_FACING = frozenset({"guest", "sales"})


def actor_for(agent_type: str | None) -> str:
    """
    'guest' or 'member', from the agent's type.

    Defaults to 'guest' for an unknown type. The safe default is the tighter
    one: a new agent nobody has classified yet should be over-restricted and
    visibly broken, not under-restricted and quietly leaking.
    """
    return "guest" if (agent_type or "guest") in GUEST_FACING else "member"


def all_definitions() -> dict[str, AgentDefinition]:
    from app.services.ai.agents import frontdesk, guest, marketing, owner, sales

    return {
        d.agent_type: d
        for d in (
            guest.DEFINITION,
            frontdesk.DEFINITION,
            sales.DEFINITION,
            marketing.DEFINITION,
            owner.DEFINITION,
        )
    }


def get(agent_type: str) -> AgentDefinition:
    defs = all_definitions()
    if agent_type not in defs:
        raise KeyError(f"unknown agent type {agent_type!r}")
    return defs[agent_type]
