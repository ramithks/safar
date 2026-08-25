"""
A negotiation, from the hotel's configured rule to the guest's answer.

**Why this file exists rather than more unit tests.** The bug that started this
work was not in any branch of the negotiation engine — every branch was tested.
It was that the *read* of the room's own rule never happened, and the engine
quietly used the hotel-wide ceiling instead. A unit test of `decide()` passes
either way, because `decide()` is handed the rules.

So these run the whole path — a configured rule, a quote, a negotiation, a
counter, an approval where the rung requires one — and assert **which rule was
used**, not merely that the call succeeded. Two room types with different rules,
so "the right rule applied" is a claim that can fail.
"""
import asyncio
from datetime import date, timedelta

import pytest

from app.services.ai import registry, runtime
from app.services.ai.providers import EchoProvider
from app.services.pms import negotiation
from tests.test_golden_conversations import (  # noqa: F401 — the hotel fixture
    CHECK_IN, CHECK_OUT, OTHER_PLAN, OTHER_ROOM, PARTNER, PROPERTY, RATE_PLAN,
    ROOM_TYPE, ctx_for, db,
)

registry.load_all()


def run(coro):
    return asyncio.run(coro)


def rule(scope, **kw):
    row = {"id": f"r-{scope}-{kw.get('room_type_id') or kw.get('rate_plan_id') or 'all'}",
           "property_id": PROPERTY, "scope": scope, "enabled": True,
           "room_type_id": None, "rate_plan_id": None,
           "floor_pct": None, "floor_paise": None, "target_pct": None,
           "target_paise": None, "ceiling_paise": None,
           "auto_pct": None, "approval_pct": None,
           "valid_from": None, "valid_to": None, "days_of_week": None,
           "blackout_dates": None, "min_nights": None, "min_units": None}
    row.update(kw)
    return row


def haggle(db, message, *, room, plan, autonomy=3):
    """One negotiating turn on a conversation that already has a quote."""
    db.rows("conversations")[0]["guest_profile"] = {
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT), "adults": 2,
        "units": 1, "room_type_id": room, "rate_plan_id": plan, "stage": "quoted",
    }
    return run(runtime.run_turn(
        ctx_for(db, autonomy_level=autonomy), message,
        provider=EchoProvider(["Main dekhta hoon.", "Ye best hai."])))


def negotiated(result):
    return [a for a in result.actions if a.tool == "negotiate_rate"]


def provenance(result):
    return ((result.requirements or {}).get("provenance") or {}).get("negotiation") or []


class TestTheRightRuleForTheRightRoom:
    """
    Two rooms, two rules. The assertion is which one applied — the thing a unit
    test of `decide()` cannot make.
    """

    def _two_rooms_two_rules(self, db):
        db.seed("negotiation_rules",
                rule("property", auto_pct=10),
                rule("room_type", room_type_id=ROOM_TYPE, auto_pct=5),
                rule("room_type", room_type_id=OTHER_ROOM, auto_pct=20))

    def test_the_deluxes_own_rule_prices_a_deluxe(self, db):
        self._two_rooms_two_rules(db)
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN)
        assert negotiated(result), "the engine was never asked"
        assert provenance(result)[0]["rule_source"] == negotiation.ROOM_TYPE_RULE

    def test_and_the_gardens_own_rule_prices_a_garden(self, db):
        self._two_rooms_two_rules(db)
        result = haggle(db, "thoda kam kar do", room=OTHER_ROOM, plan=OTHER_PLAN)
        assert provenance(result)[0]["rule_source"] == negotiation.ROOM_TYPE_RULE

    def test_the_two_rooms_get_different_answers(self, db):
        """
        The proof that the scoping is real rather than incidental. A 5% room and
        a 20% room cannot come back with the same counter-offer.
        """
        self._two_rooms_two_rules(db)
        strict = negotiated(haggle(db, "4000 mein ho jayega?",
                                   room=ROOM_TYPE, plan=RATE_PLAN))[0].output
        loose = negotiated(haggle(db, "4000 mein ho jayega?",
                                  room=OTHER_ROOM, plan=OTHER_PLAN))[0].output
        assert strict.get("counterTotalPaise") != loose.get("counterTotalPaise"), (
            "both rooms produced the same counter — the room-type rule is not "
            "being read, which is exactly the bug this file exists for"
        )

    def test_a_rate_plan_rule_beats_the_rooms(self, db):
        db.seed("negotiation_rules",
                rule("property", auto_pct=10),
                rule("room_type", room_type_id=ROOM_TYPE, auto_pct=5),
                rule("rate_plan", rate_plan_id=RATE_PLAN, auto_pct=15))
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN)
        assert provenance(result)[0]["rule_source"] == negotiation.RATE_PLAN_RULE

    def test_with_no_rows_the_hotel_ceiling_applies_and_says_so(self, db):
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN)
        assert provenance(result)[0]["rule_source"] == negotiation.HOTEL_DEFAULT


