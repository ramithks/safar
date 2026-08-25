"""
A guest corrects a fact, and everything computed from it stops being known.

**The failure this exists for.** The session is derived state, and until now
exactly one input to it was watched: `_superseded` noticed new *dates* and
`move_dates` dropped the priced fields. Every other correction a guest can make
went unhandled, and each one leaves the same residue —

    "actually 3 rooms, not 2"      the quote for two rooms stays on the session
    "make it 4 adults"             the same quote, priced for two, stays
    "the Garden Standard then"     the rate plan from the Deluxe stays, and the
                                   next quote is asked for a plan belonging to a
                                   different room

— a `quote_id` that is true of a stay the guest has just stopped describing.
`prompts._live_quote` then puts it in the next system prompt as *"there is a live
quote for ₹11,400"*, and the model offers a three-person party a price
calculated for two, on a room that may not sleep three.

**Declared once rather than rediscovered per field.** `session.DERIVED_FROM` says
what each fact is only true of; `corrections` says what the guest actually
changed, as opposed to filled in; `invalidated_by` walks the graph transitively,
because correcting a room invalidates the rate plan, which invalidates the quote,
which invalidates the negotiation and the payment state.
"""
import pytest

from app.services.messaging import session
from tests.fake_supabase import FakeDb

CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"

