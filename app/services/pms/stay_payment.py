"""
Asking a guest to pay for a stay that already exists.

    booking engine  ──► quote ──► order ──► reservation      (money first)
    this module     ──► reservation ──► order ──► folio      (room first)

Both end at `payment_orders` and both settle through the same Razorpay webhook.
The difference is which end the reservation is on, and it matters: the booking
engine creates a stay *because* somebody paid, so its fulfilment writes a
reservation; here the stay is already real and fulfilment writes a **payment on
its folio**. Conflating them would either double-book a paid stay or leave a
collected payment off the bill.

**Nothing here decides an amount.** The figure comes from
`total_paise - paid_paise` on the reservation row. A caller may ask for less
than that — a deposit, a token — and may not ask for more, because a link that
overcharges is a link the hotel has to refund. The AI employee is a caller like
any other, so "send me a payment link for ₹1" and "…for ₹100000" are both
answered by the same clamp rather than by the model's judgement.

**A guest saying "I have paid" is not a payment.** The only thing that moves
`paid_paise` is `settle()`, and the only thing that calls `settle()` is a
Razorpay webhook whose signature verified. There is deliberately no path from a
conversation to a settled folio.
"""
import logging
from dataclasses import dataclass

from app.db.client import get_db, run_db
from app.services import razorpay
from app.services.pms import frontdesk

logger = logging.getLogger(__name__)

# `payment_orders.purpose` for a link raised against an existing stay. Kept as a
# constant because the webhook dispatches on it and a typo there is a payment
# that is taken and never posted.
PURPOSE = "stay_balance"

# Razorpay refuses anything under a rupee, and a link for less is not a payment
# request anybody meant to make.
MIN_PAISE = 100


class PaymentRequestError(Exception):
    """Why a payment link could not be raised, in words a guest could read."""


@dataclass(frozen=True)
class PaymentRequest:
    reservation_id: str
    ref: str
    razorpay_order_id: str
    amount_paise: int
    balance_paise: int
    pay_url: str

    def as_dict(self) -> dict:
        return {
            "reservationId": self.reservation_id,
            "ref": self.ref,
            "orderId": self.razorpay_order_id,
            "amountPaise": self.amount_paise,
            "balancePaise": self.balance_paise,
            "payUrl": self.pay_url,
        }


def pay_url(razorpay_order_id: str) -> str:
    """
    Where the guest goes to pay.

    A path on the hotel's own app rather than a Razorpay-hosted page, because
    the page has to name the hotel and the stay — a guest who opens a bare
    checkout on a phone at a bus stand has no way to tell what they are paying
    for, and that is the moment they close the tab.
    """
    from app.config import settings

    base = (settings.pms_app_url or "").rstrip("/")
    return f"{base}/pay/{razorpay_order_id}"


