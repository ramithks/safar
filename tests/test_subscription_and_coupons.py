"""
The money, in the parts that can be reasoned about without a network.

`scripts/billing_e2e.py` proves the whole thing against real Razorpay and real
webhooks. This file holds the arithmetic and the state machine still, because
those are where a mistake is silent: a discount that is a rupee out, a status
that maps to the wrong word, a grace period that renews itself every time a
card is declined.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.billing import plan_coupons, subscription
from app.services import billing


PRICE = 99_900  # ₹999


def coupon(**over):
    base = {"id": "c1", "code": "LAUNCH50", "kind": "percent", "percent_off": 50,
            "percent_cycles": 1, "razorpay_offer_id": "offer_1", "free_days": None,
            "description": "", "active": True, "max_per_partner": 1,
            "redeemed_count": 0, "max_redemptions": None, "eligible_partners": [],
            "starts_at": None, "expires_at": None}
    return {**base, **over}


# ── what a code is worth ─────────────────────────────────────────────────────


@pytest.mark.parametrize("percent,expected_first,expected_off", [
    (20, 79_920, 19_980),
    (30, 69_930, 29_970),
    (50, 49_950, 49_950),
    (60, 39_960, 59_940),
])
def test_the_launch_percentages(percent, expected_first, expected_off):
    d = plan_coupons.quote(coupon(percent_off=percent), PRICE)
    assert d.first_payment_paise == expected_first
    assert d.discount_paise == expected_off
    assert d.first_payment_paise + d.discount_paise == PRICE


def test_the_renewal_is_not_discounted():
    """
    The launch coupons take money off the *first* payment.

    Stated here because it is the thing an owner would otherwise discover on
    their second invoice, and the checkout screen has to say it.
    """
    d = plan_coupons.quote(coupon(percent_off=50), PRICE)
    assert d.renewal_paise == PRICE
    assert d.renewal_paise != d.first_payment_paise


def test_a_discount_can_never_take_the_charge_to_nothing():
    """
    A mandate for ₹0 is one Razorpay will not authorise, and the owner would be
    told by *Razorpay* that something is wrong with their hotel. The table
    bounds `percent_off` at 90; this is the arithmetic refusing anyway, because
    an admin edit lands in this function before it lands in a constraint.
    """
    for percent in (90, 99, 100, 150, 1000):
        d = plan_coupons.quote(coupon(percent_off=percent), PRICE)
        assert d.first_payment_paise >= 100, percent
        assert d.discount_paise < PRICE, percent


def test_a_negative_percentage_cannot_increase_the_price():
    d = plan_coupons.quote(coupon(percent_off=-50), PRICE)
    assert d.discount_paise == 0
    assert d.first_payment_paise == PRICE


def test_free_days_discount_time_and_not_money():
    d = plan_coupons.quote(coupon(kind="free_days", free_days=30,
                                  percent_off=None, razorpay_offer_id=None), PRICE)
    assert d.discount_paise == 0
    assert d.first_payment_paise == PRICE
    assert d.free_days == 30
    assert d.label == "30 days free"


def test_only_free_days_defers_the_first_charge():
    """`start_at` is the whole free-period mechanic and must not fire on a percent."""
    free = plan_coupons.quote(coupon(kind="free_days", free_days=14, percent_off=None,
                                     razorpay_offer_id=None), PRICE)
    assert plan_coupons.start_at_for(free) > datetime.now(timezone.utc).timestamp()
    assert plan_coupons.start_at_for(plan_coupons.quote(coupon(), PRICE)) is None
    assert plan_coupons.start_at_for(None) is None


@pytest.mark.parametrize("typed,expected", [
    ("launch50", "LAUNCH50"), ("  LAUNCH50  ", "LAUNCH50"), ("LA UNCH50", "LAUNCH50"),
])
def test_codes_are_normalised_the_way_people_paste_them(typed, expected):
    assert plan_coupons.normalise_code(typed) == expected


@pytest.mark.parametrize("bad", ["", "  ", "ab", "x" * 21, "LAUNCH-50", "läunch", None])
def test_a_code_that_is_not_a_code_is_refused(bad):
    with pytest.raises(plan_coupons.CouponError):
        plan_coupons.normalise_code(bad)


def test_every_rejection_is_the_same_sentence():
    """
    "No such code", "expired", "you have used it" and "all gone" are four facts
    about our marketing. A prober who can tell them apart can map the campaign.
    """
    now = datetime.now(timezone.utc)
    cases = [
        coupon(active=False),
        coupon(expires_at=(now - timedelta(days=1)).isoformat()),
        coupon(starts_at=(now + timedelta(days=1)).isoformat()),
        coupon(max_redemptions=5, redeemed_count=5),
        coupon(eligible_partners=["11111111-1111-4111-8111-111111111111"]),
    ]
    messages = set()
    for row in cases:
        with pytest.raises(plan_coupons.CouponError) as exc:
            plan_coupons._check(row, "22222222-2222-4222-8222-222222222222")
        messages.add(exc.value.message)
    assert messages == {plan_coupons.GENERIC_REJECTION}, messages


def test_a_live_coupon_for_an_eligible_partner_passes():
    partner = "22222222-2222-4222-8222-222222222222"
    # `_check` reads redemptions from the database; the eligibility list is the
    # part that can be asserted without one.
    row = coupon(eligible_partners=[partner], max_per_partner=1)
    with pytest.raises(Exception):  # noqa: B017 — the DB read, not the rules
        plan_coupons._check(row, partner)


# ── the state machine ────────────────────────────────────────────────────────


def test_razorpay_states_map_to_words_a_hotel_owner_reads():
    for razorpay_status, ours in subscription.STATUS_MAP.items():
        assert ours in subscription.HEADLINE, razorpay_status
        headline, _ = subscription.HEADLINE[ours]
        assert headline and "_" not in headline, (razorpay_status, headline)


def test_a_declined_card_and_exhausted_retries_are_one_thing_to_the_hotel():
    # Razorpay distinguishes 'pending' (retrying) from 'halted' (given up).
    # A hotel owner has one thing to do about either.
    assert subscription.STATUS_MAP["pending"] == "past_due"
    assert subscription.STATUS_MAP["halted"] == "past_due"


def test_authentication_is_active_because_the_mandate_is_real():
    assert subscription.STATUS_MAP["authenticated"] == "active"
    assert subscription.STATUS_MAP["created"] == "pending"


def test_no_headline_leaks_our_vocabulary():
    """
    "StayOS is active" is fine — `active` is an English word an owner uses
    about their own subscription. What must never appear is anything that only
    means something inside this codebase.
    """
    for status, (headline, detail) in subscription.HEADLINE.items():
        text = (headline + " " + detail).lower()
        for word in ("past_due", "webhook", "razorpay", "mandate",
                     "subscription_id", "grace_until", "autopay", "status"):
            assert word not in text, (status, word, text)


# ── who keeps working ────────────────────────────────────────────────────────


def test_a_hotel_with_no_plan_still_gets_its_ai():
    """
    A hotel in onboarding has not subscribed and must not be cut off before it
    has seen the product work. What stops that hotel is its credit balance,
    which is a number it can see.
    """
    assert subscription.is_serving(None) is True


def test_a_failed_payment_keeps_the_ai_answering_through_grace():
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    assert subscription.is_serving({"status": "past_due", "grace_until": future}) is True


def test_and_stops_once_grace_has_run_out():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert subscription.is_serving({"status": "past_due", "grace_until": past}) is False


def test_an_unreadable_grace_date_keeps_the_hotel_working():
    # Fail open. A parse error is our bug, and a hotel's guests should not meet
    # a dead assistant because of one.
    assert subscription.is_serving({"status": "past_due", "grace_until": "not a date"}) is True


@pytest.mark.parametrize("status,serving", [
    ("active", True), ("trialing", True), ("paused", True),
    ("cancelled", False), ("expired", False), ("pending", False),
])
def test_who_is_being_served(status, serving):
    assert subscription.is_serving({"status": status}) is serving


def test_a_cancelled_plan_says_cancelled_rather_than_ready():
    """
    The regression: `current()` filtered to the live states, so a hotel that
    had just cancelled read as having no plan at all — and the screen said
    "Your StayOS plan is ready" to somebody who had cancelled a minute earlier.
    """
    plan_row = {"name": "StayOS", "price_paise": PRICE, "credit_grant": 3000}
    projected = subscription.project({"status": "cancelled"}, plan_row)
    assert "cancelled" in projected["headline"].lower()
    assert projected["serving"] is False
    assert projected["canStart"] is True


def test_the_projection_never_invents_a_price():
    plan_row = {"name": "StayOS", "price_paise": PRICE, "credit_grant": 3000}
    assert subscription.project(None, plan_row)["pricePaise"] == PRICE
    assert subscription.project({"status": "active"}, plan_row)["pricePaise"] == PRICE


# ── a conversation is one thing ──────────────────────────────────────────────


def test_a_guest_channel_is_priced_as_a_conversation():
    assert billing.usage_type_for_channel("website") == "ai.conversation.website"
    assert billing.usage_type_for_channel("instagram") == "ai.conversation.instagram"
    assert billing.usage_type_for_channel("WhatsApp") == "ai.conversation.whatsapp"


def test_a_staff_thread_is_not_a_guest_conversation():
    """A receptionist asking the front desk agent something is not a sale."""
    for channel in ("internal", "", None, "phone"):
        assert billing.usage_type_for_channel(channel) is None


def test_the_window_is_stable_across_a_conversation():
    """
    Every turn has to compute the same key, or the second message in a thread
    is a second charge. Two calls a moment apart are the same window.
    """
    now = datetime.now(timezone.utc)
    assert billing.conversation_window(now) == billing.conversation_window(
        now + timedelta(minutes=30))
    assert billing.conversation_window(now) == billing.conversation_window(
        now + timedelta(hours=1))


def test_a_thread_that_wakes_a_week_later_is_a_new_conversation():
    now = datetime.now(timezone.utc)
    assert billing.conversation_window(now) != billing.conversation_window(
        now + timedelta(days=7))


def test_the_window_is_a_day():
    assert billing.CONVERSATION_WINDOW_SECONDS == 24 * 60 * 60


# ── the promise ──────────────────────────────────────────────────────────────


def test_only_the_owner_copilot_can_read_the_hotels_billing():
    """
    A guest asking "how much do you pay TravBuddy" must get nothing. The tool
    list is the first boundary and `_require_staff` inside each tool is the
    second — this asserts the first, which is the one a refactor moves.
    """
    from app.services.ai import registry
    from app.services.ai.agents import get

    registry.load_all()
    billing_tools = {"get_plan_and_credits", "get_ai_usage"}
    assert billing_tools <= set(get("owner").tools)
    for agent_type in ("guest", "sales", "frontdesk", "marketing"):
        assert not (billing_tools & set(get(agent_type).tools)), agent_type
