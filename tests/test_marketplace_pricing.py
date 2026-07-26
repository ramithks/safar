"""
Pricing pipeline tests — the backend half of the contract.

These mirror the website's lib/marketplace/pricing.test.ts case for case. If one
suite changes a number, the other must too, or the summary a traveller sees and
the amount we charge will drift. The properties that matter:

  * seasonal rates price per night, even across a season boundary
  * discounts come off before commission
  * amount = gross - discount, and commission + payout = amount, always
  * commission is 10% for every kind (₹499/month + 10%), from one table
"""
import pytest

from app.services import marketplace_pricing as p


def coupon(**over):
    base = {
        "id": "c1",
        "partner_id": "pp1",
        "listing_id": None,
        "trip_id": None,
        "code": "MONSOON",
        "kind": "percent",
        "value": 10,
        "max_discount": None,
        "min_amount": None,
        "starts_at": None,
        "ends_at": None,
        "usage_limit": None,
        "used_count": 0,
        "active": True,
    }
    base.update(over)
    return base


def rate(starts_on, ends_on, price):
    return {"starts_on": starts_on, "ends_on": ends_on, "price": price}


# ── commission source of truth ───────────────────────────────────────────────


class TestCommissionSourceOfTruth:
    def test_every_kind_is_ten_percent(self):
        # V3 unified pricing: ₹499/month + 10% on every kind.
        assert p.commission_pct_for("hotel") == 10.0
        assert p.commission_pct_for("group_trip") == 10.0
        assert p.commission_pct_for("experience") == 10.0

    def test_unknown_kinds_never_charge(self):
        assert p.commission_pct_for("mystery") == 0.0


class TestSplitCommission:
    def test_takes_exactly_the_rate(self):
        assert p.split_commission(100_000, 5) == (5_000, 95_000)

    def test_always_sums_back(self):
        for amount in [1, 7, 99, 333, 100_000, 920_000, 12_345_678]:
            for pct in [0, 5, 10, 12.5, 33.33]:
                commission, payout = p.split_commission(amount, pct)
                assert commission + payout == amount

    def test_rounds_commission_not_payout(self):
        # 333 * 10% = 33.3 -> commission 33, payout takes the remainder.
        assert p.split_commission(333, 10) == (33, 300)


# ── nights and seasonal rates ────────────────────────────────────────────────


class TestNights:
    def test_counts_nights_not_days(self):
        assert p.nights_between("2026-08-01", "2026-08-04") == 3

    def test_zero_for_empty_or_reversed(self):
        assert p.nights_between("2026-08-04", "2026-08-04") == 0
        assert p.nights_between("2026-08-04", "2026-08-01") == 0

    def test_crosses_a_month_boundary(self):
        assert p.nights_between("2026-10-29", "2026-11-02") == 4


class TestSeasonRates:
    rates = [rate("2026-12-20", "2026-12-31", 6000)]

    def test_matches_inclusively(self):
        assert p.rate_for_day(self.rates, "2026-12-20")["price"] == 6000
        assert p.rate_for_day(self.rates, "2026-12-31")["price"] == 6000

    def test_null_outside_range(self):
        assert p.rate_for_day(self.rates, "2026-12-19") is None
        assert p.rate_for_day(self.rates, "2027-01-01") is None

    def test_falls_back_to_base_with_no_rates(self):
        assert p.nightly_prices(2000, [], "2026-08-01", "2026-08-04") == [200_000, 200_000, 200_000]

    def test_prices_each_night_across_a_boundary(self):
        # The bug worth guarding: pricing the whole stay at the check-in rate.
        rates = [rate("2026-12-24", "2026-12-26", 5000)]
        assert p.nightly_prices(2000, rates, "2026-12-23", "2026-12-26") == [
            200_000,  # 23rd base
            500_000,  # 24th peak
            500_000,  # 25th peak
        ]


# ── coupons ──────────────────────────────────────────────────────────────────


class TestCouponRejection:
    now = p.datetime(2026, 8, 1, tzinfo=p.timezone.utc)

    def test_accepts_a_live_coupon(self):
        assert p.coupon_rejection(coupon(), 100_000, self.now) is None

    def test_rejects_inactive_unstarted_expired_exhausted(self):
        assert p.coupon_rejection(coupon(active=False), 100_000, self.now) == "inactive"
        assert p.coupon_rejection(coupon(starts_at="2026-09-01"), 100_000, self.now) == "not_started"
        assert p.coupon_rejection(coupon(ends_at="2026-07-01"), 100_000, self.now) == "expired"
        assert p.coupon_rejection(coupon(usage_limit=5, used_count=5), 100_000, self.now) == "exhausted"

    def test_rejects_below_minimum(self):
        assert p.coupon_rejection(coupon(min_amount=2000), 100_000, self.now) == "below_minimum"
        assert p.coupon_rejection(coupon(min_amount=1000), 100_000, self.now) is None

    def test_every_reason_has_traveller_facing_copy(self):
        """
        A reason with no entry in COUPON_REJECTIONS reaches the traveller as
        `null`, which the booking widget renders as nothing at all — the code
        silently fails to apply and they are never told why.
        """
        reasons = {"inactive", "not_started", "expired", "exhausted", "below_minimum"}
        assert reasons <= set(p.COUPON_REJECTIONS)
        assert all(p.COUPON_REJECTIONS[r].strip() for r in reasons)

    def test_unknown_covers_codes_that_never_resolve(self):
        """
        The commonest rejection isn't in `coupon_rejection` at all: a typo, a
        code scoped to another listing, or one targeted at someone else's phone
        never resolves to a row, so the quote endpoint reaches for this key
        itself. Without it the traveller typed a code, watched the total not
        move, and was told nothing.
        """
        assert p.COUPON_REJECTIONS["unknown"].strip()


