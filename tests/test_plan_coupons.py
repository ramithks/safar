"""
Plan coupons — the discount codes we issue against the ₹499 partner plan.

The money-losing failure mode here is not "the discount didn't apply", it is
"the discount applied twice" or "we ledgered a charge nobody paid". A
free-months mandate has NO first charge — `start_at` defers it — so the two
paths that learn a mandate authenticated (browser callback, webhook) must
agree about that, in either order, or the partner gets a phantom ₹499 row and
an extra month on top of their free period.

Reuses the in-memory PostgREST stand-in from the hardening suite: the
properties under test are control flow, not SQL.
"""
import pytest

from app.api.v1.marketplace import payments
from app.services import marketplace_bookings as mb
from app.services import partner_terms
from app.services import plan_coupons
from tests.test_marketplace_hardening import FakeDB, run


PARTNER = "11111111-1111-1111-1111-111111111111"
COUPON = "22222222-2222-2222-2222-222222222222"
SUB = "sub_TEST"


@pytest.fixture
def wired(monkeypatch):
    """Point every module that reaches for a DB client at one fake."""

    def _install(db):
        monkeypatch.setattr(plan_coupons, "get_db", lambda: db)
        monkeypatch.setattr(payments, "get_db", lambda: db)
        monkeypatch.setattr(mb, "get_db", lambda: db)
        # `settle_authenticated_mandate` learned to read the partner's terms
        # after this fixture was written, and `partner_terms` reaches for its
        # own client. Unpatched, the ordinary-charge test left the fake and hit
        # the real database — which is what a 204 from postgrest in a unit test
        # means every time.
        monkeypatch.setattr(partner_terms, "get_db", lambda: db)
        return db

    return _install


def _db(*, redemptions=None, pending=COUPON, kind="free_months", value=6):
    return FakeDB(
        rows={
            "partners": [
                {
                    "id": PARTNER,
                    "subscription_id": SUB,
                    "pending_plan_coupon_id": pending,
                    "stay_plan_expires_at": None,
                }
            ],
            "platform_coupons": [
                {
                    "id": COUPON,
                    "code": "FOUNDER6",
                    "kind": kind,
                    "value": value,
                    "active": True,
                    "used_count": 0,
                    "usage_limit": None,
                    "starts_at": None,
                    "ends_at": None,
                    "razorpay_offer_id": None if kind == "free_months" else "offer_x",
                }
            ],
            "platform_coupon_redemptions": redemptions or [],
            "partner_payments": [],
            # No row: `partner_terms.resolve` falls back to `default_terms()`,
            # which carries the ordinary ₹499 fee this test is about.
            "partner_terms": [],
        },
        unique={"partner_payments": ["razorpay_payment_id"]},
    )


# ── code matching ────────────────────────────────────────────────────────────


class TestCodeNormalisation:
    """
    Same guard as the booking coupons, for the same reason: `%` and `_` are
    Postgres wildcards, and a lookup that ever became a LIKE would let
    `LAUNCH_` collect LAUNCH1's free months.
    """

    @pytest.mark.parametrize("code", ["%", "_", "LAUNCH_", "LAUNCH%", "%%", "", None, "ab"])
    def test_rejects_non_codes(self, code):
        assert plan_coupons.normalise_code(code) is None

    def test_upper_cases_valid_codes(self):
        assert plan_coupons.normalise_code(" founder6 ") == "FOUNDER6"


class TestDescribe:
    def test_prefers_the_authored_description(self):
        assert plan_coupons.describe({"description": "Diwali special", "kind": "flat", "value": 1}) == (
            "Diwali special"
        )

    @pytest.mark.parametrize(
        "coupon,expected",
        [
            ({"kind": "free_months", "value": 1}, "1 month free"),
            ({"kind": "free_months", "value": 6}, "6 months free"),
            ({"kind": "percent", "value": 50, "duration_months": 6}, "50% off for 6 months"),
            ({"kind": "flat", "value": 200, "duration_months": 12}, "₹200 off for 12 months"),
            ({"kind": "percent", "value": 25}, "25% off"),
        ],
    )
    def test_renders_the_offer(self, coupon, expected):
        assert plan_coupons.describe(coupon) == expected


