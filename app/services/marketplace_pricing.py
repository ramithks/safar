"""
Marketplace money math — the backend mirror of the website's
lib/marketplace/pricing.ts.

The two files MUST agree function-for-function: the website quotes a price to
the traveller, the backend charges it, and the payout ledger settles it, so a
divergence here is a real rupee discrepancy in someone's account. The test
suite (tests/test_marketplace_pricing.py) pins the same cases as the TS
pricing.test.ts.

Everything here is integer paise. Rupees appear only where a partner typed them
(listing prices, coupon values) and are converted once, at the edge. No float
ever reaches a total.

The pipeline, in order — this is the contract:

    season rate (per night) -> gross -> coupon discount -> total -> advance
                                                        -> commission + payout

Discounts come off before commission, so the partner and TravBuddy share the
cost of a promotion in proportion. Commission is charged on the TOTAL — the
whole booking we brought the partner — and deducted from the advance we hold;
the payout is whatever is left of that advance, so the two always sum back to
the amount charged.
"""
# NOTE: deliberately no `from __future__ import annotations` — keeps parity with
# the rest of app/api/v1/marketplace, where runtime annotations matter to
# pydantic/FastAPI.
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

PAISE = 100


def _round_half_up(x: float) -> int:
    """
    Round to nearest, ties toward +infinity — exactly JavaScript's Math.round.

    This must match, not merely approximate, the website's pricing.ts: Python's
    built-in round() uses banker's rounding (round(0.5) == 0, round(2.5) == 2),
    so at an exact half-paisa boundary the two engines would otherwise quote and
    charge amounts one paisa apart. Every amount here is non-negative, where
    floor(x + 0.5) reproduces Math.round precisely.
    """
    return math.floor(x + 0.5)

# ── the single source of truth for commission ────────────────────────────────
#
# Mirrors KIND_RULES in the website's lib/marketplace/types.ts. Every place that
# needs a rate reads it from here — there is no second copy to drift.
#
# V3 (unified pricing): every kind is ₹499/month + 10% on bookings we process.
#   hotel      -> ₹499/month subscription + 10% on bookings we process
#   group_trip -> ₹499/month subscription + 10%
#   experience -> ₹499/month subscription + 10%
COMMISSION_PCT = {"hotel": 10.0, "group_trip": 10.0, "experience": 10.0}


def commission_pct_for(item_type: str) -> float:
    """The commission rate for a kind. Unknown kinds default to 0, never a charge."""
    return COMMISSION_PCT.get(item_type, 0.0)


def to_paise(rupees: float) -> int:
    """Whole ₹ -> paise, rounding so a stray '1499.50' can't truncate to 1499."""
    return _round_half_up(rupees * PAISE)


def split_commission(amount_paise: int, pct: float) -> tuple[int, int]:
    """
    Split a gross amount into (commission, payout).

    Rounds the commission and gives the payout the remainder, so the two always
    sum back to exactly `amount_paise` — rounding must never create or destroy a
    paisa in the ledger.
    """
    commission = _round_half_up(amount_paise * pct / 100)
    return commission, amount_paise - commission


# ── seasonal rates ───────────────────────────────────────────────────────────

_ONE_DAY = 86_400


def _parse_day(day: str) -> date:
    return date.fromisoformat(day[:10])


def nights_between(check_in: str, check_out: str) -> int:
    """Nights between two YYYY-MM-DD dates; 0 for an empty or reversed range."""
    nights = (_parse_day(check_out) - _parse_day(check_in)).days
    return nights if nights > 0 else 0


def rate_for_day(rates: list[dict], day: str) -> Optional[dict]:
    """
    The season rate covering `day`, or None to fall back to the base price.

    Ranges are inclusive at both ends and cannot overlap — the DB enforces that
    with a GiST exclusion constraint, so the first match is the only match.
    """
    for r in rates:
        if r["starts_on"] <= day <= r["ends_on"]:
            return r
    return None


def nightly_prices(
    base_price_rupees: float, rates: list[dict], check_in: str, check_out: str
) -> list[int]:
    """
    Per-night prices in paise across a stay.

    A stay straddling a season boundary is priced night by night, not by
    whichever rate the check-in date landed in — the part naive code gets wrong,
    and the part the guest notices.
    """
    nights = nights_between(check_in, check_out)
    start = _parse_day(check_in)
    out: list[int] = []
    for i in range(nights):
        day = (date.fromordinal(start.toordinal() + i)).isoformat()
        r = rate_for_day(rates, day)
        out.append(to_paise(r["price"] if r else base_price_rupees))
    return out


