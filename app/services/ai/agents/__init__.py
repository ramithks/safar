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