def _read_reservation(property_id: str, reservation_id: str) -> dict | None:
    rows = (
        get_db().table("reservations")
        .select("id,ref,property_id,partner_id,status,total_paise,paid_paise,"
                "guest_name,check_in,check_out")
        .eq("id", reservation_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _insert_order(row: dict) -> dict:
    """
    One insert, with `reservation_id` in it from the start.

    The booking engine writes its order before the reservation exists and
    stamps the id on afterwards. Here the reservation is the reason the order
    exists, so writing it in two steps would leave a window in which the row
    violates its own shape constraint -- and it is the shape constraint that
    stops the webhook settling an order with no folio to post to.
    """
    return get_db().table("payment_orders").insert(row).execute().data[0]


def _open_order(reservation_id: str, amount_paise: int) -> dict | None:
    """
    A link for this stay and this amount that nobody has paid yet.

    Reused rather than replaced. A guest who asks twice — or a model that calls
    the tool twice — should get the same link back, not a second live order for
    the same money: two open orders for one balance is how a stay gets paid for
    twice by a couple who both tapped.
    """
    rows = (
        get_db().table("payment_orders")
        .select("*")
        .eq("reservation_id", reservation_id)
        .eq("purpose", PURPOSE)
        .eq("status", "created")
        .eq("amount_paise", amount_paise)
        .order("created_at", desc=True)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


async def create_request(
    *,
    property_id: str,
    reservation_id: str,
    amount_paise: int | None = None,
) -> PaymentRequest:
    """
    Raise a payment link against a stay's outstanding balance.

    `amount_paise=None` means the whole balance, which is what a hotel means by
    "send them the payment link" almost every time.
    """
    if not razorpay.configured():
        raise PaymentRequestError(
            "Online payments aren't switched on for this hotel yet."
        )

    reservation = await run_db(_read_reservation, property_id, reservation_id)
    if not reservation:
        raise PaymentRequestError("I couldn't find that booking.")

    if reservation["status"] in ("cancelled", "no_show"):
        raise PaymentRequestError(
            "That booking has been cancelled, so there's nothing to pay."
        )

    balance = int(reservation["total_paise"] or 0) - int(reservation["paid_paise"] or 0)
    if balance <= 0:
        raise PaymentRequestError("That booking is already paid in full.")

    wanted = balance if amount_paise is None else int(amount_paise)
    if wanted > balance:
        # Clamped rather than refused. "Send them a link for ten thousand" on a
        # stay with eight thousand outstanding is a hotelier rounding, not an
        # attempt to overcharge, and the safe reading of it is the balance.
        wanted = balance
    if wanted < MIN_PAISE:
        raise PaymentRequestError("That amount is too small to collect online.")

    existing = await run_db(_open_order, reservation_id, wanted)
    if existing:
        return PaymentRequest(
            reservation_id=reservation_id,
            ref=reservation["ref"],
            razorpay_order_id=existing["razorpay_order_id"],
            amount_paise=wanted,
            balance_paise=balance,
            pay_url=pay_url(existing["razorpay_order_id"]),
        )

    order = await razorpay.create_order(
        amount_paise=wanted,
        receipt=f"stay-{reservation['ref']}",
        notes={
            "purpose": PURPOSE,
            "reservationId": reservation_id,
            "ref": reservation["ref"],
            "propertyId": property_id,
        },
    )

    try:
        await run_db(
            _insert_order,
            {
                "razorpay_order_id": order["id"],
                "purpose": PURPOSE,
                "partner_id": reservation["partner_id"],
                "property_id": property_id,
                "amount_paise": wanted,
                "reservation_id": reservation_id,
                "guest_details": {"name": reservation.get("guest_name") or ""},
                "addons": [],
                "attribution": {},
                "status": "created",
            },
        )
    except Exception as exc:  # noqa: BLE001
        # `payment_orders_one_open_stay_link` rejected it: somebody else raised
        # a link for this stay and this amount between our check and our
        # insert. Their link is as good as the one we were about to make, so
        # hand it back rather than failing a guest over a race they cannot see.
        # The Razorpay order we created goes unused, which costs nothing — an
        # order nobody opens is never charged.
        other = await run_db(_open_order, reservation_id, wanted)
        if other is None:
            raise
        logger.info("reusing concurrent payment link for reservation=%s (%s)",
                    reservation_id, type(exc).__name__)
        return PaymentRequest(
            reservation_id=reservation_id,
            ref=reservation["ref"],
            razorpay_order_id=other["razorpay_order_id"],
            amount_paise=wanted,
            balance_paise=balance,
            pay_url=pay_url(other["razorpay_order_id"]),
        )

    logger.info(
        "payment link raised reservation=%s order=%s amount=%s",
        reservation_id, order["id"], wanted,
    )
    return PaymentRequest(
        reservation_id=reservation_id,
        ref=reservation["ref"],
        razorpay_order_id=order["id"],
        amount_paise=wanted,
        balance_paise=balance,
        pay_url=pay_url(order["id"]),
    )


class _SystemActor:
    """
    Who the folio says collected the money.

    A payment that arrives by webhook has no member behind it, and
    `post_folio_entry` wants one. Naming it "Online payment" is what a hotelier
    reading the bill needs; writing a null there is what the audit trail work
    existed to stop.
    """

    member_id = None
    display_name = "Online payment"


async def settle(order: dict) -> dict:
    """
    Post a paid link onto the stay's folio. Called once per order, ever.

    Idempotent on the Razorpay order id, which is what makes it safe from both
    the browser callback and a webhook Razorpay retried for a day.
    """
    reservation_id = order.get("reservation_id")
    if not reservation_id:
        raise RuntimeError(f"stay payment order {order['id']} has no reservation")

    entry = await frontdesk.post_folio_entry(
        order["property_id"],
        reservation_id,
        kind="payment",
        description="Payment received online",
        amount_paise=int(order["amount_paise"]),
        method="razorpay",
        idempotency_key=f"stay-payment:{order['razorpay_order_id']}",
        member=_SystemActor(),
    )
    logger.info(
        "stay payment settled reservation=%s order=%s",
        reservation_id, order["razorpay_order_id"],
    )
    return entry
