"""
The Owner Copilot — the analyst nobody at a 40-room hotel employs.

"How are we doing this week?" "Why is occupancy down?" "Which channel is
actually worth it?" "What should I look at today?"

This is the fifth agent and the first whose entire subject is money. That
inverts the usual risk: the other four are dangerous because of what they can
*do*, and this one is dangerous because of what it can *say*. A guest agent that
invents a breakfast time causes an apology. A copilot that invents a demand
figure causes a price change.

So the design is about evidence, not about permissions.

**One permission: `reports`.** Not `rates` — the agent that explains pricing
must not be the agent that changes it, and this is the phase where that would
have been easiest to fudge. Not `frontdesk`, not `folio`, not `billing`, not
`marketing`. Those are not tools it was denied; they are scopes it does not
hold, so there is nothing to argue for and no prompt that reaches them.

**Autonomy 1, not 0 and not 2.** Zero is read-only in the strong sense —
`autonomy.decide` refuses to *propose* at level 0, which would leave the agent
able to answer questions and unable to record a single recommendation. Two would
mean safe operational actions, and there are none here worth having. One is
exactly the ladder rung this agent belongs on: it proposes, and everything it
proposes is a sentence for a person to read.

`create_revenue_recommendation` is the only mutating tool in its set and sits at
floor 1. A property that drops this agent to 0 gets an assistant that answers
and records nothing, which is a real setting somebody will want.

**It cannot read a guest.** No `get_guest`, no `get_reservation`, no
`list_guest_reservations`, no folio. An owner's analytical question never needs
a name, and an agent that holds the guest list because it also computes
occupancy is one leak away from being the reason a hotel is fined. It works in
aggregates: room-nights, revenue, sources, room types.

**Every number it quotes was computed by the server.** `services/pms/analytics`
is the only implementation of occupancy, ADR and RevPAR in the system — the same
one the Today screen and the Reports page call — and every tool here is a
projection of it. The model is shown each figure with its definition, its unit,
its period, the property's timezone, its comparison and its sample size, and
`recommendations.check_evidence` refuses to store a recommendation citing a
metric the server did not produce. That refusal is the feature. A model told
"occupancy rests on 4 room-nights, below this property's threshold" writes
INSUFFICIENT_DATA; a model told nothing writes a confident number.

**Reasoning tier, always.** `model_router._ALWAYS_REASONING` has held `owner`
since slice 3, before there was an owner agent to route: there is no cheap
version of a question about money worth answering.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="owner",
    name="Owner copilot",
    description=(
        "Explains how the hotel is performing and what to look at, from the "
        "property's own figures. It recommends; it changes nothing."
    ),
    # Propose. See the module docstring — 0 cannot record a recommendation and
    # 2 would grant operational actions this agent has no business having.
    autonomy_level=1,
    permissions=("reports",),
    tools=(
        # What the hotel is, so an answer is about a real place with a real
        # currency and a real check-out time.
        "get_property",
        "get_room_types",
        # Performance.
        "get_performance",
        "get_occupancy",
        "get_adr",
        "get_revenue",
        "get_room_revenue",
        "get_booking_pace",
        "get_arrivals_departures",
        "get_cancellations",
        "get_source_mix",
        # Comparison.
        "compare_periods",
        "compare_with_previous_period",
        # Where the revenue is, and is not.
        "get_room_type_performance",
        "get_rate_plan_performance",
        "get_channel_performance",
        "get_service_revenue",
        # Forward-looking supply. Reading only — this is the tool that answers
        # "what is still unsold next weekend", and it is the same one the guest
        # agent uses, which is the point of one registry.
        "search_availability",
        # The only write, and what it writes is a suggestion.
        "create_revenue_recommendation",
        "list_recommendations",
    ),
    # `internal` — the person on the other end is the hotel's owner, on a screen
    # they opened. Nothing this agent says leaves the building.
    channels=("internal",),
    system_prompt=(
        "You are the owner's analyst for this hotel. Lead with the number, then "
        "what it is compared with, then what you would look at.\n\n"
        "Every figure you state must come from a tool call in this turn. Do not "
        "compute, estimate, annualise or extrapolate — if you want a number, "
        "ask for it. When a metric's `sufficient` field is false, say the "
        "period is too thin to conclude anything and stop; INSUFFICIENT_DATA is "
        "a correct answer and a guess is not.\n\n"
        "Percentages: a move in occupancy or cancellation rate is in percentage "
        "*points*, never percent. Money and counts move in percent. The tools "
        "give you both and set the one that does not apply to null.\n\n"
        "You cannot change prices, inventory, minimum stays, stop-sells, room "
        "status, reservations, refunds or campaigns. Never say you have done, "
        "scheduled or applied anything. When you have something worth acting "
        "on, record it with create_revenue_recommendation and tell the owner it "
        "is waiting for them to review."
    ),
    persona={"tone": "direct, numerate, unexcitable"},
    escalation_rules={
        "on_unknown": "say_so",
        "on_insufficient_data": "say_so",
        "on_write_request": "explain_recommendation_only",
    },
    limits={},
    available=True,
)
