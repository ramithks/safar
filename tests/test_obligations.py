"""
Obligation tracking: what the guest asked, and what closed it.

The value of this module is entirely in its vocabulary, so most of what is here
is the vocabulary being exercised against phrasings real guests actually used —
Hindi, Hinglish and English — plus the two properties that make it safe to run
on every turn: it never opens something nothing can close, and it drains.
"""
import pytest

from app.services.messaging import obligations as ob
from app.services.messaging import session


class Outcome:
    """The shape `patch_from` and `satisfied_by` read."""

    def __init__(self, tool, ok=True, output=None, arguments=None):
        self.tool = tool
        self.ok = ok
        self.output = output
        self.input = arguments or {}


# The message from §2, verbatim.
SIX_PART = (
    "10 Sep ko 2 rooms chahiye, breakfast included hai? "
    "airport pickup bhi chahiye aur best price batao."
)


class TestTheBriefsExample:
    def test_it_finds_every_part_of_the_six_part_message(self):
        found = set(ob.detect(SIX_PART))
        assert {"availability", "price", "negotiation", "meals", "services"} <= found

    def test_answering_two_parts_leaves_the_rest_open(self):
        state = ob.open_from(SIX_PART, [Outcome("search_availability"), Outcome("get_quote")])
        # Availability and price are discharged; the other three are not.
        assert "availability" not in state and "price" not in state
        assert {"negotiation", "meals", "services"} <= set(state)

    def test_the_prompt_line_names_them(self):
        state = ob.open_from(SIX_PART, [Outcome("search_availability"), Outcome("get_quote")])
        line = ob.describe({"obligations": state})
        assert "meals or breakfast" in line
        assert "a transfer or an extra" in line
        assert "a better price" in line

    def test_they_drain_as_they_are_answered(self):
        state = ob.open_from(SIX_PART, [Outcome("search_availability"), Outcome("get_quote")])
        state = ob.open_from("ok", [Outcome("get_meal_info")], state)
        assert "meals" not in state
        state = ob.open_from("ok", [Outcome("list_services")], state)
        assert "services" not in state
        state = ob.open_from("ok", [Outcome("negotiate_rate")], state)
        assert state == {}
        assert ob.describe({"obligations": state}) is None


class TestVocabulary:
    @pytest.mark.parametrize("message,expected", [
        ("photos bhejo", "photos"),
        ("can I see some pictures?", "photos"),
        ("breakfast included hai?", "meals"),
        ("khana milega?", "meals"),
        ("airport pickup ho jayega?", "services"),
        ("station se gaadi bhej sakte ho?", "services"),
        ("main 10 baje check-in kar sakta hoon?", "early_checkin"),
        ("late checkout possible?", "early_checkin"),
        ("cancel karne pe paisa wapas milega?", "cancellation"),
        ("is it refundable?", "cancellation"),
        ("thoda kam karo na bhai", "negotiation"),
        ("what's your best price?", "negotiation"),
        ("we are 20 people for a wedding", "group"),
        ("payment hogyi", "payment"),
        ("kitna hoga?", "price"),
        ("book kar do", "booking"),
        ("kahan pe hai hotel?", "location"),
        ("wifi hai?", "amenities"),
    ])
    def test_real_phrasings(self, message, expected):
        assert expected in ob.detect(message), ob.detect(message)

    @pytest.mark.parametrize("message", ["ok", "yes", "thanks!", "Sahil", "👍", "", "   "])
    def test_an_acknowledgement_raises_nothing(self, message):
        assert ob.detect(message) == []

    def test_milega_alone_is_not_an_availability_question(self):
        # The false positive this was written for: a guest asking whether they
        # get their *money* back was recorded as asking whether a room was free,
        # which would have the agent checking availability for somebody trying
        # to cancel.
        assert "availability" not in ob.detect("cancel karne pe paisa wapas milega?")
        assert "availability" not in ob.detect("refund kab tak milega")
        # …and still fires when it is about a room.
        assert "availability" in ob.detect("room milega 10 tarikh ko?")
        assert "availability" in ob.detect("2 kamre chahiye")

    def test_ac_does_not_fire_on_back(self):
        # `_words` uses boundaries for exactly this. Without them half the
        # vocabulary matches prose containing the letters.
        assert "amenities" not in ob.detect("I will come back later")
        assert "amenities" in ob.detect("is there AC in the room?")


