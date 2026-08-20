"""
Collecting money for a stay that already exists.

The rules pinned here are the ones that would each cost a real hotel real
money if they regressed, and none of them is visible from a screen:

  * a link never asks for more than is owed
  * a paid stay cannot be asked to pay again
  * two requests for the same balance produce **one** link
  * a purpose the deployment does not recognise is refused, not guessed at
  * nothing in the conversation path can mark a stay paid

The last one is the reason `create_payment_link` exists at all. A guest agent
that could settle a folio on "I have paid" would be a guest agent anybody could
stay for free by lying to.
"""
import asyncio

import pytest

from app.services.ai import registry
from app.services.pms import stay_payment


class FakeTable:
    """Enough of the PostgREST builder for the reads `create_request` makes."""

    def __init__(self, rows, sink=None):
        self._rows, self._sink = rows, sink

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def neq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def insert(self, row):
        if self._sink is not None:
            self._sink.append(row)
        return FakeTable([{**row, "id": "order-row"}])

    def execute(self):
        return type("R", (), {"data": self._rows})()


def fake_db(reservation=None, open_order=None, sink=None):
    def table(name):
        if name == "reservations":
            return FakeTable([reservation] if reservation else [])
        if name == "payment_orders":
            return FakeTable([open_order] if open_order else [], sink=sink)
        return FakeTable([])

    return type("DB", (), {"table": staticmethod(table)})()


RESERVATION = {
    "id": "res-1", "ref": "TB-AAA111", "property_id": "prop-1", "partner_id": "part-1",
    "status": "confirmed", "total_paise": 1_000_000, "paid_paise": 200_000,
    "guest_name": "Anita Rao", "check_in": "2026-09-01", "check_out": "2026-09-03",
}


@pytest.fixture
def razorpay_on(monkeypatch):
    monkeypatch.setattr(stay_payment.razorpay, "configured", lambda: True)
    created = {}

    async def create_order(*, amount_paise, receipt, notes=None):
        created.update(amount_paise=amount_paise, receipt=receipt, notes=notes)
        return {"id": "order_TEST123"}

    monkeypatch.setattr(stay_payment.razorpay, "create_order", create_order)
    return created


def run(coro):
    return asyncio.run(coro)


# ── what it asks for ─────────────────────────────────────────────────────────

def test_it_asks_for_the_outstanding_balance_by_default(monkeypatch, razorpay_on):
    sink = []
    monkeypatch.setattr(stay_payment, "get_db",
                        lambda: fake_db(RESERVATION, sink=sink))

    request = run(stay_payment.create_request(
        property_id="prop-1", reservation_id="res-1",
    ))

    assert request.amount_paise == 800_000  # 10,000 - 2,000
    assert razorpay_on["amount_paise"] == 800_000
    assert sink[0]["reservation_id"] == "res-1"
    assert sink[0]["purpose"] == "stay_balance"


def test_a_larger_amount_is_clamped_to_the_balance(monkeypatch, razorpay_on):
    """
    "Send them a link for twenty thousand" on a stay that owes eight is a
    hotelier rounding, not an instruction to overcharge. Clamping is the only
    reading that cannot end in a refund.
    """
    monkeypatch.setattr(stay_payment, "get_db", lambda: fake_db(RESERVATION))
    request = run(stay_payment.create_request(
        property_id="prop-1", reservation_id="res-1", amount_paise=2_000_000,
    ))
    assert request.amount_paise == 800_000


def test_a_part_payment_is_allowed(monkeypatch, razorpay_on):
    monkeypatch.setattr(stay_payment, "get_db", lambda: fake_db(RESERVATION))
    request = run(stay_payment.create_request(
        property_id="prop-1", reservation_id="res-1", amount_paise=100_000,
    ))
    assert request.amount_paise == 100_000


# ── what it refuses ──────────────────────────────────────────────────────────

def test_a_fully_paid_stay_cannot_be_asked_to_pay_again(monkeypatch, razorpay_on):
    paid = {**RESERVATION, "paid_paise": RESERVATION["total_paise"]}
    monkeypatch.setattr(stay_payment, "get_db", lambda: fake_db(paid))
    with pytest.raises(stay_payment.PaymentRequestError, match="already paid"):
        run(stay_payment.create_request(property_id="prop-1", reservation_id="res-1"))


def test_a_cancelled_stay_is_refused(monkeypatch, razorpay_on):
    monkeypatch.setattr(stay_payment, "get_db",
                        lambda: fake_db({**RESERVATION, "status": "cancelled"}))
    with pytest.raises(stay_payment.PaymentRequestError, match="cancelled"):
        run(stay_payment.create_request(property_id="prop-1", reservation_id="res-1"))


def test_an_unknown_reservation_is_refused(monkeypatch, razorpay_on):
    monkeypatch.setattr(stay_payment, "get_db", lambda: fake_db(None))
    with pytest.raises(stay_payment.PaymentRequestError, match="couldn't find"):
        run(stay_payment.create_request(property_id="prop-1", reservation_id="nope"))


def test_an_amount_below_a_rupee_is_refused(monkeypatch, razorpay_on):
    monkeypatch.setattr(stay_payment, "get_db", lambda: fake_db(RESERVATION))
    with pytest.raises(stay_payment.PaymentRequestError, match="too small"):
        run(stay_payment.create_request(
            property_id="prop-1", reservation_id="res-1", amount_paise=50,
        ))


