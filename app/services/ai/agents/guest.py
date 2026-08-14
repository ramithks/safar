"""
The Guest agent — the only one built.

Ships at **autonomy 2**: it may answer, look things up, raise tickets and assign
them, and it may not sell anything without a person. That line is where it is
for a reason. Level 2 mistakes are recoverable — a ticket nobody needed, an
answer that should have been a handoff. Level 3 mistakes are a room sold at a
price nobody watched it choose.

The line it does not cross is `folio`, `billing` and `payouts`. It holds
`rates` and `frontdesk`, because quoting and booking are what a guest agent is
*for*; it does not hold the scopes that move money on a stay that already
exists, because arguing an AI into a refund is the attack, and a scope it does
not have cannot be argued for.

**Autonomy is the only dial a hotel turns.** The selling tools are granted and
sit at autonomy floor 3, so at level 2 they produce approval requests rather
than holds. The alternative — withholding the scope *and* the autonomy — looks
safer and is worse: a hotel that raises its agent to level 3 would get silence,
because the second dial nobody mentioned is still off. Two controls where
turning the obvious one does nothing is how a configuration ends up wrong in the
direction people did not expect.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="guest",
    name="Guest agent",
    description=(
        "Answers guests on WhatsApp and Instagram: questions, requests, "
        "bookings and pre-arrival."
    ),
    autonomy_level=2,
    permissions=("tasks", "messaging", "rates", "frontdesk"),
    tools=(
        # Knowing things
        "get_property",
        "get_property_policies",
        "get_property_knowledge",
        "get_room_types",
        "list_services",
        # Knowing this guest — all scoped to the conversation, none of them
        # taking a guest id the model could be talked into changing.
        "get_guest",
        "list_guest_reservations",
        "get_reservation",
        # Selling. Granted, and gated by autonomy rather than by absence, so a
        # property that raises the agent to level 3 gets the capability without
        # a code change.
        "search_availability",
        "get_stay_options",
        "get_quote",
        "release_hold",
        "create_booking",
        # Doing things
        "create_task",
        "assign_task",
        "get_task",
        "get_task_status",
        "list_guest_tasks",
        # Stopping
        "request_human_handoff",
    ),
    channels=("whatsapp", "instagram"),
    system_prompt=(
        "You are the hotel's own assistant. Be warm and brief. Reply in the "
        "language the guest writes in."
    ),
    persona={"tone": "warm, brief", "languages": ["en", "hi"]},
    knowledge_scope=(
        "checkin", "checkout", "breakfast", "dining", "parking", "pets",
        "children", "wifi", "amenities", "directions", "house_rules",
        "cancellation", "payment", "services", "accessibility", "local",
    ),
    escalation_rules={
        "on_unknown": "handoff",
        "on_complaint": "handoff",
        "on_refund": "handoff",
        "on_repeat_request": "handoff",
    },
    limits={"max_discount_pct": 10, "max_booking_value_paise": 5_000_000},
    available=True,
)
