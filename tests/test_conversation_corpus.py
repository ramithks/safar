"""
Every scenario in the corpus, played end to end.

One test per scenario, so a failure names the conversation rather than the
category. What is asserted is in `tests/conversations/__init__.py`: the
requirement each message raised before any model ran, the tools that ran, the
tools that must not have, and what the session carried between turns.
"""
import pytest

from tests.conversations import check, play
from tests.conversations.corpus import ALL
# The fixture hotel: rooms, rate plans, group bands, and the availability RPCs.
# Reused rather than rebuilt — a second copy of a fixture this size is a second
# thing to keep in step, and the ids in `tests/conversations` are its ids.
from tests.test_golden_conversations import db  # noqa: F401


@pytest.mark.parametrize("scenario", ALL, ids=lambda s: f"{s.category[:1]}:{s.name}")
def test_conversation(scenario, db):  # noqa: F811
    problems = check(db, play(db, scenario))
    assert not problems, "\n  ".join([f"{scenario.name}:"] + problems)


def test_the_corpus_covers_the_categories_it_claims_to():
    """
    A corpus that quietly loses a category is a corpus that reports coverage it
    does not have.
    """
    covered = {s.category[:1] for s in ALL}
    assert covered >= set("ABCEFIJLN"), f"missing: {set('ABCEFIJLN') - covered}"


def test_every_scenario_asserts_something():
    """
    A scenario with no requirement, no tool and no session expectation passes
    whatever the system does.
    """
    empty = [
        s.name for s in ALL
        if not s.final_session and not s.never_ran
        and not any(t.requires or t.ran or t.forbids or t.session_holds
                    for t in s.turns)
    ]
    assert not empty, f"these assert nothing: {empty}"


# ── the two defects this corpus found on its first run ──────────────────────

class TestAForcedCallCannotUndoWhatTheTurnEstablished:
    """
    A guest changed their dates and the session went backwards.

    Turn three of `C:dates change after a quote`: the guest wrote *"actually
    <new dates> kar do"*, the model called `get_stay_options` with the new
    dates — correctly — and then the required-action layer forced
    `negotiate_rate`, whose arguments it had built from the **stored** session:
    the old dates. `patch_from` took the last value it saw, so the patch carried
    the dates the guest had just moved away from, and every later prompt told
    the model the guest was staying on nights they had already changed.

    Ordering was the wrong rule, and so is ignoring forced calls altogether:
    `get_group_rates` is forced with `units: 5` read straight out of "5 kamre
    chahiye", which is the freshest fact on the turn and must land. What
    separates them is not the tool but where the argument came from — the
    guest's sentence, or the session the guest is contradicting.
    """

    def _outcome(self, tool, source, **args):
        from app.services.ai.schemas import ToolOutcome

        return ToolOutcome(tool=tool, status="executed", input=args,
                           decision_source=source)

    def test_the_fresh_date_wins_over_the_forced_one(self):
        from app.services.messaging import session

        patch = session.patch_from([
            self._outcome("get_stay_options", "model",
                          check_in="2026-10-04", check_out="2026-10-06"),
            self._outcome("negotiate_rate", "application",
                          check_in="2026-09-24", check_out="2026-09-26"),
        ])
        assert patch["check_in"] == "2026-10-04"
        assert patch["check_out"] == "2026-10-06"

    def test_and_wins_regardless_of_the_order_they_arrive_in(self):
        """
        The forced call runs last today. Asserting the rule rather than the
        current ordering is what stops this coming back when it does not.
        """
        from app.services.messaging import session

        patch = session.patch_from([
            self._outcome("negotiate_rate", "application", check_in="2026-09-24"),
            self._outcome("get_stay_options", "model", check_in="2026-10-04"),
        ])
        assert patch["check_in"] == "2026-10-04"

    def test_a_forced_call_still_fills_what_nothing_else_supplied(self):
        """
        The half that must not be lost. "5 kamre chahiye" forces
        `get_group_rates` with units read out of the guest's own sentence, and
        no model call established it.
        """
        from app.services.messaging import session

        patch = session.patch_from([
            self._outcome("get_group_rates", "application", units=5),
        ])
        assert patch["units"] == 5

    def test_two_model_calls_still_follow_each_other_in_order(self):
        """
        Within the model's own calls the last one wins, which is right: a model
        that searches one week and then quotes another has changed its mind
        about the same turn.
        """
        from app.services.messaging import session

        patch = session.patch_from([
            self._outcome("search_availability", "model", check_in="2026-10-04"),
            self._outcome("get_quote", "model", check_in="2026-10-11"),
        ])
        assert patch["check_in"] == "2026-10-11"


class TestKarDoIsJustDoIt:
    """
    `kar do` was in the haggling vocabulary, and it is simply "do it".

    Every one of these was read as a request for a discount — each of which
    opens a negotiation, takes a hold, and can volunteer a rate to a guest who
    never asked. Nothing is lost by removing it: every genuine haggle in the
    set below is matched by a word that is actually about lowering a price.
    """

    @pytest.mark.parametrize("message", [
        "book kar do", "cancel kar do", "confirm kar do",
        "check in 2 baje kar do", "breakfast add kar do", "ek room kar do",
        "4 tareekh se 6 tareekh kar do",
    ])
    def test_doing_a_thing_is_not_haggling(self, message):
        from app.services.ai import required_actions

        assert not required_actions.wants_discount(message)

    @pytest.mark.parametrize("message", [
        "kam kar do", "thoda kam kar do", "rate kam kardo",
        "discount kar do", "4000 kar do", "adjust kar do",
    ])
    def test_and_lowering_a_price_still_is(self, message):
        from app.services.ai import required_actions

        assert required_actions.wants_discount(message)
