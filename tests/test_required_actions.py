"""
The two tool calls a hotel's revenue depends on, made compulsory.

**The measurement this exists for.** The live replay — same code, same
conversation, same model — scored between 28 and 44 of 44 assertions across
runs. The assertions that moved were always the same two:

    "Actually 5 kamre chahiye, 10 log aa rahe hain"   sometimes no get_group_rates
    "Yaar thoda kam karo na, 4000 per night mein?"    sometimes no negotiate_rate

Nothing was wrong on the failing runs. The tool was granted, described, on the
menu, and named twice in the prompt. The model answered instead, and on the
negotiation turn its reply then repeated the guest's own ₹4,000 — which the
reply guardrail correctly refused, which turned a guest who was ready to pay
into a handoff at eleven at night.

A prompt cannot fix that, because the prompt already says it. So the decision
moved out of the model: `required_actions.detect` reads the guest's words and
the structured session, and `runtime` runs the tool itself when the model did
not.

**What is asserted here, and what is not.** Everything below is about detection
and about arguments — the parts that must be identical on every run. Whether
the model *would* have called the tool is not testable here and is measured
instead by `scripts/agent_eval.py` against the live model. The enforcement path
itself is asserted in `test_ai_runtime.py`, with a scripted provider that
deliberately refuses to call anything.

**Both directions are asserted.** A false requirement is not a harmless extra
tool call: `NEGOTIATION` on a message that was not a haggle opens a negotiation
the guest never asked for, and `GROUP_BOOKING` on "2 adults" spends a turn and a
half a second on volume bands for a couple. So there are as many tests below of
messages that must require nothing as of messages that must.
"""
import pytest

from app.services.ai import required_actions as ra


def intents(message: str, session: dict | None = None) -> list[str]:
    return [r.intent for r in ra.detect(message, session or {})]


def requirement(message: str, session: dict | None = None, intent: str = ra.GROUP_BOOKING):
    for found in ra.detect(message, session or {}):
        if found.intent == intent:
            return found
    return None


# A session far enough along that a negotiation has something to be about.
# Everything in it is the shape `messaging/session.py` actually stores.
PRICED = {
    "check_in": "2026-09-10",
    "check_out": "2026-09-12",
    "rate_plan_id": "11111111-1111-4111-8111-111111111111",
    "room_type_id": "22222222-2222-4222-8222-222222222222",
    "room_name": "Deluxe Balcony",
    "units": 1,
    "adults": 2,
    "children": 0,
}


# ── group booking ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    # The replay's own sentence, which is the reason this module exists.
    ("Actually 5 kamre chahiye, 10 log aa rahe hain", 5),
    # §4's list, in all three languages a guest actually writes.
    ("5 kamre", 5),
    ("5 rooms", 5),
    ("3 rooms chahiye", 3),
    ("3 kamre chahiye", 3),
    ("10 rooms available hain?", 10),
    ("mujhe 10 kamre chahiye", 10),
    ("हमें 5 कमरे चाहिए", 5),
    ("do rooms mil jayenge?", 2),
    ("paanch kamre book karne hain", 5),
    ("teen kamron ka rate kya hai", 3),
    ("ten rooms please", 10),
    ("four rooms for the weekend", 4),
    # Rooms stated after the noun rather than before it.
    ("rooms 4 chahiye", 4),
])
def test_a_number_of_rooms_is_a_group_enquiry(message, expected):
    found = requirement(message)
    assert found is not None, f"{message!r} raised nothing"
    assert found.tool == "get_group_rates"
    assert found.arguments["units"] == expected
    assert found.forceable, "the application must be able to run this itself"


@pytest.mark.parametrize("message,expected_units", [
    # §4: "we need rooms for 10 people". Party size is how half of India states
    # a group, and a hotel that only listens for a room count misses it.
    ("we need rooms for 10 people", 5),
    ("hum 10 log hain", 5),
    ("8 people aa rahe hain, rooms chahiye", 4),
    ("we are 12 people", 6),
    ("20 pax ke liye booking", 10),
    ("6 guests ke liye kya rate hoga", 3),
])
def test_a_party_size_is_a_group_enquiry_too(message, expected_units):
    found = requirement(message)
    assert found is not None, f"{message!r} raised nothing"
    assert found.arguments["units"] == expected_units, (
        "two to a room, and only to decide whether to look the bands up"
    )


