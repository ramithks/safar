"""
Regression tests for the 2026-07-25 production-hardening pass.

Every test here pins a bug that cost real money or lost a real booking, and
each one fails against the code as it was. They use a small in-memory stand-in
for the PostgREST client rather than a live database, because the properties
under test are about control flow — "did we extend the plan twice", "did we
refuse to settle a paid booking" — not about SQL.
"""
import asyncio

import pytest

from app.api.v1.marketplace.payments import normalise_coupon_code
from app.services import marketplace_bookings as mb


# ── a very small PostgREST stand-in ──────────────────────────────────────────
#
# Supports only the operators these code paths actually use. Filters are applied
# in order, which is enough because every query here is a primary-key or
# unique-column lookup.


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table, op, payload=None):
        self.db, self.table, self.op, self.payload = db, table, op, payload
        self.filters = []
        self.single = False
        self.limit_n = None

    # filters
    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def neq(self, col, val):
        self.filters.append(lambda r: r.get(col) != val)
        return self

    def is_(self, col, val):
        assert val == "null", f"fake only models is_(col, 'null'), got {val!r}"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def gte(self, col, val):
        # Dates are ISO strings here, where lexical and chronological order agree.
        self.filters.append(lambda r: r.get(col) is not None and r[col] >= val)
        return self

    @property
    def not_(self):
        return _Negated(self)

    def select(self, *_a, **_k):
        return self

    def maybe_single(self):
        self.single = True
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def _matching(self):
        rows = self.db.rows.get(self.table, [])
        return [r for r in rows if all(f(r) for f in self.filters)]

    def execute(self):
        self.db.calls.append((self.op, self.table))
        if self.op == "select":
            rows = self._matching()
            if self.single:
                if len(rows) > 1:
                    raise RuntimeError("maybe_single matched multiple rows")
                return _Result(rows[0] if rows else None)
            if self.limit_n is not None:
                rows = rows[: self.limit_n]
            return _Result(rows)

        if self.op == "insert":
            row = dict(self.payload)
            for col in self.db.unique.get(self.table, []):
                value = row.get(col)
                if value is not None and any(
                    r.get(col) == value for r in self.db.rows.get(self.table, [])
                ):
                    raise RuntimeError(f"duplicate key value violates unique constraint on {col}")
            self.db.rows.setdefault(self.table, []).append(row)
            return _Result([row])

        if self.op == "update":
            hit = self._matching()
            for row in hit:
                row.update(self.payload)
            return _Result(hit)

        raise AssertionError(self.op)


class _Negated:
    def __init__(self, q):
        self.q = q

    def in_(self, col, vals):
        self.q.filters.append(lambda r: r.get(col) not in vals)
        return self.q


class FakeDB:
    def __init__(self, rows=None, unique=None):
        self.rows = rows or {}
        self.unique = unique or {}
        self.calls = []
        self.rpcs = []

    def table(self, name):
        return _Table(self, name)

    def rpc(self, name, args):
        self.rpcs.append((name, args))
        return _Rpc()


class _Rpc:
    def execute(self):
        return _Result(1)


class _Table:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def select(self, *_a, **_k):
        return _Query(self.db, self.name, "select")

    def insert(self, payload):
        return _Query(self.db, self.name, "insert", payload)

    def update(self, payload):
        return _Query(self.db, self.name, "update", payload)


@pytest.fixture
def fake_db(monkeypatch):
    def _install(db):
        monkeypatch.setattr(mb, "get_db", lambda: db)
        return db

    return _install


def run(coro):
    return asyncio.run(coro)


# ── coupon codes are matched exactly, never as LIKE patterns ─────────────────


