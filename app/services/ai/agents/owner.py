"""
The Owner Copilot — read-only. Slice 9.

"How many rooms are occupied?" "What is today's revenue?" "How many
cancellations this week?"

Provisioned at **autonomy 0** and holding only `reports`, which is the whole
design: an agent that answers questions about money must not be able to move
any. Every mutating tool in the registry sits at floor 2 or above, so level 0
refuses them without anyone having to remember to leave them out of the tool
list.

The tools it needs are aggregates — occupancy, ADR, revenue, pace — and those
are `services/pms/workspace.today_stats` and whatever the reports service
becomes. Not wrapped yet.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="owner",
    name="Owner copilot",
    description="Answers the owner's questions about occupancy, revenue and demand. Reads only.",
    autonomy_level=0,
    permissions=("reports",),
    tools=(
        "get_property",
        "get_room_types",
        "search_availability",
    ),
    channels=("internal",),
    system_prompt=(
        "You answer the owner's questions about how the hotel is doing. Lead "
        "with the number. Say when a figure is partial or when the day is not "
        "over."
    ),
    persona={"tone": "direct, numerate"},
    escalation_rules={"on_unknown": "say_so"},
    available=False,
)
