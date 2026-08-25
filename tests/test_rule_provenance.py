"""
Which rule priced this, and was it the one the hotelier set?

**The failure this exists for.** For weeks every call to
`negotiation._read_rules` raised `AttributeError` inside the test fixture and
fell through to the hotel-wide ceiling. That fallback is correct — a read that
failed must never widen what may be given away — and it is *silent*, so nothing
broke, nothing failed, and the per-room-type and per-rate-plan rules had no test
in the suite that reached them, on any path, ever. It was found in a debugging
trace.

In production the same shape is worse than in a fixture. A hotel that has set a
strict floor on its suites, and a database read that hiccups, gets the hotel-wide
ceiling instead: safe, and still not the rules the hotelier configured. Nothing
in any record told the two apart.

So the source is named, and it distinguishes *nobody configured anything* from
*we could not find out*:

    rate_plan   this plan's own rule
    room_type   this room's own rule
    property    a hotel-wide rule row
    settings    no rule row at all; the hotel's ceiling
    emergency   a read failed, and we fell back

**The fallback direction is unchanged.** Every test below that provokes a
failure also asserts the agent came away with *less* authority, not more.
"""
import asyncio

import pytest

from app.services.pms import negotiation
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
ROOM = "22222222-2222-4222-8222-222222222222"
OTHER_ROOM = "33333333-3333-4333-8333-333333333333"
PLAN = "11111111-1111-4111-8111-111111111111"
OTHER_PLAN = "44444444-4444-4444-8444-444444444444"

# A hotel that allows 10% unattended and 25% with approval.
SETTINGS = {"max_discount_pct": 10, "max_approved_discount_pct": 25}


def run(coro):
    return asyncio.run(coro)


def rule_row(scope, **kw):
    row = {"id": f"r-{scope}", "property_id": PROPERTY, "scope": scope,
           "enabled": True, "room_type_id": None, "rate_plan_id": None,
           "floor_pct": None, "floor_paise": None, "target_pct": None,
           "target_paise": None, "ceiling_paise": None,
           "auto_pct": None, "approval_pct": None,
           "valid_from": None, "valid_to": None, "days_of_week": None,
           "blackout_dates": None, "min_nights": None, "min_units": None}
    row.update(kw)
    return row


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {"id": PROPERTY, "settings": SETTINGS})
    monkeypatch.setattr("app.services.pms.negotiation.get_db", lambda: fake)
    return fake


def rules(db, *, plan=PLAN, room=ROOM):
    return run(negotiation.rules_for(PROPERTY, rate_plan_id=plan,
                                     room_type_id=room, settings=SETTINGS))


# ── the scopes, most specific first ─────────────────────────────────────────

class TestWhichScopeApplied:
    def test_a_rate_plan_rule_wins_and_says_so(self, db):
        db.seed("negotiation_rules",
                rule_row("property", auto_pct=10),
                rule_row("room_type", room_type_id=ROOM, auto_pct=8),
                rule_row("rate_plan", rate_plan_id=PLAN, auto_pct=5))
        resolved = rules(db)
        assert resolved.source == negotiation.RATE_PLAN_RULE
        assert resolved.auto_pct == 5

    def test_a_room_type_rule_wins_when_the_plan_has_none(self, db):
        db.seed("negotiation_rules",
                rule_row("property", auto_pct=10),
                rule_row("room_type", room_type_id=ROOM, auto_pct=8))
        resolved = rules(db)
        assert resolved.source == negotiation.ROOM_TYPE_RULE
        assert resolved.auto_pct == 8

    def test_a_hotel_wide_row_wins_when_neither_does(self, db):
        db.seed("negotiation_rules", rule_row("property", auto_pct=12))
        resolved = rules(db)
        assert resolved.source == negotiation.PROPERTY_RULE
        assert resolved.auto_pct == 12

    def test_no_rows_at_all_falls_to_the_hotels_ceiling(self, db):
        resolved = rules(db)
        assert resolved.source == negotiation.HOTEL_DEFAULT
        assert resolved.auto_pct == 10

    def test_another_rooms_rule_does_not_apply_to_this_room(self, db):
        """
        The scoping. A rule written for the suites must not price a standard,
        and this is the read that the missing `or_` had made untestable.
        """
        db.seed("negotiation_rules",
                rule_row("property", auto_pct=10),
                rule_row("room_type", room_type_id=OTHER_ROOM, auto_pct=30))
        resolved = rules(db, room=ROOM)
        assert resolved.source == negotiation.PROPERTY_RULE
        assert resolved.auto_pct == 10, "another room's discount was applied"

    def test_a_disabled_rule_still_wins_its_scope(self, db):
        """
        The point of switching one off. A hotel that has closed negotiation on
        one rate plan means that plan not to be negotiable — not to fall
        through to the hotel-wide rule and be negotiable after all.
        """
        db.seed("negotiation_rules",
                rule_row("property", auto_pct=10),
                rule_row("rate_plan", rate_plan_id=PLAN, enabled=False))
        resolved = rules(db)
        assert resolved.source == negotiation.RATE_PLAN_RULE
        assert resolved.enabled is False


