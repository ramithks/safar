"""
Booking finalisation — the single place a booking becomes `paid`.

Ported from the website's lib/marketplace/bookings.ts. Both the browser callback
(/checkout/verify) and the Razorpay webhook route through here, so the two paths
cannot drift. Consequences, unchanged from the original:

  * It must be **idempotent**. Verify and the webhook routinely both fire for
    the same payment, in either order.
  * It must **claim seats atomically**. A read-then-write oversells under
    concurrency, so the decrement is a single guarded UPDATE in Postgres
    (`claim_trip_seats`), and losing that race is a real, handled outcome.

Receipt emails are deliberately NOT sent here. The website owns traveller- and
partner-facing mail (the backend's Resend usage is admin-only), so this returns
the booking on the transition to `paid` and the caller sends exactly once.
"""
import logging
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# A booking in any of these has already been settled by the other path.
SETTLED = {"paid", "confirmed", "completed"}
TERMINAL = {"refunded", "cancelled"}

BOOKING_COLS = (
    "id, ref, status, item_type, item_id, item_title, travellers, partner_id, "
    "razorpay_payment_id, lead_name, lead_phone, lead_email, travel_date, "
    "amount_paise, payout_paise"
)


async def finalise_booking(order_id: str, payment_id: str) -> dict:
    """
    Returns {"status": "paid"|"already_paid"|"sold_out"|"not_found", ...}.

    `paid` is the ONLY status that means "this call caused the transition" —
    the caller uses it to decide whether to send receipts, so a repeat of the
    same payment never produces a second email.
    """
    res = await run_db(
        lambda: get_db()
        .table("bookings")
        .select(BOOKING_COLS)
        .eq("razorpay_order_id", order_id)
        .maybe_single()
        .execute()
    )
    booking = res.data if res else None
    if not booking:
        logger.error("[bookings] no booking for order %s", order_id)
        return {"status": "not_found"}

    ref = booking.get("ref")
    if booking.get("status") in SETTLED or booking.get("status") in TERMINAL:
        # The other path got here first. Not an error.
        return {"status": "already_paid", "ref": ref, "booking_id": booking["id"]}

    # Seats move exactly once, and only for trips that track them. The RPC is a
    # single guarded statement: `set seats_left = seats_left - n where seats_left >= n`.
    if booking.get("item_type") == "group_trip":
        try:
            claim = await run_db(
                lambda: get_db()
                .rpc(
                    "claim_trip_seats",
                    {"p_trip": booking["item_id"], "p_seats": booking["travellers"]},
                )
                .execute()
            )
            remaining = claim.data if claim else None
            if isinstance(remaining, (int, float)) and remaining < 0:
                return {"status": "sold_out", "ref": ref, "booking_id": booking["id"]}
        except Exception:  # noqa: BLE001
            logger.exception("[bookings] claim_trip_seats failed for %s", booking["id"])
            return {"status": "sold_out", "ref": ref, "booking_id": booking["id"]}

    # The guard in the WHERE clause is what makes a concurrent double-settle
    # land on exactly one winner.
    try:
        update = await run_db(
            lambda: get_db()
            .table("bookings")
            .update(
                {
                    "status": "paid",
                    "razorpay_payment_id": payment_id,
                    "payout_status": "held" if booking.get("partner_id") else "na",
                }
            )
            .eq("id", booking["id"])
            .not_.in_("status", list(SETTLED | TERMINAL))
            .execute()
        )
        settled = bool(update.data)
    except Exception:  # noqa: BLE001
        # The unique index on razorpay_payment_id makes a double-write fail here
        # rather than duplicating a booking — which is exactly what we want.
        logger.exception("[bookings] finalise update failed for %s", booking["id"])
        settled = False

    if not settled:
        if booking.get("item_type") == "group_trip":
            # Give the seats back — we claimed them for a booking that never settled.
            try:
                await run_db(
                    lambda: get_db()
                    .rpc(
                        "release_trip_seats",
                        {"p_trip": booking["item_id"], "p_seats": booking["travellers"]},
                    )
                    .execute()
                )
            except Exception:  # noqa: BLE001
                logger.exception("[bookings] seat release failed for %s", booking["id"])
        return {"status": "already_paid", "ref": ref, "booking_id": booking["id"]}

    return {
        "status": "paid",
        "ref": ref,
        "booking_id": booking["id"],
        # Returned so the caller can send receipts without a second round-trip.
        "booking": booking,
    }


