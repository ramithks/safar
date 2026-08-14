"""
The Front Desk agent — staff-facing. Slice 8.

"Who is arriving today?" "Which rooms are dirty?" "Put 102 out of order."

Two things make it a different agent rather than the Guest agent with more
permissions. It runs with `actor='member'`, so the conversation scoping in
`context.require_reservation` does not apply — a receptionist asking about a
booking is scoped by property, which `require_member` already established. And
it needs tools that do not exist yet: room status, arrivals, departures,
housekeeping boards. Those are PMS services today with no tool wrapper.

Recorded here now so the permission set is decided while the reasoning is
visible: it holds `frontdesk` and `housekeeping`, and it does not hold `folio`.
An agent that can move money at a hotel's front desk is a different
conversation, and it should be had deliberately rather than inherited.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="frontdesk",
    name="Front desk agent",
    description="Answers staff questions about arrivals, rooms and the day's work.",
    autonomy_level=1,
    permissions=("frontdesk", "housekeeping", "tasks"),
    tools=(
        "get_property",
        "get_room_types",
        "get_reservation",
        "search_availability",
        "get_stay_options",
        "create_task",
        "assign_task",
        "get_task",
        "get_task_status",
    ),
    channels=("internal",),
    system_prompt=(
        "You assist the hotel's own staff. Be terse. Give numbers, room numbers "
        "and names, not paragraphs."
    ),
    persona={"tone": "terse, operational"},
    escalation_rules={"on_unknown": "say_so"},
    available=False,
)