# ── redemption is once, and once only ────────────────────────────────────────


class TestRedemption:
    def test_grants_the_free_months_once(self, wired):
        db = wired(_db())
        assert run(plan_coupons.apply_pending(PARTNER, SUB)) == 6
        assert ("redeem_plan_coupon",
                {"p_coupon_id": COUPON, "p_partner_id": PARTNER,
                 "p_subscription_id": SUB, "p_months_granted": 6}) in db.rpcs

    def test_clears_the_pending_pointer(self, wired):
        db = wired(_db())
        run(plan_coupons.apply_pending(PARTNER, SUB))
        assert db.rows["partners"][0]["pending_plan_coupon_id"] is None

    def test_no_pending_coupon_grants_nothing(self, wired):
        wired(_db(pending=None))
        assert run(plan_coupons.apply_pending(PARTNER, SUB)) == 0

    def test_offer_coupons_grant_no_plan_time(self, wired):
        """percent/flat are discounted by Razorpay; we must not also gift months."""
        wired(_db(kind="percent", value=50))
        assert run(plan_coupons.apply_pending(PARTNER, SUB)) == 0

    def test_granted_free_period_sees_an_existing_redemption(self, wired):
        wired(_db(redemptions=[{"subscription_id": SUB, "months_granted": 6}]))
        assert run(plan_coupons.granted_free_period(SUB)) is True

    def test_granted_free_period_ignores_an_offer_redemption(self, wired):
        """A percent coupon redeemed against this mandate did NOT buy free time,
        so the real ₹499 charge still has to be recorded."""
        wired(_db(redemptions=[{"subscription_id": SUB, "months_granted": None}]))
        assert run(plan_coupons.granted_free_period(SUB)) is False


# ── the settle path: a free period is never also a charge ────────────────────


class TestSettleAuthenticatedMandate:
    def test_free_months_records_no_rupee_charge(self, wired, monkeypatch):
        """
        `start_at` deferred the first invoice, so there is nothing to charge
        today. Recording one would both invent a payment and, via
        activate_stay_plan(months=1), stack an extra month on the free period.
        """
        wired(_db())
        charged = []
        monkeypatch.setattr(
            payments, "record_subscription_charge",
            lambda *a, **k: charged.append(a) or _async_true(),
        )
        granted = []
        monkeypatch.setattr(
            payments, "activate_stay_plan",
            lambda pid, months=1: granted.append(months) or _async_true(),
        )
        monkeypatch.setattr(payments, "set_subscription_status", lambda *a, **k: _async_true())

        run(payments.settle_authenticated_mandate(PARTNER, SUB, "pay_1"))

        assert charged == [], "a free-months mandate must not ledger a ₹499 charge"
        assert granted == [6]

    def test_loser_of_the_race_ledgers_nothing(self, wired, monkeypatch):
        """
        The webhook already redeemed the coupon, so `apply_pending` returns 0
        here. Without the granted_free_period guard this would read as "no
        coupon" and record a ₹499 that was never charged — the 2026-07-25
        double-month bug in a new costume.
        """
        wired(_db(pending=None, redemptions=[{"subscription_id": SUB, "months_granted": 6}]))
        charged = []
        monkeypatch.setattr(
            payments, "record_subscription_charge",
            lambda *a, **k: charged.append(a) or _async_true(),
        )

        run(payments.settle_authenticated_mandate(PARTNER, SUB, "pay_1"))
        assert charged == []

    def test_plain_mandate_still_records_its_charge(self, wired, monkeypatch):
        """No coupon anywhere: the ordinary ₹499 path must be untouched."""
        wired(_db(pending=None))
        charged = []
        monkeypatch.setattr(
            payments, "record_subscription_charge",
            lambda *a, **k: charged.append(a) or _async_true(),
        )

        run(payments.settle_authenticated_mandate(PARTNER, SUB, "pay_1"))
        assert charged == [(SUB, "pay_1")]


def _async_true():
    """A already-completed awaitable, so monkeypatched coroutines stay awaitable."""

    async def _done():
        return True

    return _done()