# ── coupons ──────────────────────────────────────────────────────────────────

COUPON_REJECTIONS = {
    # `unknown` is the one the traveller hits most: a typo, a code that belongs
    # to another property, or a code targeted at someone else's number. None of
    # those resolve to a row at all, so they never reach `coupon_rejection`
    # below — the caller sets this one itself. Deliberately vague: which rule
    # was tripped is not the traveller's business, and saying "that code is for
    # a different hotel" leaks the partner's targeting.
    "unknown": "That code isn't valid for this booking.",
    "inactive": "This code is no longer active.",
    "not_started": "This code isn't valid yet.",
    "expired": "This code has expired.",
    "exhausted": "This code has been fully claimed.",
    "below_minimum": "Your total is below the minimum for this code.",
}


def _parse_ts(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def coupon_rejection(
    coupon: dict, gross_paise: int, now: Optional[datetime] = None
) -> Optional[str]:
    """
    Why a coupon does not apply, or None when it does.

    Kept separate from the discount maths so checkout can tell a traveller which
    rule they tripped instead of silently charging full price.
    """
    now = now or datetime.now(timezone.utc)
    if not coupon.get("active", True):
        return "inactive"
    starts = _parse_ts(coupon.get("starts_at"))
    ends = _parse_ts(coupon.get("ends_at"))
    if starts and now < starts:
        return "not_started"
    if ends and now >= ends:
        return "expired"
    limit = coupon.get("usage_limit")
    if limit is not None and coupon.get("used_count", 0) >= limit:
        return "exhausted"
    minimum = coupon.get("min_amount")
    if minimum is not None and gross_paise < to_paise(minimum):
        return "below_minimum"
    return None


def coupon_discount_paise(
    coupon: dict, gross_paise: int, now: Optional[datetime] = None
) -> int:
    """
    The discount a coupon takes off a gross amount, in paise.

    Clamped to the gross so a ₹2,000 flat coupon on a ₹1,500 order discounts
    ₹1,500 and no more — a negative amount would sail into Razorpay. Returns 0
    for any coupon that doesn't apply.
    """
    if coupon_rejection(coupon, gross_paise, now):
        return 0

    if coupon["kind"] == "flat":
        discount = to_paise(coupon["value"])
    else:
        discount = _round_half_up(gross_paise * coupon["value"] / 100)
        cap = coupon.get("max_discount")
        if cap is not None:
            discount = min(discount, to_paise(cap))

    return max(0, min(discount, gross_paise))


# ── the advance ──────────────────────────────────────────────────────────────
#
# A partner may take a deposit instead of the full price, with the balance
# collected by them directly. Commission is charged on the whole booking but can
# only be *collected* out of the advance, so the advance has a floor: without one
# a partner could take ₹1 through us and leave nothing for the fee to come from.
#
# MIN_DEPOSIT_PCT is mirrored by MIN_DEPOSIT_PCT in the website's pricing.ts and
# by CHECK constraints on partner_listings and group_trips. All three must agree.

MIN_DEPOSIT_PCT = 20


def advance_paise(total_paise: int, deposit: Optional[dict] = None) -> int:
    """
    What the traveller pays today, in paise.

    Integer arithmetic throughout: the floor is ceil(total × 20 / 100) written
    as (total × 20 + 99) // 100, matching the JS copy exactly rather than
    approximately — a float ceil could differ by a paisa at a boundary.
    """
    if total_paise <= 0:
        return max(0, total_paise)
    if not deposit:
        return total_paise

    kind = deposit.get("type") or deposit.get("deposit_type") or "none"
    value = deposit.get("value")
    if value is None:
        value = deposit.get("deposit_value")
    if kind == "none" or not value:
        return total_paise

    if kind == "percent":
        raw = _round_half_up(total_paise * float(value) / 100)
    else:
        raw = to_paise(float(value))

    floor = (total_paise * MIN_DEPOSIT_PCT + 99) // 100
    advance = max(raw, floor)

    # Whole rupees. 25% of ₹11,999 is ₹2,999.75, and nobody wants to be charged
    # 75 paise or to read a "Pay ₹3,000" button that bills something else.
    # Rounded UP so the advance never dips back below the floor, then clamped.
    return min(total_paise, ((advance + 99) // 100) * 100)


# ── the quote ────────────────────────────────────────────────────────────────


@dataclass
class Quote:
    gross_paise: int
    discount_paise: int
    #: gross - discount. What the booking is worth in full.
    total_paise: int
    #: Charged now: the advance, or the whole total when there isn't one.
    amount_paise: int
    #: total - amount. Due to the partner directly; we take no cut of it.
    balance_paise: int
    commission_paise: int
    payout_paise: int
    coupon_rejection: Optional[str]


def quote(
    unit_prices_paise: list[int],
    quantity: int,
    commission_pct: float,
    coupon: Optional[dict] = None,
    deposit: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> Quote:
    """
    Turn a listing plus a traveller's choices into money.

    Everything downstream — the Razorpay order, the booking row, the payout
    ledger — reads from this result rather than recomputing, so the numbers
    cannot disagree with each other.
    """
    qty = max(1, int(quantity))
    gross = sum(unit_prices_paise) * qty

    rejection = coupon_rejection(coupon, gross, now) if coupon else None
    discount = coupon_discount_paise(coupon, gross, now) if coupon else 0
    total = gross - discount

    # The discount comes off the TOTAL, not off the advance — a traveller paying
    # 30% up front still sees the whole coupon value, and the partner's balance
    # shrinks by the rest of it.
    amount = advance_paise(total, deposit)

    # Commission is a cut of the WHOLE booking, not of the slice we happen to
    # hold: 10% of a ₹7,999 seat is ₹800 whether the traveller pays ₹1,600 now
    # or all of it. We take it out of the advance and hand the partner the rest,
    # who then collects the balance directly.
    #
    # Capped at the advance, so a partner is never invoiced for more money than
    # passed through us — with the 20% floor and a 10% rate the cap can't bind,
    # but a listing carrying a rate above its advance would otherwise produce a
    # negative payout.
    commission = min(amount, _round_half_up(total * commission_pct / 100))
    payout = amount - commission

    return Quote(
        gross_paise=gross,
        discount_paise=discount,
        total_paise=total,
        amount_paise=amount,
        balance_paise=total - amount,
        commission_paise=commission,
        payout_paise=payout,
        coupon_rejection=rejection,
    )


def quote_stay(
    base_price_rupees: float,
    season_rates: list[dict],
    check_in: str,
    check_out: str,
    rooms: int,
    commission_pct: float,
    coupon: Optional[dict] = None,
    deposit: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> Quote:
    """Convenience for stays: prices each night, then quotes it."""
    return quote(
        unit_prices_paise=nightly_prices(base_price_rupees, season_rates, check_in, check_out),
        quantity=rooms,
        commission_pct=commission_pct,
        coupon=coupon,
        deposit=deposit,
        now=now,
    )


# ── cancellation ─────────────────────────────────────────────────────────────


def refund_pct_for(
    tiers: list[dict], travel_date: str, cancelled_at: Optional[datetime] = None
) -> float:
    """
    The refund percentage that applies to a cancellation.

    Tiers are sorted here rather than trusting input order — a partner editing
    them in the dashboard won't keep them ordered, and the wrong tier winning is
    a refund dispute.
    """
    if not tiers:
        return 0.0
    cancelled_at = cancelled_at or datetime.now(timezone.utc)
    travel = _parse_day(travel_date)
    travel_dt = datetime(travel.year, travel.month, travel.day, tzinfo=timezone.utc)
    hours_before = (travel_dt - cancelled_at).total_seconds() / 3600
    for tier in sorted(tiers, key=lambda t: t["hours_before"], reverse=True):
        if hours_before >= tier["hours_before"]:
            return float(tier["refund_pct"])
    return 0.0


def refund_paise(
    amount_paise: int,
    tiers: list[dict],
    travel_date: str,
    cancelled_at: Optional[datetime] = None,
) -> int:
    """Refund due on an already-paid booking, in paise."""
    pct = refund_pct_for(tiers, travel_date, cancelled_at)
    return min(amount_paise, _round_half_up(amount_paise * pct / 100))