async def mark_refunded(order_id: str, payment_id: str, refunded: bool, reason: str) -> None:
    """Settle a booking we took money for but cannot honour."""
    await run_db(
        lambda: get_db()
        .table("bookings")
        .update(
            {
                "status": "refunded" if refunded else "cancelled",
                "payout_status": "na",
                "razorpay_payment_id": payment_id,
                "meta": {"refund_reason": reason, "auto_refunded": refunded},
            }
        )
        .eq("razorpay_order_id", order_id)
        .execute()
    )


async def mark_refunded_by_payment(payment_id: str) -> None:
    """Refund arriving from the webhook, keyed on the payment rather than the order."""
    await run_db(
        lambda: get_db()
        .table("bookings")
        .update({"status": "refunded", "payout_status": "na"})
        .eq("razorpay_payment_id", payment_id)
        .execute()
    )


async def cancel_unpaid_order(order_id: str) -> None:
    """payment.failed — only ever touches a booking still sitting in `created`."""
    await run_db(
        lambda: get_db()
        .table("bookings")
        .update({"status": "cancelled"})
        .eq("razorpay_order_id", order_id)
        .eq("status", "created")
        .execute()
    )


# ── stay subscription ────────────────────────────────────────────────────────

SUBSCRIPTION_SETTLED = {"paid", "refunded"}


async def activate_stay_plan_for_payment(order_id: str, payment_id: str) -> bool:
    """
    Mark a subscription payment settled and switch the partner's plan on.
    Idempotent: verify and the webhook both call it, in either order.

    Returns False when the order isn't a subscription — the webhook uses that
    to decide the event belonged to a booking instead.
    """
    res = await run_db(
        lambda: get_db()
        .table("partner_payments")
        .select("id, partner_id, status")
        .eq("razorpay_order_id", order_id)
        .maybe_single()
        .execute()
    )
    payment = res.data if res else None
    if not payment:
        return False

    if payment.get("status") in SUBSCRIPTION_SETTLED:
        return True  # already handled

    now = datetime.now(timezone.utc)
    period_end = (now + timedelta(days=31)).date()

    try:
        await run_db(
            lambda: get_db()
            .table("partner_payments")
            .update(
                {
                    "status": "paid",
                    "razorpay_payment_id": payment_id,
                    "period_start": now.date().isoformat(),
                    "period_end": period_end.isoformat(),
                }
            )
            .eq("id", payment["id"])
            .neq("status", "paid")
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscription] payment update failed")
        return True  # genuine subscription order; don't fall through to bookings

    await activate_stay_plan(payment["partner_id"], months=1)
    return True


async def activate_stay_plan(partner_id: str, months: int = 1) -> bool:
    """
    Activate (or extend) the ₹499 stay plan and un-pause the partner's stays.

    Extends from max(now, current expiry) so an early renewal never loses days.
    `stay_plan_*` are guarded columns — this is service-role only by design.
    """
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("stay_plan_expires_at")
        .eq("id", partner_id)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None

    now = datetime.now(timezone.utc)
    base = now
    current = (row or {}).get("stay_plan_expires_at")
    if current:
        try:
            parsed = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
            if parsed > now:
                base = parsed
        except ValueError:
            pass

    expires = base + relativedelta(months=months)

    try:
        await run_db(
            lambda: get_db()
            .table("partners")
            .update(
                {
                    "stay_plan_status": "active",
                    "stay_plan_expires_at": expires.isoformat(),
                }
            )
            .eq("id", partner_id)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscription] activate_stay_plan failed")
        return False

    # Bring back anything the expiry job paused.
    try:
        await run_db(
            lambda: get_db()
            .table("partner_listings")
            .update({"status": "published", "published_at": datetime.now(timezone.utc).isoformat()})
            .eq("partner_id", partner_id)
            .eq("kind", "hotel")
            .eq("status", "expired")
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscription] relist after payment failed")

    return True