# ── the fallbacks, which must be visible ────────────────────────────────────

class TestAFailedReadIsNamed:
    def test_a_database_failure_falls_back_and_says_it_fell_back(self, db,
                                                                 monkeypatch):
        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_rules", _explode)
        resolved = rules(db)
        assert resolved.source == negotiation.EMERGENCY_DEFAULT
        assert resolved.source in negotiation.DEGRADED

    def test_and_the_fallback_gives_away_no_more_than_before(self, db, monkeypatch):
        """
        **The property the brief is most explicit about**: the product must
        never silently give more authority because configuration failed. The
        agent comes away with the hotel's ceiling, not a rule row it could not
        read and not something more generous.
        """
        db.seed("negotiation_rules",
                rule_row("rate_plan", rate_plan_id=PLAN, auto_pct=25))

        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_rules", _explode)
        resolved = rules(db)
        assert resolved.auto_pct == 10, "a failed read widened the agent's ceiling"
        assert resolved.source == negotiation.EMERGENCY_DEFAULT

    def test_a_settings_read_that_fails_is_also_named(self, db, monkeypatch):
        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_settings", _explode)
        resolved = run(negotiation.rules_for(
            PROPERTY, rate_plan_id=PLAN, room_type_id=ROOM))
        assert resolved.source == negotiation.EMERGENCY_DEFAULT
        assert resolved.auto_pct == negotiation.DEFAULT_MAX_DISCOUNT_PCT

    def test_a_settings_failure_does_not_then_apply_a_rule_row(self, db, monkeypatch):
        """
        One failure, one honest answer. Asking for rule rows against a ceiling
        we could not establish would apply a floor on top of a number we
        guessed, which reads as configured and is not.
        """
        db.seed("negotiation_rules", rule_row("rate_plan", rate_plan_id=PLAN,
                                              auto_pct=25))

        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_settings", _explode)
        resolved = run(negotiation.rules_for(
            PROPERTY, rate_plan_id=PLAN, room_type_id=ROOM))
        assert resolved.source == negotiation.EMERGENCY_DEFAULT
        assert resolved.auto_pct == negotiation.DEFAULT_MAX_DISCOUNT_PCT


class TestMalformedAndMissingConfiguration:
    def test_a_hand_edited_settings_blob_falls_back_conservatively(self, db):
        """
        A property whose JSON somebody hand-edited should still be able to take
        a booking, just not a generous one.
        """
        policy = negotiation.resolve({"max_discount_pct": "twenty percent"})
        assert policy.auto_max_pct == negotiation.DEFAULT_MAX_DISCOUNT_PCT
        assert policy.degraded is False, (
            "malformed configuration is the hotel's, not a failure of ours — "
            "it must not read as an outage"
        )

    def test_an_approval_ceiling_below_the_automatic_one_is_a_contradiction(self, db):
        """
        It would mean a manager may authorise less than the agent already may.
        The safe reading is that they are equal.
        """
        policy = negotiation.resolve({"max_discount_pct": 20,
                                      "max_approved_discount_pct": 5})
        assert policy.approved_max_pct >= policy.auto_max_pct

    def test_missing_settings_is_the_conservative_default(self, db):
        policy = negotiation.resolve(None)
        assert policy.auto_max_pct == negotiation.DEFAULT_MAX_DISCOUNT_PCT

    def test_a_rule_row_with_nothing_in_it_inherits_rather_than_zeroing(self, db):
        """
        A row that leaves `auto_pct` blank has not said the agent may give away
        nothing — it has said nothing about the agent, and the hotel's ceiling
        still applies. Zeroing it would let adding a floor to one room silently
        revoke that room's discretion.
        """
        db.seed("negotiation_rules", rule_row("room_type", room_type_id=ROOM))
        resolved = rules(db)
        assert resolved.source == negotiation.ROOM_TYPE_RULE
        assert resolved.auto_pct == 10


# ── and it reaches the record ───────────────────────────────────────────────