class TestProperties:
    def test_every_topic_has_something_that_closes_it(self):
        # A topic nothing can discharge is an obligation that never goes away,
        # which is worse than not tracking it at all.
        for topic in ob.TOPICS:
            assert topic.closed_by, topic.key

    def test_every_closing_tool_actually_exists(self):
        from app.services.ai import registry

        registry.load_all()
        for topic in ob.TOPICS:
            missing = topic.closed_by - set(registry.REGISTRY)
            assert not missing, (topic.key, sorted(missing))

    def test_topic_keys_are_unique(self):
        keys = [t.key for t in ob.TOPICS]
        assert len(keys) == len(set(keys))

    def test_a_failed_tool_answers_nothing(self):
        state = ob.open_from("breakfast included hai?", [Outcome("get_meal_info", ok=False)])
        assert "meals" in state

    def test_obligations_age_out(self):
        state = ob.open_from("breakfast?", [])
        for _ in range(ob.MAX_AGE_TURNS + 1):
            state = ob.open_from("ok", [], state)
        assert state == {}

    def test_re_asking_resets_the_clock(self):
        state = ob.open_from("breakfast?", [])
        for _ in range(ob.MAX_AGE_TURNS - 1):
            state = ob.open_from("ok", [], state)
        state = ob.open_from("breakfast??", [], state)
        assert state["meals"] == 0

    def test_one_message_cannot_open_more_than_the_cap(self):
        everything = ("rooms chahiye price breakfast lunch dinner photos airport "
                      "pickup discount cancel wifi payment book location group wedding")
        assert len(ob.detect(everything)) <= ob.MAX_PER_MESSAGE

    def test_asking_and_answering_in_one_turn_opens_nothing(self):
        # A guest who asks about breakfast in the message that triggers
        # get_meal_info has been answered on that turn.
        state = ob.open_from("breakfast included hai?", [Outcome("get_meal_info")])
        assert "meals" not in state

    def test_a_corrupt_stored_value_is_dropped_rather_than_crashing(self):
        state = ob.open_from("ok", [], {"meals": "not a number", "bogus_topic": 1})
        assert state == {}

    def test_describe_is_none_when_there_is_nothing(self):
        assert ob.describe(None) is None
        assert ob.describe({}) is None
        assert ob.describe({"obligations": {}}) is None
        assert ob.describe({"obligations": "corrupt"}) is None


# ── session state, extended ──────────────────────────────────────────────────


class TestSessionExtensions:
    def test_a_negotiation_verdict_is_remembered(self):
        patch = session.patch_from([
            Outcome("negotiate_rate", output={"verdict": "counter"})
        ])
        assert patch["negotiation"] == "counter"

    def test_asking_the_hotel_records_that_we_are_waiting(self):
        patch = session.patch_from([
            Outcome("escalate_to_team", output={
                "escalationId": "e1", "kind": "discount_request", "teamNotified": True,
            })
        ])
        assert patch["awaiting_hotel"] == {
            "id": "e1", "kind": "discount_request", "notified": True,
        }

    def test_a_confirmed_booking_stops_the_waiting(self):
        patch = session.patch_from([
            Outcome("get_booking_status", output={"state": "CONFIRMED"})
        ])
        assert patch["payment"] == "CONFIRMED"
        assert patch["awaiting_hotel"] is None

    def test_a_pending_payment_does_not_stop_the_waiting(self):
        patch = session.patch_from([
            Outcome("get_booking_status", output={"state": "PAYMENT_PENDING"})
        ])
        assert patch["payment"] == "PAYMENT_PENDING"
        assert "awaiting_hotel" not in patch

    def test_a_new_stay_clears_the_negotiation_and_the_waiting(self):
        # A guest back in October about different nights is not still
        # mid-negotiation on September's, and telling them we are waiting on a
        # question nobody asked about this trip would be a non-sequitur.
        stale = {
            "check_in": "2026-09-10", "negotiation": "counter",
            "awaiting_hotel": {"id": "e1"}, "obligations": {"meals": 1},
            "language": "hinglish",
        }
        kept = session.clear_stay(stale)
        assert kept == {"language": "hinglish"}