def test_it_refuses_cleanly_when_razorpay_is_not_configured(monkeypatch):
    monkeypatch.setattr(stay_payment.razorpay, "configured", lambda: False)
    monkeypatch.setattr(stay_payment, "get_db", lambda: fake_db(RESERVATION))
    with pytest.raises(stay_payment.PaymentRequestError, match="aren't switched on"):
        run(stay_payment.create_request(property_id="prop-1", reservation_id="res-1"))


# ── asking twice ─────────────────────────────────────────────────────────────

def test_asking_twice_returns_the_same_link(monkeypatch, razorpay_on):
    """
    Two live orders against one balance is how a couple who both tapped pay for
    the room twice. An open link for the same amount is reused, and Razorpay is
    never called a second time.
    """
    open_order = {"razorpay_order_id": "order_EXISTING", "status": "created",
                  "amount_paise": 800_000, "reservation_id": "res-1"}
    monkeypatch.setattr(stay_payment, "get_db",
                        lambda: fake_db(RESERVATION, open_order=open_order))

    request = run(stay_payment.create_request(
        property_id="prop-1", reservation_id="res-1",
    ))
    assert request.razorpay_order_id == "order_EXISTING"
    assert razorpay_on == {}, "Razorpay was called for a link that already existed"


def test_the_pay_url_points_at_the_hotel_app_not_razorpay():
    url = stay_payment.pay_url("order_XYZ")
    assert url.endswith("/pay/order_XYZ")
    assert "razorpay" not in url


# ── the tools the agent is given ─────────────────────────────────────────────

def test_the_three_stay_tools_are_registered():
    registry.load_all()
    for name in ("create_payment_link", "get_check_in_link", "order_service"):
        assert name in registry.REGISTRY, f"{name} is defined but not registered"


def test_the_guest_agent_can_take_a_payment_and_book_an_extra():
    from app.services.ai.agents import get

    tools = set(get("guest").tools)
    assert {"create_payment_link", "get_check_in_link", "order_service"} <= tools


def test_no_tool_anywhere_can_mark_a_stay_paid():
    """
    The load-bearing one. `paid_paise` moves only when a signed Razorpay
    webhook settles an order — so no tool may hold `folio`, which is the scope
    that changes a bill, and none may be named as though it confirms a payment.
    """
    registry.load_all()
    from app.services.ai.agents import get

    for name in get("guest").tools:
        spec = registry.REGISTRY[name]
        assert spec.permission != "folio", f"{name} can change a guest's bill"


def test_ordering_an_extra_needs_a_higher_autonomy_than_answering():
    """
    Putting a charge on somebody's bill is not the same act as telling them the
    check-in time. At the guest agent's shipping level of 2 this must produce
    an approval request rather than a charge.
    """
    registry.load_all()
    from app.services.ai.agents import get

    assert registry.REGISTRY["order_service"].autonomy_floor > get("guest").autonomy_level
    assert registry.REGISTRY["get_check_in_link"].mutating is False


# ── settling ─────────────────────────────────────────────────────────────────

def test_every_purpose_the_database_allows_has_a_way_to_be_fulfilled():
    """
    The regression this file was written for.

    The webhook used to read `if purpose == 'booking' … else credits`, so the
    day a third purpose was added it was handed to the credits path, threw on a
    null billing account, released the order and made Razorpay retry the same
    failure for a day. Any purpose the check constraint admits must have an
    entry here.
    """
    import os

    os.environ.setdefault("ENABLE_HOTEL_OS", "true")
    from app.api.v1.pms.endpoint import FULFIL

    # Mirrors payment_orders_purpose_check.
    allowed = {"booking", "credits", "stay_balance"}
    assert allowed <= set(FULFIL), f"no fulfilment for {allowed - set(FULFIL)}"


def test_an_unrecognised_purpose_is_not_treated_as_a_credit_purchase():
    import os

    os.environ.setdefault("ENABLE_HOTEL_OS", "true")
    from app.api.v1.pms.endpoint import FULFIL

    assert FULFIL.get("something_new_in_a_later_migration") is None


def test_settling_is_idempotent_on_the_razorpay_order_id(monkeypatch):
    """
    The browser and the webhook both settle, and Razorpay retries. The folio
    key is derived from the order id — not the clock, not a uuid — so a
    payment posted three times appears on the bill once.
    """
    seen = []

    async def post_folio_entry(property_id, reservation_id, **kw):
        seen.append(kw["idempotency_key"])
        return {"id": "entry-1"}

    monkeypatch.setattr(stay_payment.frontdesk, "post_folio_entry", post_folio_entry)

    order = {"id": "o1", "razorpay_order_id": "order_ABC", "property_id": "prop-1",
             "reservation_id": "res-1", "amount_paise": 800_000}
    run(stay_payment.settle(order))
    run(stay_payment.settle(order))

    assert seen == ["stay-payment:order_ABC"] * 2, "the key must not vary between calls"


def test_settling_an_order_with_no_reservation_is_refused(monkeypatch):
    """
    A stay_balance order without a reservation has no folio to post to. The
    database refuses to create one; this refuses to settle one if it ever did.
    """
    order = {"id": "o1", "razorpay_order_id": "order_ABC", "property_id": "prop-1",
             "reservation_id": None, "amount_paise": 100}
    with pytest.raises(RuntimeError, match="no reservation"):
        run(stay_payment.settle(order))
