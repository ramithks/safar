"""
The commercial rules, tested as rules rather than as examples.

Everything under test here is a pure function over data — that is why they were
written as pure functions — so none of this needs a database, a model or a
network. What it does need is the *properties* stated rather than a handful of
happy paths: the floor is a floor for every asking price, not for the three in
the brief.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.services.pms import audit, fees, policies, rates, readiness
from app.services.pms import negotiation as neg


def _rules(**overrides) -> neg.Rules:
    base = dict(
        enabled=True,
        source="rate_plan",
        floor_pct=None,
        floor_paise=None,
        target_pct=None,
        target_paise=None,
        ceiling_paise=None,
        auto_pct=10,
        approval_pct=20,
        valid_from=None,
        valid_to=None,
        days_of_week=frozenset(),
        blackout_dates=frozenset(),
        min_nights=1,
        min_units=1,
    )
    base.update(overrides)
    return neg.Rules(**base)


# ── the platform fee ─────────────────────────────────────────────────────────


class TestFees:
    def test_four_percent_of_the_brief_s_own_example(self):
        split = fees.breakdown(3_584_000)
        assert split.fee_paise == 143_360
        assert split.payout_paise == 3_440_640

    def test_fee_and_payout_always_sum_back_to_the_total(self):
        # The one invariant that matters. A rounding that loses a paisa is a
        # settlement that does not reconcile.
        for total in range(0, 200_000, 617):
            for bps in (0, 1, 250, 400, 999, 10_000):
                split = fees.breakdown(total, bps)
                assert split.fee_paise + split.payout_paise == total

    def test_a_full_rate_pays_the_hotel_nothing_and_never_less(self):
        split = fees.breakdown(100_000, 10_000)
        assert split.fee_paise == 100_000
        assert split.payout_paise == 0

    def test_a_missing_or_broken_rate_falls_back_to_the_standard_one(self):
        # Deliberately *not* to zero: a data error that silently stops charging
        # looks like a free month and is found at reconciliation.
        for bad in (None, "", "abc", -5, 99_999, [400]):
            assert fees.normalise_bps(bad) == fees.DEFAULT_FEE_BPS

    def test_a_negative_total_is_not_a_negative_fee(self):
        split = fees.breakdown(-100)
        assert split.fee_paise == 0 and split.payout_paise == 0

    def test_the_snapshot_is_the_three_columns_the_reservation_carries(self):
        assert set(fees.snapshot_columns(1000, 400)) == {
            "platform_fee_bps", "platform_fee_paise", "payout_paise",
        }


# ── the negotiation engine ───────────────────────────────────────────────────


class TestNegotiationDecision:
    """§9 and §10, branch by branch."""

    GROSS = 500_000  # ₹5,000, one night, one room

    def test_the_brief_s_worked_example(self):
        # Public 5,000 · floor 4,300 · the agent may give 10% unattended.
        rules = _rules(floor_paise=430_000, auto_pct=10, approval_pct=20)

        asked_4000 = neg.decide(gross_paise=self.GROSS, requested_total_paise=400_000,
                                rules=rules, nights=1, units=1)
        assert asked_4000.verdict == "counter"
        # ₹4,500 is what the agent may give on its own; the floor is lower and is
        # reached only through an approval.
        assert asked_4000.counter_total_paise == 450_000
        assert asked_4000.approval_available is True

        asked_4300 = neg.decide(gross_paise=self.GROSS, requested_total_paise=430_000,
                                rules=rules, nights=1, units=1)
        assert asked_4300.verdict == "needs_approval"

    def test_an_agent_with_wider_authority_can_close_at_the_floor_itself(self):
        # The same hotel, having decided its AI may go to the floor unattended.
        rules = _rules(floor_paise=430_000, auto_pct=14, approval_pct=14)
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=430_000,
                              rules=rules, nights=1, units=1)
        assert decision.verdict == "allowed"
        assert decision.final_total_paise == 430_000
        assert decision.approval_available is False

    def test_no_asking_price_is_a_request_for_the_best_price(self):
        rules = _rules(auto_pct=10)
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=0,
                              rules=rules, nights=1, units=1)
        assert decision.verdict == "counter"
        assert decision.counter_total_paise == 450_000

    def test_asking_the_published_price_is_not_a_negotiation(self):
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=self.GROSS,
                              rules=_rules(), nights=1, units=1)
        assert decision.verdict == "allowed"
        assert decision.final_total_paise == self.GROSS
        assert decision.discount_pct == 0

    def test_offering_more_than_the_published_price_is_not_charged_more(self):
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=900_000,
                              rules=_rules(), nights=1, units=1)
        assert decision.final_total_paise == self.GROSS

    def test_nothing_ever_prices_below_the_floor(self):
        # The property under test, over every asking price and both ladders.
        rules = _rules(floor_paise=430_000, auto_pct=90, approval_pct=95)
        for ask in range(0, 600_000, 1_111):
            decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=ask,
                                  rules=rules, nights=1, units=1)
            for price in (decision.final_total_paise, decision.counter_total_paise):
                if price is not None:
                    assert price >= 430_000, (ask, decision)

    def test_a_percentage_floor_and_a_price_floor_resolve_to_the_harder_one(self):
        # 20% off ₹5,000 is ₹4,000; the typed-in floor is ₹4,300. The floor a
        # hotelier wrote down specifically to stop a seasonal rate going under it
        # has to win.
        rules = _rules(floor_pct=20, floor_paise=430_000, auto_pct=50, approval_pct=50)
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=100,
                              rules=rules, nights=1, units=1)
        assert decision.counter_total_paise == 430_000

    def test_a_floor_price_is_per_room_per_night(self):
        # ₹4,300 a night, three nights, two rooms.
        rules = _rules(floor_paise=430_000, auto_pct=99, approval_pct=99)
        decision = neg.decide(gross_paise=3_000_000, requested_total_paise=0,
                              rules=rules, nights=3, units=2)
        assert decision.counter_total_paise == 430_000 * 3 * 2

    def test_negotiation_switched_off_is_a_refusal_not_a_counter(self):
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=1,
                              rules=_rules(enabled=False), nights=1, units=1)
        assert decision.verdict == "rejected"
        assert decision.reason == "negotiation_closed"

    def test_a_hotel_with_no_approval_headroom_does_not_offer_one(self):
        rules = _rules(auto_pct=10, approval_pct=10)
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=100_000,
                              rules=rules, nights=1, units=1)
        assert decision.verdict == "counter"
        # Nothing to escalate to. Saying otherwise queues a proposal no manager
        # can grant and leaves a guest waiting for it.
        assert decision.approval_available is False

    def test_the_decision_never_carries_a_field_named_after_the_floor(self):
        # §33: the minimum is the one number that must not leave the server. A
        # counter may happen to equal it; nothing may be *labelled* as it.
        rules = _rules(floor_paise=430_000, target_paise=470_000)
        decision = neg.decide(gross_paise=self.GROSS, requested_total_paise=1,
                              rules=rules, nights=1, units=1)
        projected = decision.as_dict()
        for key in projected:
            assert "floor" not in key.lower()
            assert "minimum" not in key.lower()
            assert "target" not in key.lower()
        assert 430_000 not in [v for v in projected.values() if isinstance(v, int)] or \
            projected.get("counterTotalPaise") == 430_000

    def test_the_quoted_number_is_one_the_pricing_engine_can_actually_produce(self):
        # A percentage cannot express every price. Whatever comes back must be
        # reachable by an integer discount, or the agent says one number and the
        # quote computes another.
        for gross in (1_999_900, 500_000, 333_333, 100_001):
            rules = _rules(auto_pct=13, approval_pct=13)
            decision = neg.decide(gross_paise=gross, requested_total_paise=gross // 2,
                                  rules=rules, nights=1, units=1)
            price = decision.final_total_paise or decision.counter_total_paise
            pct = decision.discount_pct if decision.final_total_paise else decision.counter_discount_pct
            assert price == gross - round(gross * pct / 100)


class TestNegotiationEligibility:
    """§10, which is a list of refusals and is therefore a list of tests."""

    def test_a_blackout_night_anywhere_in_the_stay_closes_it(self):
        rules = _rules(blackout_dates=frozenset({"2026-12-31"}))
        assert neg._eligible(rules, date(2026, 12, 30), date(2027, 1, 2), 1) == "blackout_dates"

    def test_the_checkout_morning_is_not_a_night(self):
        # A rule valid to 31 March covers a stay ending on 1 April: nobody
        # sleeps on the night they leave.
        rules = _rules(valid_to=date(2026, 3, 31))
        assert neg._eligible(rules, date(2026, 3, 29), date(2026, 4, 1), 1) is None

    def test_a_stay_starting_before_the_window_is_refused(self):
        rules = _rules(valid_from=date(2026, 6, 1))
        assert neg._eligible(rules, date(2026, 5, 30), date(2026, 6, 3), 1) == "outside_valid_dates"

    def test_selling_days_are_iso_weekdays(self):
        # Monday only. 2026-08-24 is a Monday; 2026-08-25 a Tuesday.
        rules = _rules(days_of_week=frozenset({1}))
        assert neg._eligible(rules, date(2026, 8, 24), date(2026, 8, 25), 1) is None
        assert neg._eligible(rules, date(2026, 8, 25), date(2026, 8, 26), 1) == "outside_valid_days"

    def test_a_stay_too_short_or_too_small_is_not_negotiable(self):
        rules = _rules(min_nights=3, min_units=2)
        assert neg._eligible(rules, date(2026, 8, 24), date(2026, 8, 25), 4) == "below_minimum_nights"
        assert neg._eligible(rules, date(2026, 8, 24), date(2026, 8, 28), 1) == "below_minimum_rooms"


class TestScopePrecedence:
    def test_most_specific_scope_wins(self):
        fallback = neg.resolve({"max_discount_pct": 5})
        rows = [
            {"scope": "property", "auto_pct": 5},
            {"scope": "room_type", "room_type_id": "rt", "auto_pct": 12},
            {"scope": "rate_plan", "rate_plan_id": "rp", "auto_pct": 25, "approval_pct": 30},
        ]
        assert neg.select_rules(rows, "rp", "rt", fallback).auto_pct == 25
        assert neg.select_rules(rows, "other", "rt", fallback).auto_pct == 12
        assert neg.select_rules(rows, "other", "other", fallback).auto_pct == 5

    def test_a_disabled_rule_still_wins_its_scope(self):
        # Switching negotiation off on one plan must not fall through to the
        # hotel-wide rule and become negotiable after all.
        fallback = neg.resolve({"max_discount_pct": 5})
        rows = [
            {"scope": "property", "auto_pct": 5},
            {"scope": "rate_plan", "rate_plan_id": "rp", "enabled": False},
        ]
        assert neg.select_rules(rows, "rp", None, fallback).enabled is False

    def test_a_rule_that_says_nothing_about_the_agent_keeps_the_hotels_ceiling(self):
        fallback = neg.resolve({"max_discount_pct": 8, "max_approved_discount_pct": 15})
        rules = neg.select_rules([{"scope": "property", "floor_paise": 100}], None, None, fallback)
        assert (rules.auto_pct, rules.approval_pct) == (8, 15)

    def test_no_rows_at_all_is_exactly_todays_behaviour(self):
        fallback = neg.resolve({"max_discount_pct": 8, "max_approved_discount_pct": 15})
        rules = neg.select_rules([], "rp", "rt", fallback)
        assert rules.source == "settings"
        assert (rules.auto_pct, rules.approval_pct) == (8, 15)
        assert rules.floor_paise is None

    def test_an_approval_ceiling_under_the_automatic_one_is_read_as_no_headroom(self):
        fallback = neg.resolve({"max_discount_pct": 10})
        rules = neg.select_rules(
            [{"scope": "property", "auto_pct": 20, "approval_pct": 5}], None, None, fallback,
        )
        assert rules.approval_pct == 20


# ── group tiers and derived plans ────────────────────────────────────────────


def _tier(**kw) -> rates.GroupTier:
    base = dict(id="t", room_type_id=None, min_units=1, max_units=None,
                discount_pct=None, fixed_price_paise=None, negotiable=False)
    base.update(kw)
    return rates.GroupTier(**base)


class TestGroupTiers:
    LADDER = [
        _tier(id="a", min_units=1, max_units=2),
        _tier(id="b", min_units=3, max_units=5, discount_pct=5),
        _tier(id="c", min_units=6, max_units=9, discount_pct=10),
        _tier(id="d", min_units=10, max_units=None, negotiable=True),
    ]

    def test_the_briefs_ladder(self):
        assert rates.select_tier(self.LADDER, 1, "rt").id == "a"
        assert rates.select_tier(self.LADDER, 4, "rt").id == "b"
        assert rates.select_tier(self.LADDER, 7, "rt").id == "c"
        assert rates.select_tier(self.LADDER, 20, "rt").id == "d"

    def test_a_tier_naming_this_room_type_beats_one_that_names_none(self):
        tiers = [
            _tier(id="all", min_units=3, discount_pct=10),
            _tier(id="suite", min_units=3, room_type_id="suite", discount_pct=5),
        ]
        assert rates.select_tier(tiers, 4, "suite").id == "suite"
        assert rates.select_tier(tiers, 4, "standard").id == "all"

    def test_overlapping_bands_resolve_to_the_higher_floor_not_the_older_row(self):
        tiers = [_tier(id="wide", min_units=1, max_units=9, discount_pct=5),
                 _tier(id="deep", min_units=3, max_units=9, discount_pct=12)]
        assert rates.select_tier(tiers, 4, "rt").id == "deep"

    def test_a_fixed_price_replaces_every_night(self):
        nightly = [(date(2026, 9, 10), 500_000), (date(2026, 9, 11), 700_000)]
        out, applied = rates.apply_tier(nightly, _tier(fixed_price_paise=380_000))
        assert [p for _, p in out] == [380_000, 380_000]
        assert applied.kind == "fixed"

    def test_a_discount_comes_off_each_night_separately(self):
        nightly = [(date(2026, 9, 10), 500_000), (date(2026, 9, 11), 700_000)]
        out, applied = rates.apply_tier(nightly, _tier(discount_pct=10))
        assert [p for _, p in out] == [450_000, 630_000]
        assert applied.kind == "discount" and applied.discount_pct == 10

    def test_a_negotiable_tier_changes_no_price_and_says_so(self):
        nightly = [(date(2026, 9, 10), 500_000)]
        out, applied = rates.apply_tier(nightly, _tier(negotiable=True))
        assert out == nightly
        assert applied.kind == "negotiable"

    def test_no_tier_leaves_everything_alone(self):
        nightly = [(date(2026, 9, 10), 500_000)]
        assert rates.apply_tier(nightly, None) == (nightly, None)


class TestDerivedPlans:
    def test_an_agency_rate_follows_its_parent_night_by_night(self):
        plans = [
            {"id": "public"},
            {"id": "b2b", "derived_from_plan_id": "public", "derive_discount_pct": 15},
        ]
        nightly = {"public": [(date(2026, 9, 10), 500_000), (date(2026, 9, 11), 800_000)]}
        out = rates.resolve_derived(plans, nightly)
        assert [p for _, p in out["b2b"]] == [425_000, 680_000]

    def test_a_plan_whose_parent_is_gone_disappears_rather_than_costing_nothing(self):
        plans = [{"id": "b2b", "derived_from_plan_id": "missing", "derive_discount_pct": 15}]
        assert "b2b" not in rates.resolve_derived(plans, {"b2b": [(date(2026, 9, 10), 1)]})

    def test_derivation_is_one_level_only(self):
        plans = [
            {"id": "public"},
            {"id": "b2b", "derived_from_plan_id": "public", "derive_discount_pct": 10},
            {"id": "sub", "derived_from_plan_id": "b2b", "derive_discount_pct": 10},
        ]
        out = rates.resolve_derived(plans, {"public": [(date(2026, 9, 10), 100_000)]})
        assert "sub" not in out

    def test_the_caller_s_map_is_never_mutated(self):
        plans = [{"id": "public"},
                 {"id": "b2b", "derived_from_plan_id": "public", "derive_discount_pct": 10}]
        original = {"public": [(date(2026, 9, 10), 100_000)]}
        rates.resolve_derived(plans, original)
        assert set(original) == {"public"}


class TestPlanValidity:
    def test_an_empty_day_list_means_every_day(self):
        assert rates.plan_open_on({"days_of_week": []}, date(2026, 8, 25)) is True

    def test_validity_dates_are_inclusive_at_both_ends(self):
        plan = {"valid_from": "2026-06-01", "valid_to": "2026-06-30"}
        assert rates.plan_open_on(plan, date(2026, 6, 1))
        assert rates.plan_open_on(plan, date(2026, 6, 30))
        assert not rates.plan_open_on(plan, date(2026, 5, 31))
        assert not rates.plan_open_on(plan, date(2026, 7, 1))


# ── policies ─────────────────────────────────────────────────────────────────


class TestCancellation:
    LADDER = {"tiers": [{"hoursBefore": 168, "refundPct": 100},
                        {"hoursBefore": 48, "refundPct": 50}],
              "noShowPct": 0}

    def test_the_ladder_is_walked_from_the_most_generous_end(self):
        terms = policies.cancellation_terms(self.LADDER)
        assert terms.refund_pct_at(400) == 100   # more than a week out
        assert terms.refund_pct_at(168) == 100   # exactly a week
        assert terms.refund_pct_at(100) == 50
        assert terms.refund_pct_at(10) == 0

    def test_a_ladder_that_stops_short_is_closed_at_zero(self):
        # Otherwise a guest cancelling in the last 48 hours falls off the end of
        # the policy entirely.
        terms = policies.cancellation_terms({"tiers": [{"hoursBefore": 48, "refundPct": 50}]})
        assert terms.tiers[-1].hours_before == 0
        assert terms.refund_pct_at(1) == 0

    def test_tiers_entered_out_of_order_still_resolve_correctly(self):
        terms = policies.cancellation_terms(
            {"tiers": [{"hoursBefore": 48, "refundPct": 50},
                       {"hoursBefore": 168, "refundPct": 100}]}
        )
        assert terms.refund_pct_at(200) == 100

    def test_no_policy_falls_back_to_the_two_columns_that_always_existed(self):
        terms = policies.cancellation_terms(None, refundable=True, cutoff_hours=72)
        assert terms.derived is True
        assert terms.free_until_hours == 72
        assert terms.refund_pct_at(100) == 100 and terms.refund_pct_at(10) == 0

    def test_a_non_refundable_plan_refunds_nothing_at_any_distance(self):
        terms = policies.cancellation_terms(None, refundable=False)
        assert terms.non_refundable is True
        assert terms.refund_pct_at(10_000) == 0

    def test_malformed_tiers_are_dropped_individually_not_wholesale(self):
        terms = policies.cancellation_terms(
            {"tiers": [{"hoursBefore": 168, "refundPct": 100},
                       {"nonsense": True},
                       "not a dict",
                       {"hoursBefore": 48, "refundPct": 900}]}
        )
        assert terms.derived is False
        assert terms.refund_pct_at(200) == 100
        assert terms.refund_pct_at(50) == 100  # 900 clamped to 100

    def test_a_refund_is_measured_to_the_arrival_moment_not_to_midnight(self):
        terms = policies.cancellation_terms({"tiers": [{"hoursBefore": 24, "refundPct": 100}]})
        check_in = date(2026, 9, 10)
        # 25 hours before a 2pm arrival is 1pm on the 9th — still refundable.
        at = datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc)
        assert policies.refund_for(terms, total_paise=100_000, check_in=check_in,
                                   check_in_time=time(14, 0), at=at)["refundPct"] == 100
        # 23 hours before is 3pm on the 9th — not.
        at = datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)
        assert policies.refund_for(terms, total_paise=100_000, check_in=check_in,
                                   check_in_time=time(14, 0), at=at)["refundPct"] == 0

    def test_a_no_show_uses_its_own_percentage(self):
        terms = policies.cancellation_terms({"tiers": [{"hoursBefore": 0, "refundPct": 100}],
                                             "noShowPct": 25})
        out = policies.refund_for(terms, total_paise=100_000, check_in=date(2026, 9, 10),
                                  no_show=True)
        assert out["refundPct"] == 25 and out["refundPaise"] == 25_000

    def test_refund_and_retained_sum_to_the_total(self):
        terms = policies.cancellation_terms(self.LADDER)
        for total in (0, 1, 99, 35_840_00, 1_000_013):
            out = policies.refund_for(terms, total_paise=total, check_in=date(2030, 1, 1))
            assert out["refundPaise"] + out["retainedPaise"] == total


class TestStayTimes:
    BANDS = policies.bands_from_rows([
        {"kind": "early_in", "until_time": "08:00", "charge_kind": "pct_of_night", "amount": 50},
        {"kind": "early_in", "until_time": "10:00", "charge_kind": "fixed", "amount": 50_000},
        {"kind": "early_in", "until_time": "14:00", "charge_kind": "free", "amount": 0},
        {"kind": "late_out", "until_time": "14:00", "charge_kind": "free", "amount": 0},
        {"kind": "late_out", "until_time": "17:00", "charge_kind": "fixed", "amount": 50_000},
        {"kind": "late_out", "until_time": "22:00", "charge_kind": "pct_of_night", "amount": 50},
    ])

    def test_the_briefs_early_checkin_ladder(self):
        def at(hh):
            return policies.resolve_time_request(
                self.BANDS, kind="early_in", wanted=time(hh, 0),
                standard=time(14, 0), nightly_paise=400_000,
            )
        assert at(7).charge_paise == 200_000        # before 8 → half a night
        assert at(9).charge_paise == 50_000         # 8–10 → ₹500
        assert at(11).free is True                  # after 10 → free

    def test_the_briefs_late_checkout_ladder(self):
        def at(hh):
            return policies.resolve_time_request(
                self.BANDS, kind="late_out", wanted=time(hh, 0),
                standard=time(11, 0), nightly_paise=400_000,
            )
        assert at(13).free is True
        assert at(16).charge_paise == 50_000
        assert at(20).charge_paise == 200_000

    def test_the_standard_time_is_free_and_needs_no_band(self):
        verdict = policies.resolve_time_request(
            [], kind="early_in", wanted=time(15, 0), standard=time(14, 0), nightly_paise=1,
        )
        assert verdict.available and verdict.free

    def test_beyond_every_band_is_an_honest_no(self):
        verdict = policies.resolve_time_request(
            self.BANDS, kind="late_out", wanted=time(23, 30),
            standard=time(11, 0), nightly_paise=400_000,
        )
        assert verdict.available is False

    def test_a_hotel_that_has_configured_nothing_does_not_invent_a_yes(self):
        verdict = policies.resolve_time_request(
            [], kind="early_in", wanted=time(10, 0), standard=time(14, 0), nightly_paise=1,
        )
        assert verdict.available is False

    def test_a_band_may_say_the_answer_is_no(self):
        bands = policies.bands_from_rows(
            [{"kind": "early_in", "until_time": "06:00", "charge_kind": "unavailable"}]
        )
        verdict = policies.resolve_time_request(
            bands, kind="early_in", wanted=time(5, 0), standard=time(14, 0), nightly_paise=1,
        )
        assert verdict.available is False and verdict.charge_kind == "unavailable"

    def test_inactive_bands_are_not_read(self):
        bands = policies.bands_from_rows(
            [{"kind": "early_in", "until_time": "10:00", "charge_kind": "free", "is_active": False}]
        )
        assert bands == []


class TestMeals:
    OPTIONS = [
        {"meal": "breakfast", "availability": "available", "price_paise": 30_000,
         "price_unit": "per_person", "starts_at": "07:30:00", "ends_at": "10:30:00",
         "diet": "both", "service_style": "buffet"},
        {"meal": "lunch", "availability": "not_available"},
    ]

    def test_inclusion_comes_from_the_rate_plan_code(self):
        by_meal = {m["meal"]: m for m in policies.meals_for_plan("CP", self.OPTIONS)}
        assert by_meal["breakfast"]["included"] is True
        assert by_meal["dinner"]["included"] is False

    def test_map_is_breakfast_and_dinner(self):
        by_meal = {m["meal"]: m for m in policies.meals_for_plan("MAP", self.OPTIONS)}
        assert by_meal["breakfast"]["included"] and by_meal["dinner"]["included"]
        assert by_meal["lunch"]["included"] is False

    def test_a_meal_the_hotel_does_not_serve_is_not_included_whatever_the_code_says(self):
        by_meal = {m["meal"]: m for m in policies.meals_for_plan("AP", self.OPTIONS)}
        assert by_meal["lunch"]["included"] is False

    def test_an_undescribed_meal_still_appears_and_says_it_is_undescribed(self):
        by_meal = {m["meal"]: m for m in policies.meals_for_plan("EP", self.OPTIONS)}
        assert by_meal["dinner"]["configured"] is False
        assert by_meal["dinner"]["availability"] == "unknown"

    def test_an_unknown_plan_code_includes_nothing(self):
        by_meal = {m["meal"]: m for m in policies.meals_for_plan("XYZ", self.OPTIONS)}
        assert not any(m["included"] for m in by_meal.values())


class TestDeposit:
    def test_the_briefs_worked_example(self):
        out = policies.deposit_for(3_584_000, token_pct=20)
        assert out["depositPaise"] == 716_800
        assert out["balancePaise"] == 2_867_200

    def test_pay_at_hotel_takes_nothing_now(self):
        out = policies.deposit_for(100_000, token_pct=20, rule="pay_at_hotel")
        assert out["depositPaise"] == 0 and out["balancePaise"] == 100_000

    def test_full_payment_leaves_no_balance(self):
        assert policies.deposit_for(100_000, token_pct=20, rule="full")["balancePaise"] == 0

    def test_a_fixed_token_can_never_exceed_the_total(self):
        out = policies.deposit_for(5_000, token_pct=20, rule="fixed", fixed_paise=999_999)
        assert out["depositPaise"] == 5_000 and out["balancePaise"] == 0

    def test_a_group_uses_the_group_deposit_where_the_hotel_set_one(self):
        assert policies.deposit_for(100_000, token_pct=20, group_deposit_pct=50,
                                    is_group=True)["depositPaise"] == 50_000
        assert policies.deposit_for(100_000, token_pct=20, group_deposit_pct=50,
                                    is_group=False)["depositPaise"] == 20_000


# ── readiness ────────────────────────────────────────────────────────────────


class TestRoomReadiness:
    COMPLETE = {
        "id": "r1", "name": "Deluxe Double", "total_units": 6,
        "max_adults": 2, "max_children": 1, "children_allowed": "allowed",
        "bed_type": "King bed", "bed_count": 1, "extra_bed": "available",
        "description": "Quiet room at the back with a balcony over the courtyard.",
        "amenities": ["AC", "wifi", "hot water"], "photos": ["https://x/1.jpg"],
    }
    PLANS = [{"base_price_paise": 350_000, "meal_plan": "CP",
              "cancellation_policy": {"tiers": [{"hoursBefore": 24, "refundPct": 100}]}}]

    def test_a_fully_described_room_is_ready(self):
        out = readiness.room_result(self.COMPLETE, self.PLANS, {})
        assert out["readyToSell"] and out["sellable"] and out["pct"] == 100

    def test_not_applicable_is_an_answer(self):
        # §2: the hotelier says "none", and that finishes the question.
        room = dict(self.COMPLETE, extra_bed="none", children_allowed="not_allowed",
                    max_children=0)
        assert readiness.room_result(room, self.PLANS, {})["readyToSell"] is True

    def test_a_blank_answer_is_not(self):
        for blanked in ("extra_bed", "children_allowed", "max_adults", "bed_count"):
            room = dict(self.COMPLETE, **{blanked: None})
            assert readiness.room_result(room, self.PLANS, {})["readyToSell"] is False

    def test_an_incomplete_room_that_predates_these_fields_is_still_sellable(self):
        # The whole of §36 in one assertion: every room type in production looks
        # like this, and none of them may go dark.
        legacy = {"id": "r", "name": "Deluxe", "total_units": 4, "amenities": [], "photos": []}
        out = readiness.room_result(legacy, self.PLANS, {})
        assert out["sellable"] is True
        assert out["readyToSell"] is False

    def test_a_room_with_no_price_cannot_be_sold(self):
        out = readiness.room_result(self.COMPLETE, [{"meal_plan": "EP"}], {})
        assert out["sellable"] is False

    def test_a_check_that_raises_counts_against_the_room_rather_than_500ing(self):
        # `amenities: 3` is not a list. The check calls len() on it and blows up.
        out = readiness.room_result(dict(self.COMPLETE, amenities=3), self.PLANS, {})
        assert "amenities" in [m["key"] for m in out["missing"]]

    def test_every_check_names_what_it_blocks(self):
        for check in readiness.ROOM_CHECKS:
            assert check.blocks
            assert set(check.blocks) <= {"agent", "website", "booking", "marketing"}


class TestSectionScoring:
    def test_a_percentage_short_of_everything_never_reads_as_a_hundred(self):
        # §40: no fake 100%.
        assert readiness.SectionResult("k", "L", 199, 200).pct == 99
        assert readiness.SectionResult("k", "L", 200, 200).pct == 100

    def test_an_empty_section_is_complete_rather_than_a_division_by_zero(self):
        assert readiness.SectionResult("k", "L", 0, 0).pct == 100

    def test_every_declared_check_has_a_unique_key_within_its_section(self):
        for key, _label, checks in readiness.SECTIONS:
            keys = [c.key for c in checks]
            assert len(keys) == len(set(keys)), key


# ── the audit log ────────────────────────────────────────────────────────────


class TestAudit:
    def test_only_commercially_sensitive_fields_are_recorded(self):
        changes = audit.diff({"name": "Old", "floor_paise": 100},
                             {"name": "New", "floor_paise": 200})
        assert [c[0] for c in changes] == ["floor_paise"]

    def test_a_value_that_only_changed_type_is_not_a_change(self):
        # A form round-tripping an integer through a text input must not write a
        # history row every time somebody opens it and presses Save.
        assert audit.diff({"auto_pct": 10}, {"auto_pct": "10"}) == []

    def test_an_absent_key_is_not_a_deletion(self):
        # PATCH semantics in this codebase are "absent means unchanged".
        assert audit.diff({"floor_paise": 100}, {"name": "x"}) == []

    def test_a_value_arriving_for_the_first_time_is_recorded_with_no_old(self):
        assert audit.diff({}, {"floor_paise": 100}) == [("floor_paise", None, "100")]

    def test_lists_and_dicts_are_rendered_for_a_person_not_as_json(self):
        changes = audit.diff({"blackout_dates": []}, {"blackout_dates": ["2026-12-31"]})
        assert changes == [("blackout_dates", "(empty)", "2026-12-31")]

    def test_booleans_read_as_yes_and_no(self):
        assert audit.diff({"enabled": True}, {"enabled": False}) == [("enabled", "yes", "no")]


@pytest.mark.parametrize("meal_plan,expected", [
    ("EP", set()),
    ("CP", {"breakfast"}),
    ("MAP", {"breakfast", "dinner"}),
    ("AP", {"breakfast", "lunch", "dinner"}),
])
def test_meal_plan_vocabulary_matches_the_column(meal_plan, expected):
    assert policies.MEAL_PLAN_INCLUDES[meal_plan] == expected