@pytest.mark.parametrize("message", [
    "group booking karni hai",
    "group rates kya hain?",
    "bulk booking ke liye discount milega?",
    "shaadi ke liye rooms chahiye",
    "corporate booking karwani hai",
    "multiple rooms chahiye",
    "we have a team outing coming up",
])
def test_the_word_group_is_enough_without_a_number(message):
    """
    The answer is the hotel's own bands and its own minimum, and the model may
    state neither from memory. So the tool runs even when the sentence carries
    no number at all — `get_group_rates` is what knows what counts as a group
    here, and it is a different number at every property.
    """
    found = requirement(message)
    assert found is not None
    assert found.forceable
    assert found.arguments["units"] >= ra.GROUP_FROM_ROOMS


@pytest.mark.parametrize("message", [
    "room chahiye",
    "1 room chahiye",
    "ek kamra mil jayega?",
    "2 adults ke liye",
    "do log hain hum",
    "breakfast included hai?",
    "checkin time kya hai",
    "thank you so much",
    "hi",
    # A price is a four-digit number and is not four thousand rooms.
    "4000 per night theek hai?",
    # A date is a number beside a noun and must not read as a room count.
    "10 September ko aana hai",
])
def test_what_is_not_a_group(message):
    """
    The other direction, and the one that costs more when it is wrong. A false
    `GROUP_BOOKING` spends a turn on volume bands for a couple; a false
    `NEGOTIATION` opens a haggle nobody started.
    """
    assert ra.GROUP_BOOKING not in intents(message), f"{message!r} read as a group"


def test_a_room_already_chosen_travels_with_the_group_question():
    """
    §4: "existing selected room + room count change". The guest picked the
    Deluxe two turns ago and now wants five of them; asking the bands about the
    property in general would lose the room, which is the failure §9 is about.
    """
    found = requirement("ab 5 kamre chahiye", PRICED)
    assert found.arguments["units"] == 5
    assert found.arguments["room_type_id"] == PRICED["room_type_id"]


def test_a_group_question_is_satisfied_by_a_quote_for_the_same_rooms():
    """
    A model that went straight to `get_quote` with five rooms did the *better*
    thing — the quote applies the band itself — and forcing a second call after
    it would be the system talking over a model that was already right.
    """
    found = requirement("5 kamre chahiye")
    assert "get_quote" in found.satisfied_by
    assert "negotiate_rate" in found.satisfied_by


# ── negotiation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    # §5's list, verbatim.
    "thoda kam karo",
    "discount milega?",
    "best price?",
    "last price?",
    "₹4000 mein kar do",
    "aur kam?",
    "final rate batao",
    # …and the ways the same thing arrives in the transcripts.
    "Yaar thoda kam karo na, 4000 per night mein ho jayega?",
    "kuch kam ho jayega?",
    "koi discount hai?",
    "thoda sasta karo",
    "ye to bahut mehnga hai",
    "best rate kya doge",
    "3500 tak ho jayega?",
    "make it 4000",
    "कम कर दीजिए",
    "कोई छूट मिलेगी?",
    "any offer for a longer stay?",
    "cheaper option hai kya?",
])
def test_pushing_on_the_price_requires_the_engine(message):
    found = requirement(message, PRICED, ra.NEGOTIATION)
    assert found is not None, f"{message!r} raised no negotiation"
    assert found.tool == "negotiate_rate"
    assert found.forceable, "with a rate plan and dates on the session it must run"


