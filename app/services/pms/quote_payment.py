"""
Paying for a booking that does not exist yet.

    AI employee ──► get_quote ──► create_booking_payment_link ──► /pay/{order}
                                                                     │
                                       reservation ◄── _fulfil ◄─────┤
                                                   ◄── webhook ◄─────┘

**Why this exists at all.** The product's whole promise is that the AI employee
answers, quotes, and takes the money. It could do the first two and not the
third, and the reason was structural rather than a missing button:

  * `create_booking` makes a reservation and carries
    `always_requires_approval=True`, which no configuration can switch off. An
    AI-created *unpaid* booking is a room taken off sale on a model's say-so, and
    that gate is right.
  * `create_payment_link` collects money for a reservation that **already
    exists**.

So a returning guest could be asked to pay and a new one could not: there was no
reservation to attach a payment to, and the only way to make one needed a
manager. The AI could sell right up to the word "yes" and then hand over.

**What closes it, without weakening anything.** The booking engine on the
hotel's own website has always done this unattended — quote, Razorpay order,
webhook, reservation — and nobody approves those. Money arriving *is* the
confirmation. This module is that same path, raised from a conversation instead
of a checkout page: the order is created, the guest pays, and
`_fulfil_booking` — the identical function the browser callback and the webhook
already share — creates the reservation.

The safety property is strictly better than what the approval gate protects
against. No room leaves inventory on the model's word; it leaves when a payment
settles. A guest who never pays costs the hotel a held quote that expires on its
own.

**Nothing here can name a price.** The amount is computed from the quote row,
which was priced server-side by `booking.create_quote`. There is no argument on
any function in this module that money could be passed through.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services import razorpay
from app.services.pms import payments as payments_service
from app.services.pms import public_booking, stay_payment

logger = logging.getLogger(__name__)

PURPOSE = "booking"


class QuotePaymentError(Exception):
    """Why a link could not be raised, in words a guest could read."""


@dataclass(frozen=True)
class QuotePaymentLink:
    quote_id: str
    # Our own `payment_orders.id`. See the note on stay_payment.PaymentRequest:
    # `agent_actions.result_id` is a uuid column and Razorpay's id is not one.
    order_id: str
    razorpay_order_id: str
    amount_paise: int
    total_paise: int
    balance_paise: int
    pay_url: str

    def as_dict(self) -> dict:
        return {
            "quoteId": self.quote_id,
            "orderId": self.razorpay_order_id,
            "amountPaise": self.amount_paise,
            "totalPaise": self.total_paise,
            "balancePaise": self.balance_paise,
            "payUrl": self.pay_url,
        }


def _quote(quote_id: str, property_id: str) -> dict | None:
    rows = (
        get_db()
        .table("quotes")
        .select("*")
        .eq("id", quote_id)
        .eq("property_id", property_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _open_order_for_quote(quote_id: str) -> dict | None:
    """
    An order already raised against this quote and still payable.

    Reused rather than replaced. A guest who asks twice — "send it again", or
    the model calling the tool a second time on a retry — must get the same
    link, not a second Razorpay order against the same rooms. Two live orders
    on one quote is two ways to pay for one stay, and the loser of that race is
    a guest who has been charged for a booking that does not exist.
    """
    rows = (
        get_db()
        .table("payment_orders")
        .select("*")
        .eq("quote_id", quote_id)
        .eq("purpose", PURPOSE)
        .eq("status", "created")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


async def create_link(
    *,
    property_id: str,
    partner_id: str,
    quote_id: str,
    guest_name: str,
    guest_phone: str | None,
    guest_email: str | None,
    source: str | None = None,
) -> QuotePaymentLink:
    """
    Turn a live quote into something a guest can pay.

    Every refusal is a `QuotePaymentError` whose message is already fit for a
    guest to read, because the caller is an AI agent that will put it in front
    of one.
    """
    quote = await run_db(_quote, quote_id, property_id)
    if not quote:
        # Includes a quote belonging to another hotel. Deliberately the same
        # message as one that never existed — a stranger's quote id is not a
        # thing to confirm the existence of.
        raise QuotePaymentError("I couldn't find that price. Let me get you a fresh one.")

    if quote["status"] != "active":
        raise QuotePaymentError(
            "That price has already been used. Let me get you a fresh one."
        )

    expires = datetime.fromisoformat(str(quote["expires_at"]).replace("Z", "+00:00"))
    if expires < datetime.now(timezone.utc):
        raise QuotePaymentError("That price has expired. Let me check those dates again.")

    total = int(quote["total_paise"])
    token = int(quote["token_paise"])
    amount = public_booking.checkout_amount_paise(total, token, 0)

    if amount < stay_payment.MIN_PAISE:
        # A property on pay-at-hotel with no deposit. Saying so is right; a
        # one-rupee order to make the flow look complete is not.
        raise QuotePaymentError(
            "This stay doesn't need anything paid online — we'll settle it at the hotel."
        )

    if not razorpay.configured():
        raise QuotePaymentError(
            "Online payment isn't available right now. Someone from the hotel will "
            "follow up with you."
        )

    existing = await run_db(_open_order_for_quote, quote_id)
    if existing and int(existing["amount_paise"]) == amount:
        return QuotePaymentLink(
            quote_id=quote_id,
            order_id=existing["id"],
            razorpay_order_id=existing["razorpay_order_id"],
            amount_paise=amount,
            total_paise=total,
            balance_paise=max(0, total - token),
            pay_url=stay_payment.pay_url(existing["razorpay_order_id"]),
        )

    rzp = await razorpay.create_order(
        amount_paise=amount,
        receipt=f"stay-{quote_id[:8]}",
        notes={"quote": quote_id, "property": property_id},
    )

    # Written before the id reaches anybody, for the same reason the booking
    # engine writes it first: the webhook can fire while the guest is still
    # opening the link, and a webhook that finds no order has nothing to settle.
    written = await payments_service.create(
        razorpay_order_id=rzp["id"],
        purpose=PURPOSE,
        partner_id=partner_id,
        property_id=property_id,
        quote_id=quote_id,
        amount_paise=amount,
        guest_details={
            "fullName": guest_name,
            "phone": guest_phone,
            "email": guest_email,
        },
        addons=[],
        attribution={"source": source} if source else {},
    )

    logger.info("quote payment link raised: quote=%s order=%s", quote_id, rzp["id"])
    return QuotePaymentLink(
        quote_id=quote_id,
        order_id=written["id"],
        razorpay_order_id=rzp["id"],
        amount_paise=amount,
        total_paise=total,
        balance_paise=max(0, total - token),
        pay_url=stay_payment.pay_url(rzp["id"]),
    )
