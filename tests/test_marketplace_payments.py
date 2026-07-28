"""
Money-path tests.

Ported from the website's lib/marketplace/payments.test.ts when checkout moved
to FastAPI. These cover the parts where a bug costs real rupees: the commission
split, and the HMAC checks that decide whether a payment is genuine.

The signature checks are the whole authentication story for /checkout/verify
and /webhook — without them anyone could POST a made-up payment id and mark a
booking paid — so they are tested against forged, replayed and tampered input
rather than only the happy path.
"""
import asyncio
import hashlib
import hmac
import json
import logging

import pytest

from app.api.v1.marketplace.payments import (
    booking_ref,
    resolve_tier_price,
    split_commission,
)
from app.services import razorpay

SECRET = "test_secret_key"


@pytest.fixture
def with_secrets(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_x", raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", SECRET, raising=False)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SECRET, raising=False)
    return settings


@pytest.fixture
def without_secrets(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "razorpay_key_id", None, raising=False)
    monkeypatch.setattr(settings, "razorpay_key_secret", None, raising=False)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", None, raising=False)
    return settings


def sign(payload: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


# ── commission ───────────────────────────────────────────────────────────────


class TestSplitCommission:
    def test_takes_exactly_ten_percent(self):
        assert split_commission(100_000, 10) == (10_000, 90_000)

    def test_always_sums_back_to_the_original(self):
        # Rounding must never create or destroy money.
        for amount in [1, 7, 99, 333, 100_000, 920_000, 12_345_678]:
            for pct in [0, 10, 12.5, 33.33]:
                commission, payout = split_commission(amount, pct)
                assert commission + payout == amount

    def test_gives_the_partner_everything_at_zero_percent(self):
        # The stay model: subscription, not commission.
        assert split_commission(249_900, 0) == (0, 249_900)

    def test_rounds_the_commission_not_the_payout(self):
        commission, payout = split_commission(333, 10)
        assert commission == 33
        assert payout == 300


# ── payment signature ────────────────────────────────────────────────────────


class TestPaymentSignature:
    def test_accepts_a_genuine_signature(self, with_secrets):
        sig = sign("order_1|pay_1")
        assert razorpay.verify_payment_signature("order_1", "pay_1", sig) is True

    def test_rejects_a_forged_signature(self, with_secrets):
        assert razorpay.verify_payment_signature("order_1", "pay_1", "deadbeef") is False

    def test_rejects_a_signature_made_with_the_wrong_secret(self, with_secrets):
        sig = sign("order_1|pay_1", "not_the_secret")
        assert razorpay.verify_payment_signature("order_1", "pay_1", sig) is False

    def test_rejects_a_valid_signature_replayed_onto_another_order(self, with_secrets):
        # The signature covers order|payment, so it cannot be moved between orders.
        sig = sign("order_1|pay_1")
        assert razorpay.verify_payment_signature("order_2", "pay_1", sig) is False

    def test_rejects_empty_inputs_instead_of_throwing(self, with_secrets):
        assert razorpay.verify_payment_signature("", "", "") is False

    def test_fails_closed_when_no_secret_is_configured(self, without_secrets):
        # A missing secret must never mean "accept everything".
        sig = sign("order_1|pay_1")
        assert razorpay.verify_payment_signature("order_1", "pay_1", sig) is False

    def test_a_forged_signature_is_not_logged_as_an_error(self, with_secrets, caplog):
        # The endpoint is unauthenticated, so anyone can force this branch.
        # Logging it at ERROR turns a stray curl into a Sentry issue.
        with caplog.at_level(logging.DEBUG, logger="app.services.razorpay"):
            razorpay.verify_payment_signature("order_1", "pay_1", "deadbeef")
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_a_missing_secret_is_logged_as_an_error(self, without_secrets, caplog):
        # This one *is* our fault and breaks every genuine payment.
        with caplog.at_level(logging.DEBUG, logger="app.services.razorpay"):
            razorpay.verify_payment_signature("order_1", "pay_1", sign("order_1|pay_1"))
        assert [r for r in caplog.records if r.levelno >= logging.ERROR]


# ── webhook signature ────────────────────────────────────────────────────────


class TestWebhookSignature:
    def test_accepts_a_genuine_webhook(self, with_secrets):
        raw = b'{"event":"payment.captured"}'
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert razorpay.verify_webhook_signature(raw, sig) is True

    def test_rejects_a_tampered_body(self, with_secrets):
        raw = b'{"event":"payment.captured"}'
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert razorpay.verify_webhook_signature(b'{"event":"payment.failed"}', sig) is False

    def test_rejects_a_missing_signature_header(self, with_secrets):
        assert razorpay.verify_webhook_signature(b"{}", None) is False

    def test_fails_closed_when_the_webhook_secret_is_unset(self, without_secrets):
        raw = b"{}"
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        assert razorpay.verify_webhook_signature(raw, sig) is False

    def test_is_not_fooled_by_a_re_serialised_body(self, with_secrets):
        # This is why the endpoint reads request.body() and never re-dumps the
        # parsed JSON: re-serialising changes the bytes and the HMAC with them.
        raw = b'{"a":1,  "b":2}'
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        reserialised = json.dumps(json.loads(raw)).encode()

        assert razorpay.verify_webhook_signature(raw, sig) is True
        assert razorpay.verify_webhook_signature(reserialised, sig) is False


# ── price resolution ─────────────────────────────────────────────────────────


class TestResolveTierPrice:
    def test_honours_a_tier_that_exists(self):
        content = {"pricing": [{"title": "Twin sharing", "amount": 12000}]}
        assert resolve_tier_price(content, "Twin sharing") == 12000

    def test_matches_case_insensitively(self):
        content = {"pricing_tiers": [{"title": "Deluxe", "amount": 9000}]}
        assert resolve_tier_price(content, "  deluxe ") == 9000

    def test_ignores_an_unknown_tier(self):
        # An unknown tier must fall through to price_from, never be honoured.
        content = {"pricing": [{"title": "Twin sharing", "amount": 12000}]}
        assert resolve_tier_price(content, "Free please") is None

    def test_ignores_a_non_positive_amount(self):
        content = {"pricing": [{"title": "Scam", "amount": 0}]}
        assert resolve_tier_price(content, "Scam") is None

    def test_survives_malformed_content(self):
        assert resolve_tier_price(None, "x") is None
        assert resolve_tier_price({}, "x") is None
        assert resolve_tier_price({"pricing": "not-a-list"}, "x") is None
        assert resolve_tier_price({"pricing": ["not-a-dict"]}, "x") is None


class TestBookingRef:
    def test_has_the_expected_shape(self):
        ref = booking_ref()
        assert ref.startswith("TB-")
        assert len(ref) == 9

    def test_avoids_ambiguous_characters(self):
        # No O/0 or I/1 — refs get read out over the phone.
        for _ in range(200):
            assert not set(booking_ref()[3:]) & set("O0I1")


# ── departures ───────────────────────────────────────────────────────────────
#
# The suite has no async plugin and doesn't need one: these drive the coroutine
# with asyncio.run, which is the whole of what a plugin would do here.


class _FakeResult:
    def __init__(self, data):
        self.data = data


@pytest.fixture
def departures(monkeypatch):
    """
    Stub the departures query so _resolve_departure can be tested for what it
    actually decides — which date a booking lands on — without a database.
    """
    rows: list[dict] = []

    async def fake_run_db(_fn):
        return _FakeResult(rows)

    from app.api.v1.marketplace import payments as mod

    monkeypatch.setattr(mod, "run_db", fake_run_db)
    monkeypatch.setattr(mod, "get_db", lambda: None)
    return rows


def _dep(dep_id: str, date: str = "2026-09-12", seats_left: int = 10, price=None) -> dict:
    return {
        "id": dep_id,
        "departure_date": date,
        "return_date": None,
        "label": None,
        "seats_total": 20,
        "seats_left": seats_left,
        "price_from": price,
    }


class TestResolveDeparture:
    def test_returns_none_when_the_trip_has_no_departures(self, departures):
        from app.api.v1.marketplace.payments import _resolve_departure

        assert asyncio.run(_resolve_departure({"id": "t1"}, None)) is None

    def test_auto_selects_the_only_open_date(self, departures):
        # The pre-departures flow: one date, no id sent, and every link already
        # in the wild keeps working.
        from app.api.v1.marketplace.payments import _resolve_departure

        departures.append(_dep("d1"))
        assert asyncio.run(_resolve_departure({"id": "t1"}, None))["id"] == "d1"

    def test_refuses_to_guess_between_two_dates(self, departures):
        from app.api.v1.marketplace.payments import PricingError, _resolve_departure

        departures.extend([_dep("d1"), _dep("d2", date="2026-10-03")])
        with pytest.raises(PricingError):
            asyncio.run(_resolve_departure({"id": "t1"}, None))

    def test_rejects_a_departure_that_is_not_on_this_trip(self, departures):
        from app.api.v1.marketplace.payments import PricingError, _resolve_departure

        departures.append(_dep("d1"))
        with pytest.raises(PricingError):
            asyncio.run(_resolve_departure({"id": "t1"}, "d-somebody-elses"))

    def test_departure_price_overrides_the_trip_price(self, departures):
        from app.api.v1.marketplace.payments import _apply_departure

        departures.append(_dep("d1", price=7999))
        listing = {"id": "t1", "price_from": 8999}
        asyncio.run(_apply_departure(listing, "d1"))
        assert listing["price_from"] == 7999

    def test_a_date_without_its_own_price_leaves_the_trip_price_alone(self, departures):
        from app.api.v1.marketplace.payments import _apply_departure

        departures.append(_dep("d1"))
        listing = {"id": "t1", "price_from": 8999}
        asyncio.run(_apply_departure(listing, "d1"))
        assert listing["price_from"] == 8999


# ── subscription signatures ──────────────────────────────────────────────────


class TestSubscriptionSignature:
    """
    The subscription callback signs `payment_id|subscription_id` — the REVERSE
    of the order flow's `order_id|payment_id`. Getting it backwards rejects
    every genuine mandate silently, so both directions are pinned here.
    """

    def test_accepts_a_genuine_subscription_signature(self, with_secrets):
        sig = sign("pay_123|sub_456")
        assert razorpay.verify_subscription_signature("sub_456", "pay_123", sig)

    def test_rejects_the_order_style_ordering(self, with_secrets):
        # sub|pay instead of pay|sub — the mistake this test exists to catch.
        sig = sign("sub_456|pay_123")
        assert not razorpay.verify_subscription_signature("sub_456", "pay_123", sig)

    def test_rejects_a_signature_for_another_subscription(self, with_secrets):
        sig = sign("pay_123|sub_456")
        assert not razorpay.verify_subscription_signature("sub_999", "pay_123", sig)

    def test_rejects_empty_inputs_instead_of_throwing(self, with_secrets):
        assert not razorpay.verify_subscription_signature("", "", "")

    def test_fails_closed_when_no_secret_is_configured(self, without_secrets):
        sig = hmac.new(b"anything", b"pay_123|sub_456", hashlib.sha256).hexdigest()
        assert not razorpay.verify_subscription_signature("sub_456", "pay_123", sig)
