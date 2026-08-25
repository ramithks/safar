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

    def test_moving_the_nights_alone_keeps_the_party(self, db):
        """
        **This assertion was reversed, and the reversal is the fix.**

        It read `assert "adults" not in now` — a date change dropped the party
        size, on the reasoning that a guest asking about October has not
        implicitly kept September's four adults. Sound for the case it was
        written for, and wrong for a live correction: a guest three turns in who
        says "actually 3 rooms" and then "and the 2nd to the 5th" has not
        changed their mind about the rooms, and dropping the count made the next
        quote price one room and the agent ask how many they wanted — the
        question they had just answered. Found by an eleven-turn journey.

        The two cases are told apart by the patch rather than by a clock, and
        the test below pins the other half.
        """
        session.merge(CONVERSATION, {"check_in": "2026-10-20",
                                     "check_out": "2026-10-22"})
        now = profile(db)
        assert now["check_in"] == "2026-10-20"
        assert "quote_id" not in now, "the September price survived"
        assert now["adults"] == 2, "a correction to the nights lost the party"
        assert now["units"] == 2
        assert now["room_type_id"] == "rt-deluxe"
        assert now["room_needs_recheck"] is True

    def test_but_re_describing_the_stay_drops_the_old_party_whole(self, db):
        """
        The other half. A guest stating a *new* size is describing a different
        stay, and an unstated field — September's one child — must not survive
        into an October request that mentioned only adults.
        """
        db.rows("conversations")[0]["guest_profile"]["children"] = 1
        session.merge(CONVERSATION, {
            "check_in": "2026-10-20", "check_out": "2026-10-22",
            "adults": 2, "units": 1,
        })
        now = profile(db)
        assert now["adults"] == 2
        assert now["units"] == 1
        assert "children" not in now, "the old trip's child came along"


# ── the guest's own words outrank any tool argument ─────────────────────────

class TestWhatTheGuestSaidBeatsWhatTheToolWasCalledWith:
    """
    **Found by mutation testing.** Deleting the line that applies
    `stated_facts` broke nothing, because every test above reached the session
    through a tool argument that happened to carry the same number.

    That is exactly the failure the line exists for. Session facts were written
    from tool *arguments* — whatever the model passed — so "Actually 4 log hain"
    updated the party only when some tool ran carrying it. On a turn where the
    model answered in prose, or where a *forced* call was built from the stale
    session, the correction did not land, and the next prompt went on describing
    the stay the guest had just changed.
    """

    def _outcome(self, tool, source, **args):
        from app.services.ai.schemas import ToolOutcome

        return ToolOutcome(tool=tool, status="executed", input=args,
                           decision_source=source)

    def test_a_correction_lands_on_a_turn_where_no_tool_ran(self, db):
        """
        The plainest case: the model says "theek hai" and calls nothing. The
        guest still said four.
        """
        session.merge(CONVERSATION, session.patch_from([], "Sorry, 4 log hain"))
        assert profile(db)["adults"] == 4

    def test_it_beats_a_forced_call_built_from_the_stale_session(self, db):
        """
        The case that actually bites. A forced tool's arguments come from the
        session — which still says two — so without this the turn would write
        the old number back over the guest's correction, and the session would
        go backwards on the very turn they corrected it.
        """
        patch = session.patch_from(
            [self._outcome("get_group_rates", "application", units=2, adults=2)],
            "Sorry, 4 log hain",
        )
        assert patch["adults"] == 4
        session.merge(CONVERSATION, patch)
        assert profile(db)["adults"] == 4

    def test_and_beats_the_model_own_arguments_too(self, db):
        """
        A model that misreads the count and calls a tool with the old one does
        not get to overrule the sentence it misread.
        """
        patch = session.patch_from(
            [self._outcome("get_stay_options", "model", units=2, adults=2)],
            "Actually 3 kamre chahiye",
        )
        assert patch["units"] == 3

    def test_and_the_price_derived_from_the_old_count_goes_with_it(self, db):
        """
        The two mechanisms composed, which is the whole point: the correction
        arrives from the guest's words, and the graph drops what depended on it.
        """
        session.merge(CONVERSATION, session.patch_from([], "Sorry, 4 log hain"))
        now = profile(db)
        assert now["adults"] == 4
        assert "quote_id" not in now

    def test_a_message_that_states_nothing_changes_nothing(self, db):
        """
        Most turns are a question. This must not write on every one of them.
        """
        assert session.patch_from([], "Breakfast included hai?") == {}
        assert session.patch_from([], "") == {}
        assert session.patch_from([], None) == {}