@pytest.mark.parametrize("message,figure", [
    ("4000 mein ho jayega?", 4000),
    ("₹4,000 mein kar do", 4000),
    ("Rs 3500 per night", 3500),
    ("make it 4000", 4000),
    ("4500 tak kar sakte ho?", 4500),
    ("kar do 3800", 3800),
])
def test_the_guests_own_figure_reaches_the_engine_as_an_input(message, figure):
    """
    **The one path a guest-supplied number is allowed to travel, and what
    happens to it.** §20: a guest saying ₹4,000 must never become an approved
    ₹4,000. It is passed to `negotiate_rate` as `requested_nightly_rupees` —
    a question — and what may be *said* is whatever the engine answers. The
    number going in is not the number coming out, and nothing here lets the two
    be confused.
    """
    found = requirement(message, PRICED, ra.NEGOTIATION)
    assert found.arguments["requested_nightly_rupees"] == figure


@pytest.mark.parametrize("message", [
    "best price kya hai",
    "thoda kam karo",
    "discount milega",
])
def test_a_haggle_with_no_figure_names_none(message):
    """
    "Your best price" is not an offer of a number, and inventing one to pass to
    the engine would be the application doing exactly what the model is
    forbidden from doing.
    """
    found = requirement(message, PRICED, ra.NEGOTIATION)
    assert "requested_nightly_rupees" not in found.arguments


class TestANegotiationBeforeThereIsAnythingToNegotiate:
    """
    The ordinary first turn, and the one this module used to have no opinion on.

    **What was wrong.** This case returned `must_execute=False` with
    `negotiate_rate` named as its tool. Both halves were mistakes, and they were
    mistakes in opposite directions.

    Advisory was wrong because "best price kya de sakte ho?" before anything has
    been priced is not a rare state — it is how most haggles *open*. Leaving the
    deterministic layer with no opinion there left the model unsupervised at
    exactly the moment it was most likely to stop: the arbiter has nothing to
    weigh a handoff against when the turn is owed nothing, so an unjustified
    `request_human_handoff` went through unchallenged. That is the shape of the
    lost sale that started this work.

    Naming `negotiate_rate` was wrong because there is no rate plan to discount.
    Sending the model at that tool would be sending it at one that cannot run.

    **What the new assertions measure.** That the turn is owed a business
    action (`must_execute`), that the action is *discovery* rather than
    negotiation (`get_stay_options`), and — unchanged in substance from the
    assertion this replaces — that the application does not run it on a session
    with nothing in it. `forceable` is what "the application will run this
    itself" has always actually meant; `must_execute` means "this turn owes the
    guest something", and separating them is what lets a requirement be real
    without being executable.
    """

    def test_the_turn_owes_something_even_with_an_empty_session(self):
        found = requirement("thoda kam karo", {}, ra.NEGOTIATION)
        assert found is not None
        assert found.must_execute is True

    def test_and_what_it_owes_is_to_find_out_what_is_being_haggled_over(self):
        found = requirement("thoda kam karo", {}, ra.NEGOTIATION)
        assert found.tool == "get_stay_options"
        assert "negotiate_rate" in found.satisfied_by, (
            "a model that goes straight to the negotiation engine has answered "
            "the guest, and must not be told it still owes something"
        )

    def test_but_the_application_does_not_run_it_over_nothing(self):
        """
        Unchanged in substance. There is no honest set of arguments to invent
        for a stay nobody has described, and inventing dates would be the
        application doing what the model is forbidden from doing.
        """
        found = requirement("thoda kam karo", {}, ra.NEGOTIATION)
        assert found.arguments is None
        assert found.forceable is False

    def test_dates_without_a_rate_plan_are_enough_to_run_the_lookup(self):
        """
        The half that *is* constructible. A guest who has said which nights has
        given the system everything the search needs, so this stops being a
        thing to hope the model does.
        """
        found = requirement(
            "thoda kam karo",
            {"check_in": "2026-12-01", "check_out": "2026-12-03", "adults": 2},
            ra.NEGOTIATION,
        )
        assert found.forceable is True
        assert found.tool == "get_stay_options"
        assert found.arguments["check_in"] == "2026-12-01"
        assert found.arguments["check_out"] == "2026-12-03"

    def test_a_room_count_in_the_same_breath_is_carried_into_the_lookup(self):
        found = requirement(
            "5 kamre chahiye, thoda kam karo",
            {"check_in": "2026-12-01", "check_out": "2026-12-03"},
            ra.NEGOTIATION,
        )
        assert found.arguments["units"] == 5