class TestCouponDiscount:
    def test_flat(self):
        assert p.coupon_discount_paise(coupon(kind="flat", value=500), 300_000) == 50_000

    def test_percent(self):
        assert p.coupon_discount_paise(coupon(kind="percent", value=15), 200_000) == 30_000

    def test_percent_cap(self):
        c = coupon(kind="percent", value=50, max_discount=1000)
        assert p.coupon_discount_paise(c, 1_000_000) == 100_000

    def test_never_more_than_the_order(self):
        # ₹2,000 flat on a ₹1,500 order must not go negative.
        assert p.coupon_discount_paise(coupon(kind="flat", value=2000), 150_000) == 150_000

    def test_nothing_when_it_does_not_apply(self):
        assert p.coupon_discount_paise(coupon(active=False), 200_000) == 0


# ── the quote ────────────────────────────────────────────────────────────────


class TestQuote:
    def test_multiplies_units_by_quantity(self):
        q = p.quote([200_000], quantity=3, commission_pct=10)
        assert q.gross_paise == 600_000
        assert q.amount_paise == 600_000

    def test_discount_before_commission(self):
        # ₹2,000 gross, 10% off -> ₹1,800 charged, 5% of ₹1,800 to TravBuddy.
        q = p.quote(
            [200_000],
            quantity=1,
            commission_pct=5,
            coupon=coupon(kind="percent", value=10),
        )
        assert q.gross_paise == 200_000
        assert q.discount_paise == 20_000
        assert q.amount_paise == 180_000
        assert q.commission_paise == 9_000
        assert q.payout_paise == 171_000

    def test_always_balances(self):
        for unit in [1, 99, 333, 250_000, 1_234_567]:
            for qty in [1, 2, 7]:
                for pct in [0, 5, 10, 12.5]:
                    q = p.quote(
                        [unit], quantity=qty, commission_pct=pct,
                        coupon=coupon(kind="percent", value=13),
                    )
                    assert q.amount_paise == q.gross_paise - q.discount_paise
                    assert q.commission_paise + q.payout_paise == q.amount_paise
                    assert q.payout_paise >= 0

    def test_reports_why_a_coupon_did_not_apply(self):
        q = p.quote([100_000], quantity=1, commission_pct=5, coupon=coupon(min_amount=5000))
        assert q.coupon_rejection == "below_minimum"
        assert q.discount_paise == 0
        assert q.amount_paise == 100_000

    def test_floors_quantity_at_one(self):
        assert p.quote([100_000], quantity=0, commission_pct=5).gross_paise == 100_000


class TestQuoteStay:
    def test_peak_season_per_night_at_five_percent(self):
        q = p.quote_stay(
            base_price_rupees=2000,
            season_rates=[rate("2026-12-24", "2026-12-26", 5000)],
            check_in="2026-12-23",
            check_out="2026-12-26",
            rooms=2,
            commission_pct=5,
        )
        # (2000 + 5000 + 5000) x 2 rooms = ₹24,000
        assert q.gross_paise == 2_400_000
        assert q.commission_paise == 120_000
        assert q.payout_paise == 2_280_000


# ── refunds ──────────────────────────────────────────────────────────────────


class TestRefunds:
    tiers = [
        {"hours_before": 24, "refund_pct": 25},
        {"hours_before": 168, "refund_pct": 100},
        {"hours_before": 72, "refund_pct": 50},
    ]

    def test_best_tier_regardless_of_input_order(self):
        d = lambda s: p.datetime.fromisoformat(s).replace(tzinfo=p.timezone.utc)
        assert p.refund_pct_for(self.tiers, "2026-08-20", d("2026-08-01T00:00:00")) == 100
        assert p.refund_pct_for(self.tiers, "2026-08-20", d("2026-08-16T00:00:00")) == 50
        assert p.refund_pct_for(self.tiers, "2026-08-20", d("2026-08-18T12:00:00")) == 25

    def test_nothing_in_last_window_or_without_policy(self):
        d = p.datetime.fromisoformat("2026-08-19T18:00:00").replace(tzinfo=p.timezone.utc)
        assert p.refund_pct_for(self.tiers, "2026-08-20", d) == 0
        assert p.refund_pct_for([], "2026-08-20", d) == 0

    def test_never_more_than_paid(self):
        d = p.datetime.fromisoformat("2026-08-01T00:00:00").replace(tzinfo=p.timezone.utc)
        assert p.refund_paise(500_000, self.tiers, "2026-08-20", d) == 500_000


