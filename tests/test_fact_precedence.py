"""
Which source wins when two of them disagree about the same fact.

**The rule was positional, and positional is fragile.** A guest's own words beat
a tool argument because `stated_facts` happened to be applied last in the
function; a forced call filled gaps only because a set of field names was
threaded through the loop by hand. Both were correct and both would have broken
the first time somebody reordered two blocks — which, on the evidence of this
codebase, is a matter of when.

Named instead, so the rule is a table:

    GUEST_SAID     the guest's own sentence. Nothing is better evidence of what
                   the guest wants than what they just said.
    TOOL_RESULT    an id or a name a tool returned — the system's own answer
                   about its own records.
    TOOL_ARGUMENT  what the *model* called a tool with: a reading of the guest,
                   one remove away.
    CARRIED        what a *forced* call was invoked with, which the application
                   built out of the session — the session repeating itself, not
                   new evidence.

The last one is the subtle one and the reason this exists at all: a forced
`negotiate_rate` carrying September's dates must not overwrite the October ones
the guest just gave, and a forced `get_group_rates` carrying `units: 5` read out
of "5 kamre chahiye" must still land.
"""
import pytest

from app.services.ai.schemas import ToolOutcome
from app.services.messaging import session
from tests.fake_supabase import FakeDb

CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"


def outcome(tool, source="model", output=None, **args):
    return ToolOutcome(tool=tool, status="executed", input=args,
                       output=output or {}, decision_source=source)


class TestTheTable:
    def test_the_ranks_are_ordered_as_stated(self):
        rank = session.SOURCE_RANK
        assert rank[session.GUEST_SAID] > rank[session.TOOL_RESULT]
        assert rank[session.TOOL_RESULT] > rank[session.TOOL_ARGUMENT]
        assert rank[session.TOOL_ARGUMENT] > rank[session.CARRIED]

    def test_a_lower_source_never_overwrites_a_higher_one(self):
        facts = session._Facts()
        facts.set("units", 3, session.GUEST_SAID)
        facts.set("units", 2, session.TOOL_ARGUMENT)
        facts.set("units", 1, session.CARRIED)
        assert facts.values["units"] == 3

    def test_a_higher_source_does_overwrite_a_lower_one(self):
        facts = session._Facts()
        facts.set("units", 2, session.CARRIED)
        facts.set("units", 3, session.GUEST_SAID)
        assert facts.values["units"] == 3

    def test_the_same_rank_overwrites(self):
        """
        Within a rank the last value wins, which keeps the behaviour that was
        there before: a model that searches one week and then quotes another
        has changed its mind about the same turn.
        """
        facts = session._Facts()
        facts.set("check_in", "2026-09-01", session.TOOL_ARGUMENT)
        facts.set("check_in", "2026-09-08", session.TOOL_ARGUMENT)
        assert facts.values["check_in"] == "2026-09-08"

    def test_a_none_never_claims_a_field(self):
        """
        Otherwise a tool called without a date would claim `check_in` at its own
        rank and lock out everything below it.
        """
        facts = session._Facts()
        facts.set("units", None, session.GUEST_SAID)
        facts.set("units", 2, session.TOOL_ARGUMENT)
        assert facts.values["units"] == 2


class TestThroughPatchFrom:
    def test_the_guest_beats_a_forced_calls_stale_arguments(self):
        patch = session.patch_from(
            [outcome("get_group_rates", "application", units=2, adults=2)],
            "Sorry, 4 log hain")
        assert patch["adults"] == 4

    def test_the_guest_beats_the_models_own_arguments(self):
        patch = session.patch_from(
            [outcome("get_stay_options", "model", units=2, adults=2)],
            "Actually 3 kamre chahiye")
        assert patch["units"] == 3

    def test_a_forced_call_still_fills_what_nothing_else_claimed(self):
        patch = session.patch_from(
            [outcome("get_group_rates", "application", units=5)], None)
        assert patch["units"] == 5

    def test_a_forced_call_loses_to_the_models_fresh_argument(self):
        """
        The original bug, restated in the new vocabulary. Order-independent now:
        the forced call is CARRIED whichever way round the outcomes arrive.
        """
        for order in (
            [outcome("get_stay_options", "model", check_in="2026-10-04"),
             outcome("negotiate_rate", "application", check_in="2026-09-24")],
            [outcome("negotiate_rate", "application", check_in="2026-09-24"),
             outcome("get_stay_options", "model", check_in="2026-10-04")],
        ):
            assert session.patch_from(order, None)["check_in"] == "2026-10-04"

    def test_a_tool_result_beats_a_tool_argument(self):
        """
        `get_quote` called with one room and returning another — the system's
        own record of what it priced is the better fact.
        """
        patch = session.patch_from([
            outcome("get_stay_options", "model", room_type_id="rt-asked"),
            outcome("get_quote", "model", output={"roomTypeId": "rt-priced"},
                    room_type_id="rt-asked"),
        ], None)
        assert patch["room_type_id"] == "rt-priced"

    def test_and_the_guest_beats_even_that(self):
        rooms = [{"id": "rt-garden", "name": "Garden Standard"}]
        patch = session.patch_from(
            [outcome("get_quote", "model", output={"roomTypeId": "rt-deluxe"})],
            "nahi, Garden Standard chahiye", rooms=rooms)
        assert patch["room_type_id"] == "rt-garden"