class TestItIsObservable:
    def test_the_decision_carries_the_scope_that_priced_it(self):
        rule = negotiation.Rules(
            enabled=True, source=negotiation.ROOM_TYPE_RULE,
            floor_pct=20, floor_paise=None, target_pct=None, target_paise=None,
            ceiling_paise=None, auto_pct=10, approval_pct=20,
            valid_from=None, valid_to=None, days_of_week=frozenset(),
            blackout_dates=frozenset(), min_nights=1, min_units=1)
        decision = negotiation.decide(
            gross_paise=1000000, requested_total_paise=900000,
            rules=rule, nights=2, units=1)
        # `decide` is pure and does not know the scope; `negotiate` stamps it.
        assert hasattr(decision, "rule_source")

    def test_the_scope_never_reaches_the_model(self):
        """
        `as_dict` is the projection the tool returns, and it goes into the
        model's context. The name of our configuration scope is a fact about
        *us* — the same class of thing the leak guardrail refuses in a reply.
        """
        rule = negotiation.Rules(
            enabled=True, source=negotiation.EMERGENCY_DEFAULT,
            floor_pct=None, floor_paise=None, target_pct=None, target_paise=None,
            ceiling_paise=None, auto_pct=10, approval_pct=20,
            valid_from=None, valid_to=None, days_of_week=frozenset(),
            blackout_dates=frozenset(), min_nights=1, min_units=1)
        decision = negotiation.decide(
            gross_paise=1000000, requested_total_paise=900000,
            rules=rule, nights=2, units=1)
        assert "rule_source" not in decision.as_dict()
        assert negotiation.EMERGENCY_DEFAULT not in str(decision.as_dict())

    def test_the_floor_is_still_not_on_the_object(self):
        """
        The rule that predates this and must survive it. Adding provenance must
        not add a field labelled as the minimum a hotel will take.
        """
        fields = set(negotiation.Decision.__dataclass_fields__)
        assert not {f for f in fields if "floor" in f}, fields


# ── through a real turn, which is where "observable" is proved ──────────────

class TestItReachesTheRunRecord:
    """
    The field existing is not the same as somebody being able to query it. This
    runs a turn and reads the row.
    """

    def _turn(self, monkeypatch, *, rule_scope, auto_pct=10):
        import asyncio
        from datetime import date, timedelta

        from app.services.ai import registry, runtime
        from app.services.ai.providers import EchoProvider
        import tests.test_golden_conversations as golden

        registry.load_all()
        fake = golden.db.__wrapped__(monkeypatch)
        if rule_scope:
            fake.seed("negotiation_rules", rule_row(
                rule_scope,
                property_id=golden.PROPERTY,
                room_type_id=golden.ROOM_TYPE if rule_scope == "room_type" else None,
                rate_plan_id=golden.RATE_PLAN if rule_scope == "rate_plan" else None,
                auto_pct=auto_pct))
        fake.rows("conversations")[0]["guest_profile"] = {
            "check_in": str(golden.CHECK_IN), "check_out": str(golden.CHECK_OUT),
            "adults": 2, "room_type_id": golden.ROOM_TYPE,
            "rate_plan_id": golden.RATE_PLAN, "stage": "quoted",
        }
        result = asyncio.run(runtime.run_turn(
            golden.ctx_for(fake), "thoda kam kar do",
            provider=EchoProvider(["Dekhta hoon.", "Ye best hai."])))
        return fake, result

    def test_the_run_records_which_rule_scope_priced_the_turn(self, monkeypatch):
        fake, result = self._turn(monkeypatch, rule_scope="room_type", auto_pct=8)
        provenance = (result.requirements or {}).get("provenance") or {}
        assert provenance.get("negotiation"), (
            "a turn that negotiated recorded nothing about which rule applied")
        assert provenance["negotiation"][0]["rule_source"] == negotiation.ROOM_TYPE_RULE
        assert provenance["negotiation"][0]["degraded"] is False

    def test_and_it_is_on_the_row_a_dashboard_reads(self, monkeypatch):
        fake, result = self._turn(monkeypatch, rule_scope="property")
        row = fake.rows("agent_runs")[0]
        stored = (row.get("required_actions") or {}).get("provenance") or {}
        assert stored["negotiation"][0]["rule_source"] == negotiation.PROPERTY_RULE

    def test_a_degraded_read_is_flagged_on_the_run(self, monkeypatch):
        """
        The whole point. A hotel whose configured rules stopped applying is now
        one query away rather than invisible.
        """
        def _explode(*_args, **_kw):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(negotiation, "_read_rules", _explode)
        _, result = self._turn(monkeypatch, rule_scope=None)
        entry = (result.requirements or {})["provenance"]["negotiation"][0]
        assert entry["rule_source"] == negotiation.EMERGENCY_DEFAULT
        assert entry["degraded"] is True

    def test_an_ordinary_turn_records_no_provenance_at_all(self, monkeypatch):
        """
        Absent on the turns it has nothing to say about, which is nearly all of
        them. A key that is present and empty on every row is a key nobody
        queries.
        """
        import asyncio

        from app.services.ai import registry, runtime
        from app.services.ai.providers import EchoProvider
        import tests.test_golden_conversations as golden

        registry.load_all()
        fake = golden.db.__wrapped__(monkeypatch)
        result = asyncio.run(runtime.run_turn(
            golden.ctx_for(fake), "breakfast included hai?",
            provider=EchoProvider(["Haan ji."])))
        assert "provenance" not in (result.requirements or {})