class TestToPaise:
    def test_rounds_not_truncates(self):
        assert p.to_paise(1499.5) == 149_950
        assert p.to_paise(0.005) == 1


class TestJsMathRoundParity:
    """
    The engines must round identically. Python's built-in round() is banker's
    rounding and would diverge from JS Math.round at exact halves — these pin
    the half-up behaviour the pricing module implements instead.
    """

    def test_half_rounds_up_not_to_even(self):
        # round(2.5) is 2 in Python; Math.round(2.5) is 3. We follow JS.
        assert p._round_half_up(2.5) == 3
        assert p._round_half_up(0.5) == 1
        assert p._round_half_up(3.5) == 4

    def test_commission_at_an_exact_half_matches_js(self):
        # 50 paise * 5% = 2.5 -> JS Math.round gives 3, banker's would give 2.
        assert p.split_commission(50, 5)[0] == 3

    def test_percent_discount_at_an_exact_half_matches_js(self):
        # 50 paise * 5% = 2.5 -> 3.
        assert p.coupon_discount_paise(coupon(kind="percent", value=5), 50) == 3


# ── the advance ──────────────────────────────────────────────────────────────
#
# Mirrors "advancePaise" / "quote with an advance" in the website's
# pricing.test.ts, case for case.


class TestAdvancePaise:
    def test_charges_the_whole_total_when_there_is_no_deposit_policy(self):
        assert p.advance_paise(1_000_000, None) == 1_000_000
        assert p.advance_paise(1_000_000, {"type": "none", "value": None}) == 1_000_000

    def test_takes_a_percentage_of_the_total(self):
        assert p.advance_paise(1_000_000, {"type": "percent", "value": 30}) == 300_000

    def test_takes_a_flat_rupee_amount(self):
        assert p.advance_paise(1_000_000, {"type": "fixed", "value": 2500}) == 250_000

    def test_lifts_anything_below_the_floor_up_to_it(self):
        # Commission is 10% of what we process, so a ₹1 advance on a ₹10,000 trip
        # would be a way to route the sale around us. The floor is the guard.
        assert p.advance_paise(1_000_000, {"type": "fixed", "value": 1}) == 200_000
        assert p.advance_paise(1_000_000, {"type": "percent", "value": 5}) == 200_000
        assert p.MIN_DEPOSIT_PCT == 20

    def test_never_asks_for_more_than_the_total(self):
        assert p.advance_paise(100_000, {"type": "fixed", "value": 5000}) == 100_000
        assert p.advance_paise(100_000, {"type": "percent", "value": 100}) == 100_000

    def test_never_lands_below_the_floor_or_on_part_of_a_rupee(self):
        # 20% of ₹10.01 is ₹2.002 — the floor is 201 paise, which rounds up to a
        # whole ₹3. Rounding DOWN would put the advance under the floor.
        assert p.advance_paise(1001, {"type": "fixed", "value": 1}) == 300
        # A percentage that lands on paise is rounded up to the rupee.
        assert p.advance_paise(1_199_900, {"type": "percent", "value": 25}) == 300_000

    def test_reads_the_db_row_shape_too(self):
        # The endpoint hands this straight off a listing row.
        assert p.advance_paise(1_000_000, {"deposit_type": "percent", "deposit_value": 30}) == 300_000


class TestQuoteWithAnAdvance:
    def test_splits_the_total_into_now_and_balance(self):
        q = p.quote([1_000_000], 1, 10, deposit={"type": "percent", "value": 25})
        assert q.total_paise == 1_000_000
        assert q.amount_paise == 250_000
        assert q.balance_paise == 750_000

    def test_charges_commission_on_the_whole_booking_out_of_the_advance(self):
        q = p.quote([1_000_000], 1, 10, deposit={"type": "percent", "value": 25})
        assert q.commission_paise == 100_000  # 10% of the ₹10,000 booking
        assert q.payout_paise == 150_000  # the rest of the ₹2,500 advance
        assert q.commission_paise + q.payout_paise == q.amount_paise

    def test_never_charges_more_commission_than_the_advance_we_hold(self):
        # A 40% rate against a 25% advance: capped, so the payout floors at zero
        # rather than invoicing the partner for money we never took.
        q = p.quote([1_000_000], 1, 40, deposit={"type": "percent", "value": 25})
        assert q.commission_paise == q.amount_paise
        assert q.payout_paise == 0

    def test_behaves_exactly_as_before_when_nothing_is_deposited(self):
        q = p.quote([1_000_000], 1, 10)
        assert q.amount_paise == q.total_paise
        assert q.balance_paise == 0

    def test_discount_comes_off_the_total_so_the_balance_absorbs_the_rest(self):
        q = p.quote(
            [1_000_000], 1, 10,
            coupon=coupon(kind="flat", value=2000),
            deposit={"type": "percent", "value": 25},
        )
        assert q.total_paise == 800_000
        assert q.amount_paise == 200_000
        assert q.balance_paise == 600_000
        assert q.amount_paise + q.balance_paise == q.total_paise
