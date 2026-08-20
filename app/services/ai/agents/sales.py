"""
The Sales agent — the enquiry that has not booked yet.

The Guest agent answers people the hotel already has a relationship with: a
booking, a stay, a question about breakfast. This one answers the other half of
the same inbox — someone who found the hotel on Instagram and wants to know if
there is a room on Friday and what it costs. Same channels, same runtime, same
tools, different job.

**It is the Guest agent's selling half, with two additions and one subtraction.**
Availability, options, quotes, holds, bookings and follow-up tasks are literally
the same tools; the additions are `get_negotiation_policy` and
`propose_rate_exception`, which are what turn "the discount was clamped" into a
conversation. The subtraction is the guest's own records: no `get_guest`, no
`list_guest_reservations`, no `get_reservation`, because a person who has not
booked has nothing for those to read and an agent that can read stays is one a
stranger can try to aim at somebody else's.

**Autonomy 2**, the same as the other two, and for the reason the ladder was
written: the difference between 2 and 3 is the difference between raising a
follow-up ticket and selling a room at a price nobody watched it choose. So the
selling tools are *granted* and sit at floor 3, and at level 2 they produce
approval requests. A hotel that wants unattended quoting turns one dial and gets
it; nothing needs a code change, and nothing was withheld twice.

**What it does not hold is money that already exists.** No `folio`, no
`billing`, no `payouts` — no reading a bill, no touching a balance, no refunds.
Everything this agent does is about a stay that has not happened yet, and the
scope that could reach a stay that has is simply absent rather than restricted.

**Outbound is still not built.** The original sketch of this agent was a chaser:
it would message people who never replied. That needs the marketing opt-in on
`guests.marketing_opt_in`, a frequency cap and the WhatsApp template rules, none
of which exist, so this agent has `send_template` withheld and answers only
people who spoke first. `create_task` is the follow-up mechanism — a job for a
human, not a message the agent sends itself, which is also the honest reading of
"do not spam guests".
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="sales",
    name="Sales agent",
    description=(
        "Answers enquiries on WhatsApp and Instagram: availability, options, "
        "prices, and booking the room."
    ),
    autonomy_level=2,
    # The same four the Guest agent holds. `frontdesk` is here because
    # `create_booking` requires it and for no other reason — the tools that scope
    # checks in and folio charges to it are not granted, and `agent_tools` is a
    # closed list, so the permission alone reaches nothing.
    permissions=("tasks", "messaging", "rates", "frontdesk"),
    tools=(
        # What the hotel is
        "get_property",
        "get_property_policies",
        "get_property_knowledge",
        "get_room_types",
        "list_services",
        # What is free, and what it costs
        "search_availability",
        "get_stay_options",
        "get_quote",
        # Price, when the guest pushes back
        "get_negotiation_policy",
        "propose_rate_exception",
        # Closing, and un-closing
        "create_booking",
        # Closing it. Takes the money against a quote and lets the payment
        # create the booking, which is the only way this agent can complete
        # a sale without a manager pressing a button.
        "create_booking_payment_link",
        "release_hold",
        # Following up — a ticket for a person, not a message this agent sends
        "create_task",
        "get_task_status",
        # Speaking, and stopping
        "send_message",
        "request_human_handoff",
    ),
    channels=("whatsapp", "instagram"),
    system_prompt=(
        "You sell rooms at this hotel to people enquiring about staying. Find "
        "out the dates and how many people before you look anything up. Be "
        "straight about price and about what is not available."
    ),
    persona={"tone": "warm, direct, unpushy", "languages": ["en", "hi"]},
    knowledge_scope=(
        "checkin", "checkout", "breakfast", "dining", "parking", "pets",
        "children", "wifi", "amenities", "directions", "house_rules",
        "cancellation", "payment", "services", "accessibility", "local",
    ),
    escalation_rules={
        "on_unknown": "handoff",
        "on_complaint": "handoff",
        "on_group_booking": "handoff",
        "on_payment_issue": "handoff",
        "on_discount_above_ceiling": "handoff",
    },
    # The agent's own ceiling, on top of the property's. A hotel that lets its
    # duty manager go to 15% may want its AI to stop at 10, and that is a number
    # on the agent row rather than a sentence in a prompt.
    #
    # `max_booking_value_paise` is the same idea for size: above it,
    # `create_booking` refuses and hands over. A ₹90,000 group booking is a
    # phone call, not a chat.
    limits={"max_discount_pct": 10, "max_booking_value_paise": 5_000_000},
    available=True,
)
