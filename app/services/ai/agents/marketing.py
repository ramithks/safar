"""
The Marketing agent — architecture only. Slice 10.

Campaigns, offers, social copy, creator briefs.

It is the only one of the five that does not touch the operational system at
all: it produces drafts a human publishes. That is why it holds no PMS
permission and why its autonomy stays at 1 — "propose" is the whole job. A
marketing agent that could publish would be an agent that could say something
about the hotel that the hotel never said, on a channel the hotel cannot recall.

Its tools are generative rather than operational, and none of them exist yet.
Recorded here so the boundary is drawn before anyone is tempted to hand it
`rates` so it can put a real price in an offer.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="marketing",
    name="Marketing agent",
    description="Drafts campaigns and offers for a human to approve and publish.",
    autonomy_level=1,
    permissions=(),
    tools=(
        "get_property",
        "get_property_knowledge",
        "get_room_types",
        "list_services",
    ),
    channels=("internal",),
    system_prompt=(
        "You draft marketing copy for this hotel. Everything you write is a "
        "draft for a person to approve. Never invent an offer, a price or an "
        "award."
    ),
    persona={"tone": "specific, unsentimental"},
    escalation_rules={"always": "propose_only"},
    available=False,
)