class TestCouponCodeNormalisation:
    """
    `_resolve_coupon` used to pass the traveller's input straight into
    PostgREST `ilike`, where `%` and `_` are wildcards. Against live data,
    submitting `HIMALAYA__` applied HIMALAYA10's ₹1,700 discount to someone who
    had never seen the code, and a bare `%` matched every coupon the partner
    had and 500'd the quote endpoint on `maybe_single`.
    """

    @pytest.mark.parametrize("code", ["%", "_", "HIMALAYA__", "HIMALAYA%", "%10", "STAY_00", "%%"])
    def test_rejects_anything_containing_like_wildcards(self, code):
        assert normalise_coupon_code(code) is None

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("HIMALAYA10", "HIMALAYA10"),
            ("himalaya10", "HIMALAYA10"),
            ("  stay500  ", "STAY500"),
            ("RHEA20", "RHEA20"),
        ],
    )
    def test_accepts_and_upper_cases_real_codes(self, given, expected):
        assert normalise_coupon_code(given) == expected

    @pytest.mark.parametrize("code", [None, "", "  ", "AB", "A" * 21, "CODE WITH SPACE", "co;de"])
    def test_rejects_everything_that_is_not_a_code(self, code):
        assert normalise_coupon_code(code) is None


# ── one payment buys exactly one month ───────────────────────────────────────


class TestSubscriptionChargeIsIdempotent:
    """
    The browser callback (/subscribe PUT, invoice_id=None) and the
    `subscription.charged` webhook (invoice_id set) both report the SAME first
    payment. Keying idempotency on the invoice alone meant the webhook re-ran
    the whole body, and the plan extension ran even though the ledger insert
    had been rejected as a duplicate — so every new subscriber got two months
    for ₹499, and every webhook replay added another.
    """

    def _db(self):
        return FakeDB(
            rows={
                "partners": [{"id": "p1", "subscription_id": "sub_1"}],
                "partner_payments": [],
            },
            unique={
                "partner_payments": ["razorpay_payment_id", "razorpay_invoice_id"],
            },
        )

    def test_browser_callback_then_webhook_grants_one_month(self, fake_db, monkeypatch):
        db = fake_db(self._db())
        months_seen = []

        async def fake_activate(partner_id, months=1):
            months_seen.append(months)
            return True

        monkeypatch.setattr(mb, "activate_stay_plan", fake_activate)

        # 1. the browser comes back from Checkout — no invoice id exists yet
        run(mb.record_subscription_charge("sub_1", "pay_1", invoice_id=None, amount_paise=49900))
        # 2. Razorpay's webhook reports the same charge, this time with an invoice
        run(mb.record_subscription_charge("sub_1", "pay_1", invoice_id="inv_1", amount_paise=49900))

        assert len(months_seen) == 1, "one ₹499 charge must extend the plan exactly once"
        assert len(db.rows["partner_payments"]) == 1, "and must leave exactly one ledger row"

    def test_webhook_replay_does_not_add_a_month(self, fake_db, monkeypatch):
        db = fake_db(self._db())
        months_seen = []

        async def fake_activate(partner_id, months=1):
            months_seen.append(months)
            return True

        monkeypatch.setattr(mb, "activate_stay_plan", fake_activate)

        for _ in range(4):
            run(
                mb.record_subscription_charge(
                    "sub_1", "pay_1", invoice_id="inv_1", amount_paise=49900
                )
            )

        assert len(months_seen) == 1
        assert len(db.rows["partner_payments"]) == 1

    def test_a_genuine_second_cycle_does_extend(self, fake_db, monkeypatch):
        db = fake_db(self._db())
        months_seen = []

        async def fake_activate(partner_id, months=1):
            months_seen.append(months)
            return True

        monkeypatch.setattr(mb, "activate_stay_plan", fake_activate)

        run(mb.record_subscription_charge("sub_1", "pay_1", invoice_id="inv_1", amount_paise=49900))
        run(mb.record_subscription_charge("sub_1", "pay_2", invoice_id="inv_2", amount_paise=49900))

        assert len(months_seen) == 2, "next month's charge is a new cycle and must extend the plan"
        assert len(db.rows["partner_payments"]) == 2


# ── a failed attempt must not kill a retryable order ─────────────────────────