# ── the record it leaves ────────────────────────────────────────────────────

@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {"id": CONVERSATION, "guest_profile": {}})
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def profile(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


class TestTheProvenanceRecord:
    def test_it_says_where_each_fact_came_from(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [outcome("get_stay_options", "model", check_in="2026-09-10")],
            "3 kamre chahiye"))
        sources = profile(db)["_sources"]
        assert sources["units"]["source"] == session.GUEST_SAID
        assert sources["check_in"]["source"] == session.TOOL_ARGUMENT

    def test_it_says_which_turn(self, db):
        session.merge(CONVERSATION, session.patch_from([], "2 kamre chahiye"))
        session.merge(CONVERSATION, session.patch_from([], "3 kamre chahiye"))
        sources = profile(db)["_sources"]
        assert sources["units"]["turn"] == 2, "the correction is dated to its turn"

    def test_a_fact_that_goes_takes_its_provenance_with_it(self, db):
        """
        Otherwise the record grows a tail of facts the session no longer holds,
        and reading it six weeks later means checking each entry still applies.
        """
        # The room count has to be *set* first, or the second turn is filling a
        # blank rather than correcting — and filling a blank invalidates
        # nothing, which is the whole point of `corrections`.
        session.merge(CONVERSATION, session.patch_from(
            [outcome("get_quote", "model", output={"quoteId": "q-1"},
                     check_in="2026-09-10", check_out="2026-09-14", units=2)],
            None))
        assert "quote_id" in profile(db)["_sources"]

        session.merge(CONVERSATION, session.patch_from(
            [], "3 kamre chahiye"))
        assert "quote_id" not in profile(db)["_sources"], (
            "the dropped quote left its provenance behind")

    def test_a_turn_that_establishes_nothing_records_nothing(self, db):
        assert session.patch_from([], "breakfast included hai?") == {}

    def test_the_record_is_private_to_the_session(self, db):
        """
        It is a debugging aid, not something the prompt spends tokens on. The
        underscore is the convention this column already uses for keys `merge`
        consumes.
        """
        session.merge(CONVERSATION, session.patch_from([], "3 kamre chahiye"))
        told = session.describe({"guest_profile": profile(db)})
        assert told is None or "_sources" not in told


class TestConfidence:
    """
    The third of the five things §3 asks for per entity — source, confidence,
    turn, canonical value, dependencies. The other four were already here.
    """

    def test_a_room_read_exactly_and_one_read_fuzzily_are_recorded_apart(self, db):
        rooms = [{"id": "rt-garden", "name": "Garden Standard"}]

        session.merge(CONVERSATION, session.patch_from(
            [], "Garden Standard chahiye", rooms=rooms))
        assert profile(db)["_sources"]["room_type_id"]["confidence"] == "exact"

        session.merge(CONVERSATION, session.patch_from(
            [], "gardern standrd chahiye", rooms=rooms))
        assert profile(db)["_sources"]["room_type_id"]["confidence"] == "fuzzy"

    def test_a_spelled_out_date_and_an_inferred_one_are_recorded_apart(self, db):
        from datetime import date

        today = date(2026, 8, 25)
        session.merge(CONVERSATION, session.patch_from(
            [], "25 September to 28 September", today=today))
        assert profile(db)["_sources"]["check_in"]["confidence"] == "explicit"

        session.merge(CONVERSATION, session.patch_from(
            [], "2nd to 5th", today=today))
        assert profile(db)["_sources"]["check_in"]["confidence"] == "inferred"

    def test_anything_a_tool_merely_carried_is_stated(self, db):
        session.merge(CONVERSATION, session.patch_from(
            [outcome("get_stay_options", "model", check_in="2026-09-10")], None))
        assert profile(db)["_sources"]["check_in"]["confidence"] == "stated"

    def test_every_recorded_fact_has_all_five(self, db):
        """
        The completeness check. A record missing a field is one somebody has to
        remember the shape of.
        """
        from datetime import date

        rooms = [{"id": "rt-garden", "name": "Garden Standard"}]
        session.merge(CONVERSATION, session.patch_from(
            [], "Garden Standard, 2nd to 5th, 3 kamre",
            rooms=rooms, today=date(2026, 8, 25)))
        now = profile(db)
        for field, entry in now["_sources"].items():
            assert entry["source"] in session.SOURCE_RANK, field   # source
            assert entry["confidence"], field                      # confidence
            assert entry["turn"] >= 1, field                       # turn
            assert field in now, field                             # canonical value
        # dependencies
        assert session.DERIVED_FROM["quote_id"]
