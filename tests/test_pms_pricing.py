"""
Pricing tests.

Pricing is pure and it decides money, which makes it the highest-value thing in
the PMS to test and the cheapest. The properties that matter:

  * the GST slab is decided per night, not per booking
  * a discount is clamped to the property's floor, never the caller's request
  * per-night discounts sum exactly to the total discount, with no rounding drift
  * no float ever reaches a total
"""
from datetime import date

import pytest

from app.services.pms.pricing import (
    GST_HIGH,
    GST_LOW,
    NightlyRate,
    date_range,
    gst_pct_for_night,
    nights_between,
    price_stay,
    resolve_property_limits,
)


def nights(*prices: int, start: date = date(2026, 8, 14)) -> list[NightlyRate]:
    return [NightlyRate(date=date.fromordinal(start.toordinal() + i), price_paise=p)
            for i, p in enumerate(prices)]


# ── half-open ranges ─────────────────────────────────────────────────────────

def test_nights_are_half_open():
    # You occupy the nights you sleep, not the morning you leave.
    assert nights_between(date(2026, 8, 14), date(2026, 8, 16)) == 2
    assert len(date_range(date(2026, 8, 14), date(2026, 8, 16))) == 2
    assert date_range(date(2026, 8, 14), date(2026, 8, 16))[-1] == date(2026, 8, 15)


def test_same_day_is_not_a_stay():
    assert nights_between(date(2026, 8, 14), date(2026, 8, 14)) == 0


# ── GST slabs ────────────────────────────────────────────────────────────────

def test_slab_boundary_is_per_unit_per_night():
    assert gst_pct_for_night(749_900) == GST_LOW    # ₹7,499
    assert gst_pct_for_night(750_000) == GST_HIGH   # ₹7,500 exactly — at, not above
    assert gst_pct_for_night(750_100) == GST_HIGH


def test_a_stay_crossing_the_slab_is_taxed_at_both_rates():
    # Two weekday nights below the threshold, one weekend night above it. The
    # common bug is charging one rate for the whole stay; that is a tax filing
    # problem, not a rounding one.
    priced = price_stay(nights(400_000, 400_000, 900_000), units=1)

    assert priced.gross_paise == 1_700_000
    expected = round(400_000 * 0.12) * 2 + round(900_000 * 0.18)
    assert priced.tax_paise == expected
    assert set(priced.tax_breakdown) == {"12%", "18%"}


def test_units_do_not_change_the_slab():
    # Three rooms at ₹4,000 is ₹12,000 gross but ₹4,000 per unit per night, so
    # it stays in the lower slab. Deciding the slab on the booking total would
    # overcharge every multi-room booking.
    priced = price_stay(nights(400_000), units=3)
    assert priced.gross_paise == 1_200_000
    assert priced.tax_paise == round(1_200_000 * 0.12)


# ── discounts ────────────────────────────────────────────────────────────────

def test_discount_is_clamped_to_the_property_floor():
    priced = price_stay(nights(500_000, 500_000), units=1,
                        requested_discount_pct=30, max_discount_pct=10)

    assert priced.discount_pct_applied == 10
    assert priced.clamped_from_pct == 30
    assert priced.discount_paise == 100_000


def test_a_request_within_the_floor_is_not_flagged():
    priced = price_stay(nights(500_000), units=1,
                        requested_discount_pct=5, max_discount_pct=10)
    assert priced.discount_pct_applied == 5
    assert priced.clamped_from_pct is None


def test_a_zero_floor_refuses_every_discount():
    # The safe configuration for a property that does not negotiate. An agent
    # asking for 20% gets zero rather than an error, which keeps the
    # conversation going.
    priced = price_stay(nights(500_000), units=1,
                        requested_discount_pct=20, max_discount_pct=0)
    assert priced.discount_paise == 0
    assert priced.clamped_from_pct == 20


def test_negative_discount_cannot_inflate_the_price():
    priced = price_stay(nights(500_000), units=1, requested_discount_pct=-50)
    assert priced.discount_paise == 0


def test_per_night_discounts_sum_exactly_to_the_total():
    # Three nights at an awkward price with a 7% discount: the naive
    # implementation rounds each night independently and lands a rupee or two
    # away from the booking's own discount figure.
    priced = price_stay(nights(333_333, 333_333, 333_334), units=2,
                        requested_discount_pct=7, max_discount_pct=15)

    net = priced.gross_paise - priced.discount_paise
    assert priced.total_paise == net + priced.tax_paise
    assert priced.discount_paise == round(priced.gross_paise * 7 / 100)


def test_discount_that_drops_a_night_below_the_slab_lowers_its_tax():
    # ₹7,600 discounted 10% is ₹6,840 — genuinely in the lower slab, and taxing
    # it at 18% would overcharge the guest on a rate they never paid.
    priced = price_stay(nights(760_000), units=1,
                        requested_discount_pct=10, max_discount_pct=10)
    assert priced.tax_paise == round(684_000 * 0.12)


# ── totals ───────────────────────────────────────────────────────────────────

def test_everything_is_an_integer():
    priced = price_stay(nights(333_333, 777_777), units=3,
                        requested_discount_pct=9, max_discount_pct=15)
    for value in (priced.gross_paise, priced.discount_paise, priced.tax_paise,
                  priced.total_paise, priced.token_paise, priced.balance_paise):
        assert isinstance(value, int), f"{value!r} is not an int"


def test_token_and_balance_reconstruct_the_total():
    priced = price_stay(nights(480_000, 600_000), units=1, token_pct=20)
    assert priced.token_paise + priced.balance_paise == priced.total_paise


def test_token_never_exceeds_the_total():
    priced = price_stay(nights(100_000), units=1, token_pct=150)
    assert priced.token_paise == priced.total_paise
    assert priced.balance_paise == 0


def test_tax_inclusive_property_adds_no_tax():
    priced = price_stay(nights(500_000), units=1, tax_inclusive=True)
    assert priced.tax_paise == 0
    assert priced.total_paise == priced.gross_paise


def test_a_stay_needs_at_least_one_night_and_one_room():
    with pytest.raises(ValueError):
        price_stay([], units=1)
    with pytest.raises(ValueError):
        price_stay(nights(100_000), units=0)


# ── property settings ────────────────────────────────────────────────────────

def test_limits_fall_back_when_settings_are_missing_or_broken():
    # A property whose settings JSON someone hand-edited should still be able to
    # take a booking — just not a generous one.
    assert resolve_property_limits(None) == (10, 20)
    assert resolve_property_limits({}) == (10, 20)
    assert resolve_property_limits({"max_discount_pct": "abc"}) == (10, 20)
    assert resolve_property_limits({"max_discount_pct": 25, "token_pct": 30}) == (25, 30)


def test_absurd_limits_are_treated_as_typos():
    assert resolve_property_limits({"max_discount_pct": 900}) == (100, 20)
    assert resolve_property_limits({"max_discount_pct": -5}) == (0, 20)