QUOTED = {
    "check_in": "2026-09-10", "check_out": "2026-09-14",
    "adults": 2, "children": 0, "units": 2,
    "room_type_id": "rt-deluxe", "rate_plan_id": "rp-cp",
    "quote_id": "q-1", "stage": "quoted", "language": "hinglish",
}


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {"id": CONVERSATION, "guest_profile": dict(QUOTED)})
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def profile(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


# ── the graph itself ────────────────────────────────────────────────────────

class TestWhatDependsOnWhat:
    def test_a_quote_is_only_true_of_the_stay_it_priced(self):
        for field in ("check_in", "check_out", "units", "adults", "children",
                      "room_type_id", "rate_plan_id"):
            assert "quote_id" in session.invalidated_by({field}), field

    def test_the_graph_is_walked_transitively(self):
        """
        Correcting the room invalidates the rate plan, which invalidates the
        quote, which invalidates the negotiation and the payment state. One hop
        would leave a quote priced against a rate plan we had just dropped.
        """
        dead = session.invalidated_by({"room_type_id"})
        assert {"rate_plan_id", "quote_id", "negotiation", "payment", "stage"} <= dead

    def test_the_room_is_never_invalidated_by_anything(self):
        """
        A guest who moves their dates has not changed their mind about the room,
        and dropping it would make the agent ask a question they answered two
        turns ago — a regression this codebase already fixed once. It is carried
        forward unverified instead.
        """
        for field in session.CORRECTABLE - {"room_type_id"}:
            assert "room_type_id" not in session.invalidated_by({field}), field

    def test_nothing_depends_on_the_guests_language(self):
        """The one thing on this column about the person rather than the trip."""
        for field in session.CORRECTABLE:
            assert "language" not in session.invalidated_by({field})

    def test_every_declared_source_is_itself_a_correctable_fact_or_derived(self):
        """
        A dependency on something nobody ever sets is a dependency that never
        fires, which is worse than no dependency: it reads as protection.
        """
        known = session.CORRECTABLE | set(session.DERIVED_FROM)
        for field, sources in session.DERIVED_FROM.items():
            assert sources <= known, f"{field} depends on unknown {sources - known}"


class TestWhatCountsAsACorrection:
    def test_filling_a_blank_is_not_a_correction(self, db):
        """
        The guest naming their dates for the first time invalidates nothing —
        that is the conversation progressing.
        """
        assert session.corrections({}, {"units": 2}) == set()
        assert session.corrections({"units": None}, {"units": 2}) == set()

    def test_the_same_value_again_is_not_a_correction(self, db):
        assert session.corrections({"units": 2}, {"units": 2}) == set()
        assert session.corrections({"units": 2}, {"units": "2"}) == set()

    def test_a_different_value_is(self, db):
        assert session.corrections({"units": 2}, {"units": 3}) == {"units"}

    def test_only_facts_a_guest_can_correct(self, db):
        """
        `stage` and `quote_id` are ours — we derived them — and a guest does not
        correct them. Treating a derived field as a correction would make the
        graph trigger on its own output.
        """
        assert session.corrections({"stage": "quoted"}, {"stage": "booked"}) == set()
        assert session.corrections({"quote_id": "q-1"}, {"quote_id": "q-2"}) == set()


# ── through `merge`, which is what production calls ─────────────────────────

class TestARoomCountCorrection:
    def test_the_quote_for_two_rooms_does_not_survive_three(self, db):
        session.merge(CONVERSATION, {"units": 3})
        now = profile(db)
        assert now["units"] == 3
        assert "quote_id" not in now, "a quote priced for two rooms survived"
        assert "stage" not in now

    def test_but_the_stay_itself_does(self, db):
        session.merge(CONVERSATION, {"units": 3})
        now = profile(db)
        assert now["check_in"] == "2026-09-10"
        assert now["room_type_id"] == "rt-deluxe"
        assert now["language"] == "hinglish"

    def test_and_the_room_is_marked_for_rechecking(self, db):
        """
        Three rooms of a type that had two left is a different question from two
        rooms of it. The preference survives; the availability is not assumed.
        """
        session.merge(CONVERSATION, {"units": 3})
        assert profile(db)["room_needs_recheck"] is True


class TestAPartySizeCorrection:
    def test_a_quote_for_two_adults_does_not_survive_four(self, db):
        session.merge(CONVERSATION, {"adults": 4})
        now = profile(db)
        assert now["adults"] == 4
        assert "quote_id" not in now

    def test_children_arriving_invalidates_the_price_too(self, db):
        """
        A child is a person in a room, and several rate plans price them.
        """
        session.merge(CONVERSATION, {"children": 2})
        assert "quote_id" not in profile(db)


class TestARoomChange:
    def test_choosing_a_different_room_drops_the_old_rooms_rate_plan(self, db):
        """
        The subtlest of the three, and the one with a wrong *tool call* at the
        end of it rather than a wrong price: a rate plan belongs to a room type,
        so `rp-cp` from the Deluxe is not a plan the Garden Standard has. Kept,
        it goes into the next `get_quote` as an argument and the tool refuses.
        """
        session.merge(CONVERSATION, {"room_type_id": "rt-garden"})
        now = profile(db)
        assert now["room_type_id"] == "rt-garden"
        assert "rate_plan_id" not in now
        assert "quote_id" not in now

    def test_and_the_new_room_is_not_marked_for_rechecking(self, db):
        """
        They just chose it from what they were shown. Marking it doubtful would
        make the agent re-ask about a room the guest picked one message ago.
        """
        session.merge(CONVERSATION, {"room_type_id": "rt-garden"})
        assert "room_needs_recheck" not in profile(db)


class TestWhatMustNotChange:
    def test_a_turn_about_breakfast_invalidates_nothing(self, db):
        session.merge(CONVERSATION, {"last_business_action": "get_meal_info"})
        now = profile(db)
        assert now["quote_id"] == "q-1"
        assert now["rate_plan_id"] == "rp-cp"

    def test_a_quote_arriving_for_the_stay_on_the_session_is_kept(self, db):
        """
        The ordinary case, and the one a graph like this can most easily break:
        `get_quote` writes back the same dates and party it was called with, and
        nothing about that is a correction.
        """
        session.merge(CONVERSATION, {
            "check_in": "2026-09-10", "check_out": "2026-09-14",
            "adults": 2, "units": 2, "quote_id": "q-2",
        })
        assert profile(db)["quote_id"] == "q-2"

    def test_new_dates_still_rebuild_the_stay_rather_than_pruning_it(self, db):
        """
        Dates keep their own path. They are the one correction that also means
        "a different trip", so the party size goes too — a guest asking about
        October has not implicitly kept September's four adults.
        """
        session.merge(CONVERSATION, {"check_in": "2026-10-20",
                                     "check_out": "2026-10-22"})
        now = profile(db)
        assert "quote_id" not in now
        assert "adults" not in now, "a new trip does not inherit the old party"
        assert now["room_type_id"] == "rt-deluxe"
        assert now["room_needs_recheck"] is True
