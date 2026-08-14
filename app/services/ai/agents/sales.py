"""
The Sales agent — outbound. Slice 10.

Follows up abandoned enquiries, chases quotes that expired without a booking,
suggests packages.

Outbound is what makes it the most dangerous of the five, and the reason it is
last. Everything else in this system answers someone who spoke first. A sales
agent starts conversations, which means every message it sends is subject to the
WhatsApp template rules, the marketing opt-in on `guests.marketing_opt_in`, and
a frequency cap nobody has written yet. Those are the constraints to design
against, not the copy.

It shares the Guest agent's selling tools, because a quote is a quote whoever
asked for it — one pricing service, one hold system, one reservation table.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="sales",
    name="Sales agent",
    description="Follows up enquiries and expired quotes. Outbound, and therefore opt-in bound.",
    autonomy_level=1,
    permissions=("messaging", "rates"),
    tools=(
        "get_property",
        "get_property_knowledge",
        "get_room_types",
        "search_availability",
        "get_stay_options",
        "get_quote",
        "request_human_handoff",
    ),
    channels=("whatsapp", "instagram"),
    system_prompt=(
        "You follow up with people who enquired and did not book. One message, "
        "no pressure, and never a second one unless they reply."
    ),
    persona={"tone": "light, unpushy"},
    escalation_rules={"on_reply": "handoff_to_guest_agent"},
    limits={"max_discount_pct": 10},
    available=False,
)
