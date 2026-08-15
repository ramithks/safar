"""
The Marketing agent — the first one that speaks first.

The other three answer somebody who wrote to the hotel. That single difference
is why this agent needed a phase rather than a tool module: an inbound reply
needs no consent, because the guest opened the conversation; a proactive message
needs permission, a limit on how often, a record of who was written to, and a
way to stop. None of that existed, and none of it belongs to an agent — it
belongs to the platform, which is why it lives in `services/marketing` and this
file is still nine lines of configuration.

**Autonomy 2**, like the other three, and the same reasoning: level 2 mistakes
are recoverable — a draft nobody wanted, a campaign somebody cancels — and level
3 mistakes here reach four hundred people at once and cannot be recalled. So
`execute_campaign` sits at floor 3 *and* carries `always_requires_approval`,
which cannot be switched off by configuration. A hotel that raises this agent to
4 gets faster drafting, not unattended mass outbound. That is deliberate: the
dial is described to hoteliers as being about how much confirming they do, and
"it can now message your entire guest list unsupervised" is not something anyone
should acquire by turning it.

**It cannot send a message.** No `send_message`, no `send_template`. Those take
a sentence and a conversation, and an agent holding one plus a guest list could
message anybody one call at a time with no consent check and no cap — the whole
of this phase routed around by a tool that already existed. Its only route to a
guest is `execute_campaign`, which reserves credit, materialises recipients and
revalidates each one at the moment before their message leaves.

**It cannot read a guest.** No `get_guest`, no `list_guest_reservations`, no
`get_reservation`. It works in counts: `find_eligible_guests` returns "83 of 127
eligible, 21 no consent, 12 capped" and never a name or a number. There is
nothing an audience decision needs a name for, and putting a hotel's guest list
into a model's context for no capability at all is the kind of thing that is
obvious only after it has happened.

**It holds `marketing` and nothing else.** Not `rates` — an offer's discount is
a figure on the campaign row that a human approved, not a price this agent may
compute. Not `frontdesk`, not `folio`, not `tasks`. The narrowest permission set
of any agent here, on the agent with the largest blast radius, which is the
right way round.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="marketing",
    name="Marketing agent",
    description=(
        "Plans and runs campaigns to past and upcoming guests. Every send needs "
        "a manager, and every recipient's consent is checked again as it goes."
    ),
    autonomy_level=2,
    # One scope. `marketing` gates every tool in the module and nothing else in
    # the system grants it, so this agent's reach is exactly the campaign path.
    permissions=("marketing",),
    tools=(
        # What the hotel is — the same four the Guest agent uses to answer a
        # question, here so the copy is about a real place.
        "get_property",
        "get_property_knowledge",
        "get_room_types",
        "list_services",
        # Who could be reached. Counts only.
        "find_eligible_guests",
        "estimate_campaign_audience",
        # Writing it.
        "create_campaign",
        "propose_campaign_message",
        "schedule_campaign",
        "cancel_campaign",
        # Sending it. The only route to a guest, and it always needs approval.
        "execute_campaign",
    ),
    # `internal` — a staff console channel, because the person the marketing
    # agent talks to is the hotel, not a guest. What reaches guests is a
    # campaign, and a campaign is not a conversation.
    channels=("internal",),
    system_prompt=(
        "You plan marketing campaigns for this hotel. You work in numbers and "
        "drafts: nothing you write reaches a guest until a manager approves it."
    ),
    persona={"tone": "specific, unsentimental"},
    escalation_rules={
        "on_send": "approval_required",
        "on_unknown": "handoff",
    },
    # No discount ceiling here, deliberately. A campaign's discount is a figure
    # on the campaign row that a human approves along with the campaign; it is
    # not something this agent grants per guest, so there is no per-agent limit
    # for it to be clamped against. `claims.check` is the enforcement.
    limits={},
    available=True,
)