class TestFailedPaymentThenRetry:
    """
    Razorpay Checkout lets a traveller retry a failed payment against the same
    order. `payment.failed` used to cancel the booking, so the successful retry
    hit a terminal status: no seats claimed, no `paid`, no receipt, no row in
    the partner's dashboard — with the money captured.
    """

    def _booking_db(self, status="created", payment_id=None):
        return FakeDB(
            rows={
                "bookings": [
                    {
                        "id": "b1",
                        "ref": "TB-AAA111",
                        "status": status,
                        "item_type": "hotel",
                        "item_id": "l1",
                        "partner_id": "p1",
                        "travellers": 2,
                        "razorpay_order_id": "order_1",
                        "razorpay_payment_id": payment_id,
                        "coupon_id": None,
                        "departure_id": None,
                        "amount_paise": 100_000,
                        "meta": {"nights": 2},
                    }
                ]
            }
        )

    def test_payment_failed_leaves_the_booking_payable(self, fake_db):
        db = fake_db(self._booking_db())
        run(mb.cancel_unpaid_order("order_1"))

        booking = db.rows["bookings"][0]
        assert booking["status"] == "created", "a failed attempt must not close the order"
        assert booking["last_payment_failed_at"] is not None, "but it should be recorded"

    def test_retry_after_a_failure_still_settles(self, fake_db):
        db = fake_db(self._booking_db())
        run(mb.cancel_unpaid_order("order_1"))

        result = run(mb.finalise_booking("order_1", "pay_retry"))

        assert result["status"] == "paid", "the successful retry must settle the booking"
        assert db.rows["bookings"][0]["status"] == "paid"
        assert db.rows["bookings"][0]["razorpay_payment_id"] == "pay_retry"

    def test_a_booking_cancelled_without_payment_can_still_be_settled(self, fake_db):
        # The shape left behind by the old handler, and by a stray webhook for a
        # booking whose order creation failed.
        db = fake_db(self._booking_db(status="cancelled"))
        result = run(mb.finalise_booking("order_1", "pay_late"))
        assert result["status"] == "paid"

    def test_a_refunded_booking_is_never_resettled(self, fake_db):
        db = fake_db(self._booking_db(status="refunded", payment_id="pay_old"))
        result = run(mb.finalise_booking("order_1", "pay_new"))
        assert result["status"] == "already_paid"
        assert db.rows["bookings"][0]["status"] == "refunded"

    def test_a_cancelled_booking_that_took_money_is_never_resettled(self, fake_db):
        db = fake_db(self._booking_db(status="cancelled", payment_id="pay_old"))
        result = run(mb.finalise_booking("order_1", "pay_new"))
        assert result["status"] == "already_paid"
        assert db.rows["bookings"][0]["status"] == "cancelled"

    def test_settling_twice_only_transitions_once(self, fake_db):
        db = fake_db(self._booking_db())
        first = run(mb.finalise_booking("order_1", "pay_1"))
        second = run(mb.finalise_booking("order_1", "pay_1"))

        assert first["status"] == "paid"
        assert second["status"] == "already_paid", "receipts must not go out twice"


# ── refunds ──────────────────────────────────────────────────────────────────


