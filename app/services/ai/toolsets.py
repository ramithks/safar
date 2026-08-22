"""
Which tools the model is *shown*, given what the message is about.

**This is not a permission.** Permission is `agent_tools`, checked in
`permissions.check()`, and nothing here can widen it: every set below is
intersected with the agent's grants, and a tool the model somehow calls anyway
still goes through the same gate it always did. What this changes is the size of
the menu, not the size of the door.

**Why it is worth a module.** Measured on 1,000 real Vertex runs:

    tools called   runs   avg tokens_in   avg latency
    0               382       2,292          6.0 s
    1               406       5,253          9.9 s
    2                68       6,908         13.1 s
    3                43      10,499         20.7 s

The floor is 2,292 tokens for a turn that calls nothing, and the guest agent's
23 tool schemas are about 14 KB of that — roughly seventy per cent of the base
prompt. Every model step re-sends the whole thing, so a three-tool turn pays for
those schemas four times. That is the shape of the cost: not one fat tool
result, which is what this was assumed to be until it was measured
(`get_stay_options` averages 1,188 characters), but the same catalogue sent
over and over.

**And it is not only a cost argument.** A model shown twenty-three tools for
"what time is breakfast" has twenty-two ways to be wrong. The classifier has
already decided what the message is about, one cheap call before the run opens;
until now that decision only picked a model tier. Using it to pick the menu as
well is free.

**The default is everything.** `guest_conversation` is both the greeting task
and the fallback for anything the classifier could not read, so it narrows
nothing — an unclear message must not also be an under-equipped one. Narrowing
happens only where the classifier was confident enough to name a real
capability.
"""
import logging

logger = logging.getLogger(__name__)

# Shown whatever the message is about.
#
# Handoff, because every task's honest ending is a person. Knowledge, because a
# guest halfway through booking still asks whether there is parking, and the
# alternative to answering is inventing. Sending, because on some paths the
# agent replies with a tool rather than with prose.
CORE = frozenset({
    "request_human_handoff",
    "get_property_knowledge",
    "get_property",
    "send_message",
})

# task → what that task can possibly need, beyond CORE.
#
# Generous on purpose. The failure this could introduce is worse than the cost
# it removes: a turn that needed `get_quote` and was not shown it cannot
# recover, while a turn shown one tool too many merely costs a few hundred
# tokens. So every set includes the tools for the obvious next step as well as
# the current one — an availability question becomes a price question in the
# same breath far more often than not.
BY_TASK: dict[str, frozenset[str]] = {
    "guest_faq": frozenset({
        "send_room_photos", "get_hotel_page_link",
        "get_property_policies", "get_room_types", "list_services",
    }),
    "guest_request": frozenset({
        "create_task", "assign_task", "get_task", "get_task_status",
        "list_guest_tasks", "get_reservation", "order_service", "list_services",
    }),
    "reservation_lookup": frozenset({
        "get_reservation", "list_guest_reservations", "get_guest",
        "get_booking_status", "get_check_in_link", "get_property_policies",
    }),
    "availability": frozenset({
        "send_room_photos", "get_hotel_page_link",
        "search_availability", "get_stay_options", "get_room_types",
        # A guest who hears a room is free asks the price next, and being shown
        # the pricing tools here saves the whole turn that would otherwise be
        # spent discovering they exist.
        "get_quote", "get_property_policies",
    }),
    "simple_quote": frozenset({
        "send_room_photos", "get_hotel_page_link",
        "search_availability", "get_stay_options", "get_quote", "get_room_types",
        "get_property_policies", "list_services", "create_booking_payment_link",
    }),
    "booking": frozenset({
        "send_room_photos", "get_hotel_page_link",
        "search_availability", "get_stay_options", "get_quote", "release_hold",
        "create_booking", "create_booking_payment_link", "get_booking_status",
        "get_property_policies", "get_room_types",
    }),
    "payment_status": frozenset({
        "get_booking_status", "get_reservation", "list_guest_reservations",
        "create_payment_link", "create_booking_payment_link",
        "get_check_in_link", "get_property_policies",
    }),
    "cancellation_dispute": frozenset({
        "get_booking_status", "get_reservation", "list_guest_reservations",
        "get_property_policies", "get_folio", "create_task",
    }),
    "complex_negotiation": frozenset({
        "send_room_photos", "get_hotel_page_link",
        "get_negotiation_policy", "propose_rate_exception", "get_quote",
        "get_stay_options", "search_availability", "get_room_types",
        "create_booking_payment_link",
    }),
    "policy_conflict": frozenset({
        "get_property_policies", "get_reservation", "get_booking_status",
    }),
    "multi_reservation": frozenset({
        "list_guest_reservations", "get_reservation", "get_booking_status",
        "get_check_in_link",
    }),
}

# Tasks that deliberately narrow nothing.
#
# `guest_conversation` is the greeting *and* the fallback for a message the
# classifier could not read. Under-equipping the turns we understand least is
# exactly backwards. The staff tasks are here for a different reason: a
# receptionist's console turn is low-volume and unpredictable in a way a guest
# thread is not, and there is no measurement saying it costs anything.
EVERYTHING = frozenset({
    "guest_conversation",
    "frontdesk_operations",
    "frontdesk_summary",
    "operational_analysis",
    "revenue_analysis",
    "marketing_planning",
    "complex_request",
    "escalation",
    "handoff",
})


def for_task(task: str | None, granted: frozenset[str] | set[str]) -> frozenset[str]:
    """
    The tool names to describe for this turn.

    `granted` is what the agent actually holds. The return value is always a
    subset of it — that is the invariant that makes this an optimisation rather
    than a second permission system, and `tests/test_toolsets.py` asserts it
    over every task and every agent definition.
    """
    granted = frozenset(granted)
    if not task or task in EVERYTHING:
        return granted

    wanted = BY_TASK.get(task)
    if wanted is None:
        # A capability nobody has mapped yet — a new task name, or one added to
        # the router and forgotten here. Showing everything is the behaviour we
        # already had, so a gap costs tokens rather than correctness.
        logger.debug("no toolset for task %r; describing everything granted", task)
        return granted

    narrowed = granted & (wanted | CORE)

    # A task whose whole toolset the agent lacks. Rather than send a model into
    # a turn with three tools that have nothing to do with the question, give it
    # everything and let it find its own way — this is a misconfigured agent,
    # not a cheap turn.
    if not (narrowed - CORE):
        return granted

    return narrowed
