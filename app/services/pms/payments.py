"""
Payment orders — where every path that could take money converges.

    browser  ──► create order ──► Razorpay Checkout ──┐
                                                      ├──► settle()  ──► one
    Razorpay ──────── webhook ────────────────────────┘                 outcome

**A payment is confirmed twice and must happen once.** Razorpay tells us a
payment succeeded through two channels that race: the browser returns with a
signature, and a server-to-server webhook fires. Either can be first, the
webhook can fire more than once, and the browser can never come back at all
because the guest closed the tab on a train. Four orderings, one reservation.

The convergence is `payment_orders.razorpay_order_id`, which is unique, plus a
status transition that only one caller can win:

    update … set status='paid' where razorpay_order_id = ? and status='created'

Postgres decides. The caller that gets a row back does the work; the caller that
gets nothing already lost and reads what the winner produced. There is no
application-level lock, no advisory lock and no "check then insert" — those all
have a window, and this does not.

**What the order carries is what the outcome needs.** The guest's details and
their chosen add-ons are written when the order is *created*, not when the
browser returns, because the webhook has no browser to ask. A tab closed during
payment still produces the right reservation with the right extras.

**Nothing here decides an amount.** `amount_paise` is computed by
`public_booking.checkout_amount_paise` from a quote the server priced, and this
module passes it to Razorpay. A client-supplied amount never reaches it.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


@dataclass
class Settlement:
    """
    What happened when an order was settled.

    `won` is the important field: True means this caller made the transition
    and is responsible for creating the reservation or granting the credits;
    False means somebody else already did and this caller should report their
    result rather than repeat it.
    """

    won: bool
    order: dict
    already: bool = False


def _create(row: dict) -> dict:
    return get_db().table("payment_orders").insert(row).execute().data[0]


async def create(
    *,
    razorpay_order_id: str,
    purpose: str,
    partner_id: str,
    property_id: str | None,
    amount_paise: int,
    quote_id: str | None = None,
    credits: int | None = None,
    billing_account_id: str | None = None,
    guest_details: dict | None = None,
    addons: list[dict] | None = None,
    attribution: dict | None = None,
    conversation_id: str | None = None,
) -> dict:
    """
    Record an order before the browser is handed it.

    Written first, deliberately. The webhook can arrive while the browser is
    still rendering Checkout, and a webhook that finds no order has nothing to
    settle — so the row exists before Razorpay's id is given to anybody.
    """
    return await run_db(_create, {
        "razorpay_order_id": razorpay_order_id,
        "purpose": purpose,
        "partner_id": partner_id,
        "property_id": property_id,
        "billing_account_id": billing_account_id,
        "quote_id": quote_id,
        "credits": credits,
        "amount_paise": amount_paise,
        "guest_details": guest_details or {},
        "addons": addons or [],
        "attribution": attribution or {},
        "conversation_id": conversation_id,
        "status": "created",
    })


def _by_order(razorpay_order_id: str) -> dict | None:
    rows = (
        get_db().table("payment_orders").select("*")
        .eq("razorpay_order_id", razorpay_order_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


async def get(razorpay_order_id: str) -> dict | None:
    return await run_db(_by_order, razorpay_order_id)


def _claim(razorpay_order_id: str, payment_id: str) -> tuple[bool, dict | None]:
    """
    Move one order from 'created' to 'paid', and say whether we were the one.

    The `eq("status", "created")` in this UPDATE is the entire concurrency
    control. Two callers issue the same statement; Postgres applies them in
    some order; the first changes a row and gets it back, the second matches
    nothing and gets an empty list. No window, no lock to forget to take.
    """
    db = get_db()
    updated = (
        db.table("payment_orders")
        .update({
            "status": "paid",
            "razorpay_payment_id": payment_id,
            "paid_at": datetime.now(timezone.utc).isoformat(),
        })
        .eq("razorpay_order_id", razorpay_order_id)
        .eq("status", "created")
        .execute().data or []
    )
    if updated:
        return True, updated[0]
    return False, _by_order(razorpay_order_id)


async def claim(razorpay_order_id: str, payment_id: str) -> Settlement:
    """
    Try to become the caller that settles this order.

    Returns `won=True` at most once per order, however many times it is called
    and from however many processes.
    """
    won, order = await run_db(_claim, razorpay_order_id, payment_id)
    if order is None:
        return Settlement(won=False, order={}, already=False)
    return Settlement(won=won, order=order, already=not won and order["status"] == "paid")


def _finish(order_id: str, patch: dict) -> dict | None:
    rows = (
        get_db().table("payment_orders").update(patch)
        .eq("id", order_id).execute().data or []
    )
    return rows[0] if rows else None


async def record_result(order_id: str, *, reservation_id: str | None = None,
                        ledger_key: str | None = None) -> dict | None:
    """
    Stamp what the order produced.

    A later webhook reads this instead of acting: `reservation_id` already set
    is the answer to "has this been done", and it is the answer that survives a
    process dying between the claim and the reservation insert — see
    `release` for the other half of that.
    """
    patch: dict = {}
    if reservation_id:
        patch["reservation_id"] = reservation_id
    if ledger_key:
        patch["ledger_key"] = ledger_key
    return await run_db(_finish, order_id, patch) if patch else None


async def release(order_id: str, reason: str) -> None:
    """
    Hand a claimed order back after failing to fulfil it.

    Won the claim, then the reservation insert failed — the stock moved, the
    quote expired, the database blinked. Leaving the order 'paid' with no
    reservation would mean a guest charged for nothing and a webhook that
    refuses to retry because the status already moved. So it goes back to
    'created' and the next webhook picks it up.

    Razorpay retries a webhook for 24 hours, which is what makes this a recovery
    rather than a hope.
    """
    def _write():
        get_db().table("payment_orders").update({
            "status": "created",
            "razorpay_payment_id": None,
            "paid_at": None,
            "failure_reason": reason[:300],
        }).eq("id", order_id).eq("status", "paid").execute()

    try:
        await run_db(_write)
    except Exception:  # noqa: BLE001
        logger.exception("could not release payment order %s", order_id)


async def mark(razorpay_order_id: str, status: str, reason: str | None = None) -> None:
    """
    Record a failed or cancelled order.

    Guarded on 'created' so a late failure event cannot undo a payment that
    already succeeded — Razorpay sends `payment.failed` for an attempt the guest
    then retried successfully, and the second attempt is the one that counts.
    """
    def _write():
        get_db().table("payment_orders").update({
            "status": status,
            "failure_reason": (reason or "")[:300] or None,
        }).eq("razorpay_order_id", razorpay_order_id).eq("status", "created").execute()

    try:
        await run_db(_write)
    except Exception:  # noqa: BLE001
        logger.warning("could not mark order %s as %s", razorpay_order_id, status)


def _history(property_id: str, limit: int) -> list[dict]:
    return (
        get_db().table("payment_orders")
        .select("id,razorpay_order_id,purpose,amount_paise,credits,status,"
                "reservation_id,created_at,paid_at,failure_reason")
        .eq("property_id", property_id)
        .order("created_at", desc=True).limit(limit).execute().data or []
    )


async def history(property_id: str, limit: int = 50) -> list[dict]:
    return await run_db(_history, property_id, limit)