class TestRefunds:
    def _paid_db(self):
        return FakeDB(
            rows={
                "bookings": [
                    {
                        "id": "b1",
                        "ref": "TB-BBB222",
                        "status": "paid",
                        "payout_status": "held",
                        "razorpay_order_id": "order_1",
                        "razorpay_payment_id": "pay_1",
                        "amount_paise": 100_000,
                        "meta": {"nights": 2, "base_price": 5000, "coupon": "STAY500"},
                    }
                ]
            }
        )

    def test_partial_refund_does_not_close_the_booking(self, fake_db):
        db = fake_db(self._paid_db())
        run(mb.mark_refunded_by_payment("pay_1", refund_paise=50_000))

        booking = db.rows["bookings"][0]
        assert booking["status"] == "paid", "a 50% refund is not a cancellation"
        assert booking["payout_status"] == "held", "the partner is still owed the rest"
        assert booking["meta"]["refunded_paise"] == 50_000

    def test_full_refund_closes_the_booking(self, fake_db):
        db = fake_db(self._paid_db())
        run(mb.mark_refunded_by_payment("pay_1", refund_paise=100_000))

        booking = db.rows["bookings"][0]
        assert booking["status"] == "refunded"
        assert booking["payout_status"] == "na"

    def test_unknown_amount_is_treated_as_a_full_refund(self, fake_db):
        db = fake_db(self._paid_db())
        run(mb.mark_refunded_by_payment("pay_1"))
        assert db.rows["bookings"][0]["status"] == "refunded"

    def test_refund_keeps_the_pricing_audit_trail(self, fake_db):
        db = fake_db(self._paid_db())
        run(mb.mark_refunded("order_1", "pay_1", refunded=True, reason="seats_unavailable"))

        meta = db.rows["bookings"][0]["meta"]
        assert meta["refund_reason"] == "seats_unavailable"
        # The evidence a dispute needs must survive the refund that triggers it.
        assert meta["base_price"] == 5000
        assert meta["coupon"] == "STAY500"
        assert meta["nights"] == 2


# ── a lapsed plan, and what paying again undoes ──────────────────────────────


class TestRenewalRestoresInventory:
    """
    The lapse policy is deliberately asymmetric (see
    20260726_plan_lapse_policy.sql): stays and experiences are unpublished,
    unbooked trips are unpublished, and a trip with travellers on it stays
    published with `bookings_closed` set. Paying again has to undo all three,
    or a partner pays ₹499 and their trips stay dark.
    """

    def _lapsed_db(self):
        return FakeDB(
            rows={
                "partners": [{"id": "p1", "stay_plan_expires_at": None}],
                "partner_listings": [
                    {"id": "l1", "partner_id": "p1", "status": "expired"},
                    {"id": "l2", "partner_id": "p1", "status": "published"},
                ],
                "group_trips": [
                    # retired because nobody had booked it
                    {
                        "id": "t1",
                        "partner_id": "p1",
                        "status": "expired",
                        "bookings_closed": False,
                        "departure_date": "2099-01-01",
                    },
                    # stayed up for its travellers, but stopped selling
                    {
                        "id": "t2",
                        "partner_id": "p1",
                        "status": "published",
                        "bookings_closed": True,
                        "departure_date": "2099-01-01",
                    },
                    # already departed — must NOT be resurrected
                    {
                        "id": "t3",
                        "partner_id": "p1",
                        "status": "expired",
                        "bookings_closed": False,
                        "departure_date": "2020-01-01",
                    },
                ],
            }
        )

    def test_paying_again_republishes_and_reopens(self, fake_db):
        db = fake_db(self._lapsed_db())
        assert run(mb.activate_stay_plan("p1", months=1)) is True

        listings = {r["id"]: r for r in db.rows["partner_listings"]}
        trips = {r["id"]: r for r in db.rows["group_trips"]}

        assert listings["l1"]["status"] == "published", "an expired stay comes back"
        assert trips["t1"]["status"] == "published", "an expired trip comes back"
        assert trips["t2"]["bookings_closed"] is False, "a closed trip sells again"

    def test_a_departed_trip_is_not_resurrected(self, fake_db):
        db = fake_db(self._lapsed_db())
        run(mb.activate_stay_plan("p1", months=1))

        trips = {r["id"]: r for r in db.rows["group_trips"]}
        assert trips["t3"]["status"] == "expired", "a trip that already left stays gone"

    def test_the_plan_itself_is_switched_back_on(self, fake_db):
        db = fake_db(self._lapsed_db())
        run(mb.activate_stay_plan("p1", months=1))
        assert db.rows["partners"][0]["stay_plan_status"] == "active"
        assert db.rows["partners"][0]["stay_plan_expires_at"] is not None