class TestTheOutcomeTheGuestGets:
    def test_a_reachable_price_is_allowed_and_priced_by_the_engine(self, db):
        db.seed("negotiation_rules", rule("property", auto_pct=20))
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN)
        out = negotiated(result)[0].output
        assert out["verdict"] in ("allowed", "counter", "needs_approval")
        # Every figure the model can see is a display string beside its paise.
        assert out.get("publishedTotal", "").startswith("₹")

    def test_a_price_below_the_floor_comes_back_as_a_counter(self, db):
        db.seed("negotiation_rules",
                rule("property", auto_pct=10, approval_pct=10, floor_pct=15))
        result = haggle(db, "2000 mein ho jayega?", room=ROOM_TYPE, plan=RATE_PLAN)
        out = negotiated(result)[0].output
        assert out["verdict"] == "counter"
        assert out.get("counterTotal", "").startswith("₹")

    def test_the_floor_never_appears_in_what_the_model_is_handed(self, db):
        """
        The rule that predates all of this. A counter may happen to equal the
        floor and saying that number is fine; a field labelled as the minimum is
        one a reply can leak.
        """
        db.seed("negotiation_rules",
                rule("property", auto_pct=10, approval_pct=10, floor_pct=15))
        out = negotiated(haggle(db, "2000 mein?", room=ROOM_TYPE,
                                plan=RATE_PLAN))[0].output
        assert not [k for k in out if "floor" in k.lower()]

    def test_a_low_autonomy_agent_gets_its_negotiation_queued(self, db):
        """
        The approval rung, end to end. `negotiate_rate` sits at floor 3, so a
        hotel at 2 gets the same business action proposed rather than executed —
        and the guest is told something honest rather than a price.
        """
        db.seed("negotiation_rules", rule("property", auto_pct=20))
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN,
                        autonomy=2)
        rows = [a for a in db.rows("agent_actions") if a["tool"] == "negotiate_rate"]
        assert rows and rows[-1]["status"] == "awaiting_approval"
        assert rows[-1]["human_approval_required"] is True

    def test_a_queued_negotiation_records_no_rule_provenance(self, db):
        """
        Because no rule was consulted — the autonomy gate returns above the
        engine. Recording a scope here would claim a lookup that never happened.
        """
        db.seed("negotiation_rules", rule("property", auto_pct=20))
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN,
                        autonomy=2)
        assert not provenance(result)


class TestADegradedReadEndToEnd:
    def test_the_turn_still_answers_and_the_run_says_it_was_degraded(self, db,
                                                                     monkeypatch):
        """
        The whole shape, from a failed read to a queryable row. The guest still
        gets an answer; the hotel can find out their suite rules stopped
        applying.
        """
        db.seed("negotiation_rules",
                rule("room_type", room_type_id=ROOM_TYPE, auto_pct=25))

        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_rules", _explode)
        result = haggle(db, "thoda kam kar do", room=ROOM_TYPE, plan=RATE_PLAN)

        assert negotiated(result), "a failed rule read stopped the turn"
        entry = provenance(result)[0]
        assert entry["rule_source"] == negotiation.EMERGENCY_DEFAULT
        assert entry["degraded"] is True

    def test_and_gave_away_no_more_than_the_hotel_ceiling(self, db, monkeypatch):
        """
        §4's hard requirement, at the end of the whole path rather than in a
        unit: the product must never silently give more authority because
        configuration failed.
        """
        db.seed("negotiation_rules",
                rule("room_type", room_type_id=ROOM_TYPE, auto_pct=40))

        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_rules", _explode)
        # ₹3,000 a night against a published ₹4,500 is a third off: comfortably
        # inside the unreadable 40% rule and well outside the hotel's 10%
        # ceiling. If the failed read let the room rule through, this comes back
        # `allowed`.
        #
        # (My first draft asked for "1 rupee mein?", which raises no negotiation
        # requirement at all — a single digit is not a price and the sentence
        # carries no haggling word. The turn ran no tools and the test failed
        # for a reason unrelated to what it was checking.)
        degraded = negotiated(haggle(db, "3000 mein ho jayega?", room=ROOM_TYPE,
                                     plan=RATE_PLAN))[0].output
        assert degraded["verdict"] != "allowed", (
            "a failed read let a 40% rule apply that nobody could read")

    def test_and_the_same_ask_is_allowed_when_the_rule_can_be_read(self, db):
        """
        The control, and the reason the assertion above means anything: the
        identical ask against the identical rule *is* allowed when the read
        works. Without this the test would pass on a build where negotiation
        was broken outright.
        """
        db.seed("negotiation_rules",
                rule("room_type", room_type_id=ROOM_TYPE, auto_pct=40))
        healthy = negotiated(haggle(db, "3000 mein ho jayega?", room=ROOM_TYPE,
                                    plan=RATE_PLAN))[0].output
        assert healthy["verdict"] == "allowed"
