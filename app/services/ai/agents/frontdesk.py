"""
The Front Desk agent — staff-facing, and the test of the whole architecture.

"Who is arriving today?" "Is 102 ready for Mr Rao?" "Check in TB-4K2M9X."
"Nisha in 209 needs the AC looked at." "How much does 102 owe?"

Slice 1 claimed that activating a second agent should be an insert rather than a
system: same runtime, same registry, same permission and autonomy tables, same
audit trail, same ledger, one new tool module. This file is where that is either
true or it is not. It required a `tools/frontdesk.py`, one migration adding an
`internal` channel, and this definition. No second runtime, no second dispatcher,
no second PMS.

**Two things make it a different agent rather than the Guest agent with more
permissions.**

It runs with `actor='member'`, so the conversation scoping in
`context.require_reservation` does not apply. That is not a relaxation — it is a
different boundary. A guest agent is scoped to one conversation's subject
because a guest may only see their own booking. A staff agent is scoped to one
*property*, because a receptionist may see every booking in the hotel they are
standing in and none in any other. The property comes from an authenticated
membership; there is no argument on any tool in the front desk set that can name
one.

And it answers on `internal`, a channel with no provider. Nothing it says leaves
the building. That is why it holds `messaging` for `request_human_handoff` and
is not granted `send_message` or `send_template`: there is no window to respect,
no template to approve, and no Meta to reach.

**On `folio`, which this file used to refuse.**

The earlier version of this definition withheld `folio` and said so: "an agent
that can move money at a hotel's front desk is a different conversation, and it
should be had deliberately rather than inherited." The conversation has now been
had, and the answer is a qualified yes, on three conditions that are code rather
than intention:

  1. The agent can *read* a folio freely. A balance is the single most-asked
     question at a desk and getting it from the PMS is strictly safer than a
     receptionist adding lines up.
  2. It can post exactly one kind of entry — `addon` — because
     `post_folio_charge` takes no `kind` argument to talk it out of. Payments,
     refunds and adjustments are not restricted, they are absent.
  3. Every charge requires a human, permanently. `always_requires_approval` on
     that tool cannot be switched off by configuration, so a hotel that raises
     the dial to 3 to stop confirming check-ins does not thereby start putting
     money on bills unwatched.

What it still does not hold: `billing`, `payouts`, `members`, `agents`,
`reports`, `rates`. Refunds, payouts, credit grants, wallet administration and
rate changes are not tools it was denied — they are scopes it does not have, so
there is nothing to argue for.

**Autonomy 2, and the floors do the rest.**

Provisioned at 2, which is where a hotel should leave it. At 2 every read runs,
every operational action runs — raise a job, assign it, move it along, mark a
room clean — and the three that change a guest's stay or their bill come back as
approval requests: `check_in`, `check_out`, `post_folio_charge`, all at floor 3.
A property that trusts it more moves one dial and gets check-in and check-out
unattended; the charge still queues, because that floor is nailed down.

The alternative — withholding the tools instead of holding the level — is the
mistake slice 1 warned about in `guest.py`: two controls where turning the
obvious one does nothing.
"""
from app.services.ai.agents import AgentDefinition

DEFINITION = AgentDefinition(
    agent_type="frontdesk",
    name="Front desk agent",
    description=(
        "Answers staff questions about arrivals, rooms, guests and the day's "
        "work, and does the operational parts of it."
    ),
    autonomy_level=2,
    permissions=("frontdesk", "housekeeping", "tasks", "folio", "messaging"),
    tools=(
        # The hotel's own facts. Shared with the Guest agent unchanged — there is
        # one answer to "what time is checkout" and it does not depend on who is
        # asking.
        "get_property",
        "get_property_policies",
        "get_property_knowledge",
        "get_room_types",
        "list_services",
        # The day. Property-scoped, staff-only, and refused outright on a guest
        # conversation by `_require_staff` as well as by the grant.
        "list_arrivals",
        "list_departures",
        "list_in_house",
        "find_reservation",
        "get_stay",
        "list_rooms",
        "get_room",
        "get_folio",
        "list_open_tasks",
        # Availability, because "can I put a walk-in somewhere" is a front desk
        # question. Reading only: `get_quote` and `create_booking` are the Sales
        # agent's, and pricing a room is not what this one is for.
        "search_availability",
        "get_stay_options",
        # Doing the work.
        "create_staff_task",
        "assign_task",
        "get_task",
        "get_task_status",
        "update_task_status",
        "set_room_status",
        # Changing a stay. Floor 3, so approval requests at the level this
        # ships at.
        "check_in",
        "check_out",
        # Money. Floor 3 *and* always_requires_approval.
        "post_folio_charge",
        # Stopping.
        "request_human_handoff",
    ),
    channels=("internal",),
    system_prompt=(
        "You assist the hotel's own staff at the front desk. Be terse — give "
        "numbers, room numbers, names and ticket codes, not paragraphs. These "
        "are colleagues who know the hotel; do not explain what they already "
        "know and do not apologise for the system's rules, just state them."
    ),
    persona={"tone": "terse, operational"},
    knowledge_scope=(
        "checkin", "checkout", "breakfast", "dining", "parking", "pets",
        "children", "wifi", "amenities", "directions", "house_rules",
        "cancellation", "payment", "services", "accessibility", "local",
    ),
    escalation_rules={
        "on_unknown": "say_so",
        "on_refund": "handoff",
        "on_dispute": "handoff",
    },
    available=True,
)