def test_a_negotiation_takes_its_stay_from_structured_state_only():
    """
    §8. Every argument comes off the session, which `messaging/session.py`
    derives from tool results and never from prose. Nothing is read out of the
    guest's sentence except the figure they explicitly named.
    """
    found = requirement("thoda kam karo", PRICED, ra.NEGOTIATION)
    assert found.arguments["rate_plan_id"] == PRICED["rate_plan_id"]
    assert found.arguments["check_in"] == PRICED["check_in"]
    assert found.arguments["check_out"] == PRICED["check_out"]
    assert found.arguments["adults"] == 2


def test_a_haggle_that_also_changes_the_room_count_negotiates_the_new_count():
    """
    §5: "negotiation after group request". "5 kamre chahiye, thoda kam karo" is
    about five rooms, and negotiating the one room on the session would answer a
    question nobody asked — at a price the guest would then be quoted five times.
    """
    found = requirement("5 kamre chahiye, thoda kam karo", PRICED, ra.NEGOTIATION)
    assert found.arguments["units"] == 5


def test_a_group_and_a_haggle_in_one_message_are_both_required():
    """And the group comes first: know the scale before pricing it."""
    detected = intents("5 kamre chahiye, thoda kam karo", PRICED)
    assert detected.index(ra.GROUP_BOOKING) < detected.index(ra.NEGOTIATION)


@pytest.mark.parametrize("message", [
    "kitna hoga?",
    "price kya hai",
    "room chahiye",
    "breakfast ka kya rate hai",
    "kya aap airport pickup karte ho",
    "thanks, dekh ke batata hoon",
    "ok",
    # "kam" inside an unrelated word must not fire. This is why the vocabulary
    # is whole-word matched.
    "kamre ki photo bhejo",
    "kaam se aa raha hoon",
])
def test_what_is_not_a_negotiation(message):
    assert ra.NEGOTIATION not in intents(message, PRICED), f"{message!r} read as a haggle"


# ── the rest of the vocabulary ──────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "payment hogyi?",
    "payment ho gayi hai",
    "check karo payment hui ya nahi",
    "paise bhej diye",
    "booking confirm hui?",
    "have you received the payment",
])
def test_a_question_about_money_that_moved_is_answered_by_a_read(message):
    """
    The failure in the original transcript: the money had landed at 19:58:52,
    the reservation had confirmed, and twenty-six seconds later the guest was
    told it would confirm once the payment processed. `get_booking_status`
    takes no arguments and reads only this conversation's own booking, so there
    is nothing to get wrong in forcing it.
    """
    found = requirement(message, {}, ra.BOOKING_STATUS)
    assert found is not None
    assert found.tool == "get_booking_status"
    assert found.forceable
    assert found.arguments == {}


@pytest.mark.parametrize("message,intent", [
    ("cancel karna hai", ra.BOOKING_CANCELLATION),
    ("booking cancel kar do", ra.BOOKING_CANCELLATION),
    ("refund milega?", ra.BOOKING_CANCELLATION),
    ("paisa wapas ho jayega", ra.BOOKING_CANCELLATION),
    ("payment link bhej do", ra.PAYMENT),
    ("send me the payment link", ra.PAYMENT),
    ("upi se pay kar sakte hain?", ra.PAYMENT),
    ("dates change karni hain", ra.BOOKING_MODIFICATION),
    ("booking reschedule karni hai", ra.BOOKING_MODIFICATION),
    ("can I talk to a human", ra.ESCALATION),
    ("manager se baat karwao", ra.ESCALATION),
    ("kitna hoga total?", ra.QUOTE_REQUIRED),
    ("what is the price", ra.QUOTE_REQUIRED),
    ("10 September ko aana hai", ra.DATE_CHANGE),
    ("2 adults ke liye", ra.GUEST_COUNT_CHANGE),
])
def test_the_rest_of_the_vocabulary(message, intent):
    assert intent in intents(message), f"{message!r} did not raise {intent}"


def test_the_intents_are_the_ones_the_brief_asked_for():
    """§3's list, so a rename cannot silently drop one."""
    assert set(ra.INTENTS) == {
        "GROUP_BOOKING", "NEGOTIATION", "QUOTE_REQUIRED", "PAYMENT",
        "BOOKING_STATUS", "BOOKING_CANCELLATION", "BOOKING_MODIFICATION",
        "ESCALATION", "ROOM_SELECTION", "DATE_CHANGE", "GUEST_COUNT_CHANGE",
    }


@pytest.mark.parametrize("message", ["", "   ", None])
def test_nothing_is_required_of_nothing(message):
    assert ra.detect(message, {}) == []


def test_an_ordinary_question_requires_no_business_action():
    """
    The common case, and the one this must stay cheap on. A greeting, a
    thank-you and a question about parking are most of a hotel's inbox.
    """
    for message in ("hi", "hello", "thank you", "parking hai kya?",
                    "wifi password kya hai", "checkout time?"):
        assert not [r for r in ra.detect(message, {}) if r.must_execute], message


# ── satisfaction and the summary ────────────────────────────────────────────

class Outcome:
    """The two fields `unmet` reads off a ToolOutcome."""

    def __init__(self, tool: str, ok: bool = True):
        self.tool, self.ok = tool, ok


def test_a_tool_that_ran_discharges_its_requirement():
    reqs = ra.detect("5 kamre chahiye", {})
    assert ra.unmet(reqs, [Outcome("get_group_rates")]) == []


def test_a_tool_that_was_refused_discharges_nothing():
    """
    A `negotiate_rate` queued for approval has not answered the guest. Treating
    a refusal as a discharge is how a requirement disappears without being met —
    the same rule `obligations.satisfied_by` follows next door.
    """
    reqs = ra.detect("5 kamre chahiye", {})
    assert ra.unmet(reqs, [Outcome("get_group_rates", ok=False)]) == reqs


def test_the_summary_says_who_did_it():
    reqs = ra.detect("5 kamre chahiye", {})
    by_model = ra.summarise(reqs, [Outcome("get_group_rates")], forced=[])
    by_app = ra.summarise(reqs, [Outcome("get_group_rates")], forced=["get_group_rates"])
    nobody = ra.summarise(reqs, [], forced=[])

    assert ra.status_of(by_model) == "model"
    assert ra.status_of(by_app) == "forced"
    assert ra.status_of(nobody) == "unmet"
    assert ra.status_of(ra.summarise([], [], forced=[])) == "none"


def test_an_advisory_intent_left_undone_is_not_a_defect():
    """
    A model that answers "kitna hoga?" by asking which dates has not failed —
    it has done the right thing. So `QUOTE_REQUIRED` going unmet is reported
    separately from a required action going unmet, and only the second is a
    number worth alarming on.
    """
    reqs = ra.detect("kitna hoga?", {})
    summary = ra.summarise(reqs, [], forced=[])
    assert summary["unmet"] == []
    assert ra.QUOTE_REQUIRED in summary["observed_unmet"]
    assert ra.status_of(summary) == "none"


def test_at_most_two_actions_are_ever_forced():
    """
    A turn that forced four tools has stopped being a conversation. The guest
    waits the same time either way, and the cap is what keeps a long message
    from becoming a batch job.
    """
    busy = "5 kamre chahiye, thoda kam karo, payment link bhej do, cancel policy kya hai"
    forceable = [r for r in ra.detect(busy, PRICED) if r.forceable]
    assert len(forceable[:ra.MAX_FORCED]) <= 2
    assert ra.MAX_FORCED == 2


def test_one_requirement_per_intent():
    """The vocabularies overlap by design; a duplicate would be forced twice."""
    detected = ra.detect("5 rooms chahiye, 10 log, group booking hai", {})
    assert len(detected) == len({r.intent for r in detected})
