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


def _is_terminal(booking: dict) -> bool:
    """
    True when this booking must not be settled by an incoming capture.

    `refunded` always is. `cancelled` normally is — but a booking cancelled
    while it still has no payment against it was never actually paid for, and a
    genuine capture arriving afterwards should still be honoured rather than
    silently pocketed. That happens two ways: the old payment.failed handler
    cancelled orders the traveller then retried successfully (see
    cancel_unpaid_order), and checkout cancels a booking whose Razorpay order
    failed to create, which a stray webhook could still reference.

    Only a *signature-verified* capture reaches here, so honouring it is the
    conservative choice: the alternative is money taken for a booking nobody
    can see.
    """
    status = booking.get("status")
    if status == "cancelled":
        return bool(booking.get("razorpay_payment_id"))
    return status in TERMINAL

BOOKING_COLS = (
    "id, ref, status, item_type, item_id, item_title, travellers, partner_id, "
    "razorpay_payment_id, lead_name, lead_phone, lead_email, travel_date, "
    "amount_paise, payout_paise, coupon_id, departure_id"
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
    if booking.get("status") in SETTLED or _is_terminal(booking):
        # The other path got here first. Not an error.
        return {"status": "already_paid", "ref": ref, "booking_id": booking["id"]}

    # Seats move exactly once, and only for trips that track them. The RPC is a
    # single guarded statement: `set seats_left = seats_left - n where seats_left >= n`.
    #
    # Which counter moves depends on whether the booking named a departure. When
    # it did, that date's inventory is the real one and the trip row's count is
    # a mirror the DB trigger refreshes; when it didn't, the trip row IS the
    # inventory (a trip from before departures existed).
    if booking.get("item_type") == "group_trip":
        claim_fn, claim_args = (
            ("claim_departure_seats", {"p_departure": booking["departure_id"]})
            if booking.get("departure_id")
            else ("claim_trip_seats", {"p_trip": booking["item_id"]})
        )
        try:
            claim = await run_db(
                lambda: get_db()
                .rpc(claim_fn, {**claim_args, "p_seats": booking["travellers"]})
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
            .not_.in_("status", list(SETTLED | {"refunded"}))
            # The real single-winner guard: only a booking that has not already
            # taken a payment can take this one. Keyed on the payment rather
            # than the status so a booking cancelled without ever being paid can
            # still be settled by a genuine capture (see _is_terminal), while a
            # concurrent double-settle still lands on exactly one winner.
            .is_("razorpay_payment_id", "null")
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
            release_fn, release_args = (
                ("release_departure_seats", {"p_departure": booking["departure_id"]})
                if booking.get("departure_id")
                else ("release_trip_seats", {"p_trip": booking["item_id"]})
            )
            try:
                await run_db(
                    lambda: get_db()
                    .rpc(release_fn, {**release_args, "p_seats": booking["travellers"]})
                    .execute()
                )
            except Exception:  # noqa: BLE001
                logger.exception("[bookings] seat release failed for %s", booking["id"])
        return {"status": "already_paid", "ref": ref, "booking_id": booking["id"]}

    # Count the coupon exactly once, on the settle that won. Guarded and atomic,
    # so a capped code can't be over-issued by a concurrent double-settle. A
    # failure here is logged, not fatal — the money already moved.
    if booking.get("coupon_id"):
        try:
            await run_db(
                lambda: get_db()
                .rpc("claim_coupon", {"p_coupon": booking["coupon_id"]})
                .execute()
            )
        except Exception:  # noqa: BLE001
            logger.exception("[bookings] coupon claim failed for %s", booking["id"])

    return {
        "status": "paid",
        "ref": ref,
        "booking_id": booking["id"],
        # Returned so the caller can send receipts without a second round-trip.
        "booking": booking,
    }


async def mark_refunded(order_id: str, payment_id: str, refunded: bool, reason: str) -> None:
    """
    Settle a booking we took money for but cannot honour.

    The refund note is MERGED into `meta`, not written over it. `meta` is the
    audit trail checkout stamped on the row — the nightly rate breakdown, the
    coupon, the seat count at checkout, which departure was sold. Replacing it
    wholesale destroyed exactly the evidence a refund dispute needs, at the one
    moment someone is most likely to ask.
    """
    res = await run_db(
        lambda: get_db()
        .table("bookings")
        .select("meta")
        .eq("razorpay_order_id", order_id)
        .maybe_single()
        .execute()
    )
    meta = ((res.data if res else None) or {}).get("meta") or {}
    if not isinstance(meta, dict):
        meta = {"original_meta": meta}

    await run_db(
        lambda: get_db()
        .table("bookings")
        .update(
            {
                "status": "refunded" if refunded else "cancelled",
                "payout_status": "na",
                "razorpay_payment_id": payment_id,
                "meta": {**meta, "refund_reason": reason, "auto_refunded": refunded},
            }
        )
        .eq("razorpay_order_id", order_id)
        .execute()
    )


async def mark_refunded_by_payment(payment_id: str, refund_paise: int | None = None) -> None:
    """
    Refund arriving from the webhook, keyed on the payment rather than the order.

    A PARTIAL refund must not close the booking. Cancellation tiers routinely
    refund 50% or 75%, and marking the whole booking `refunded` with
    payout_status `na` told the partner their stay was cancelled and wiped the
    payout they were still owed on the part the traveller did not get back.
    Only a refund that covers the captured amount settles the row; anything
    less is recorded and left alone.
    """
    res = await run_db(
        lambda: get_db()
        .table("bookings")
        .select("id, amount_paise, meta")
        .eq("razorpay_payment_id", payment_id)
        .maybe_single()
        .execute()
    )
    booking = res.data if res else None
    if not booking:
        return

    meta = booking.get("meta") if isinstance(booking.get("meta"), dict) else {}
    captured = booking.get("amount_paise") or 0
    # An unknown refund amount is treated as full: that is what Razorpay sends
    # for a plain full refund, and under-refunding the record is the safer error.
    full = refund_paise is None or refund_paise >= captured

    patch: dict = {"meta": {**meta, "refunded_paise": refund_paise, "refunded_in_full": full}}
    if full:
        patch["status"] = "refunded"
        patch["payout_status"] = "na"

    await run_db(
        lambda: get_db().table("bookings").update(patch).eq("id", booking["id"]).execute()
    )


async def cancel_unpaid_order(order_id: str) -> None:
    """
    payment.failed — record the failed attempt WITHOUT closing the booking.

    A Razorpay order survives a failed payment: Checkout lets the traveller
    retry then and there (wrong OTP, bank timeout, insufficient balance) and the
    retry is a new payment against the SAME order. Cancelling here used to make
    that retry unsettleable — `finalise_booking` saw a booking in `cancelled`,
    treated it as terminal, and returned "already_paid" without claiming seats,
    marking it paid or sending a receipt. The traveller was charged, saw a
    success screen, and the partner never learned the booking existed.

    So a failed attempt is now only a note on the row. An order that is never
    paid stays in `created`, which is already inert everywhere it is read.
    """
    await run_db(
        lambda: get_db()
        .table("bookings")
        .update({"last_payment_failed_at": datetime.now(timezone.utc).isoformat()})
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

    # Bring back everything the expiry job paused. Every kind, not just stays:
    # under V3 unified pricing the ₹499 plan gates publishing anything, so a
    # lapse pauses experiences too and paying has to un-pause them.
    try:
        await run_db(
            lambda: get_db()
            .table("partner_listings")
            .update({"status": "published", "published_at": datetime.now(timezone.utc).isoformat()})
            .eq("partner_id", partner_id)
            .eq("status", "expired")
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscription] relist after payment failed")

    # Group trips lapse in two different ways, so paying has to undo both:
    # an unbooked trip was retired outright, and a trip with travellers on it
    # stayed published with its bookings closed.
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        await run_db(
            lambda: get_db()
            .table("group_trips")
            .update({"status": "published"})
            .eq("partner_id", partner_id)
            .eq("status", "expired")
            # Never resurrect a departure that has already left — that trip is
            # over, and expire_departed_trips retired it on its own merits.
            .gte("departure_date", today)
            .execute()
        )
        await run_db(
            lambda: get_db()
            .table("group_trips")
            .update({"bookings_closed": False})
            .eq("partner_id", partner_id)
            .eq("bookings_closed", True)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscription] trip relist after payment failed")

    return True


# ── autopay ──────────────────────────────────────────────────────────────────
#
# With a mandate in place the money arrives on its own and Razorpay tells us
# about it. `subscription.charged` is the event that matters: it is both the
# first payment and every renewal, so one handler covers activation and the
# eleven months after it.

PLAN_KEY = "partner_monthly"

# Mirrors what a Razorpay subscription can be, mapped to what the plan gate
# should do about it. Anything that isn't `active` stops NEW charges but does
# not retroactively take away the period already paid for — stay_plan_expires_at
# is what actually gates the listings.
SUBSCRIPTION_ACTIVE = {"active", "authenticated"}


async def record_subscription_charge(
    subscription_id: str,
    payment_id: str,
    invoice_id: str | None,
    amount_paise: int,
) -> bool:
    """
    A cycle was charged: ledger the payment and push the plan out a month.

    Idempotent on the PAYMENT id, which is the only identifier both callers
    always have. This used to key only on the invoice, and that quietly gave
    away a free month to every subscriber alive:

      * the browser callback (/subscribe PUT) has no invoice and passed
        invoice_id=None, so it wrote a ledger row with a NULL invoice;
      * the `subscription.charged` webhook for that same ₹499 arrived with a
        real invoice id, matched nothing, and ran the whole body again.

    The second insert did fail on the unique payment id — but the extension
    below used to run regardless of whether the insert landed, so one payment
    bought two months, and every webhook replay after that bought another. The
    plan is now extended only by the call that genuinely wrote the ledger row.
    """
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("id")
        .eq("subscription_id", subscription_id)
        .maybe_single()
        .execute()
    )
    partner = res.data if res else None
    if not partner:
        logger.error("[autopay] no partner for subscription %s", subscription_id)
        return False

    if await _charge_already_ledgered(payment_id, invoice_id):
        return True  # a replay, or the other path got here first — not a new cycle

    now = datetime.now(timezone.utc)
    try:
        await run_db(
            lambda: get_db()
            .table("partner_payments")
            .insert(
                {
                    "partner_id": partner["id"],
                    "kind": "stay_subscription",
                    "amount_paise": amount_paise,
                    "status": "paid",
                    "razorpay_payment_id": payment_id,
                    "razorpay_subscription_id": subscription_id,
                    "razorpay_invoice_id": invoice_id,
                    "period_start": now.date().isoformat(),
                    "period_end": (now + relativedelta(months=1)).date().isoformat(),
                }
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        # The unique index on razorpay_payment_id rejected it, so the other path
        # won the race and has already extended the plan. Returning here is the
        # whole point: falling through would hand out the second month.
        logger.warning("[autopay] charge %s already ledgered by another path", payment_id)
        await set_subscription_status(subscription_id, "active")
        return True

    await set_subscription_status(subscription_id, "active")
    return await activate_stay_plan(partner["id"], months=1)


async def _charge_already_ledgered(payment_id: str, invoice_id: str | None) -> bool:
    """
    True when this charge is already in `partner_payments` under either id.

    Checked before the insert so the common replay costs one SELECT instead of
    a failed INSERT; the unique index is still what makes it safe under a real
    race.
    """
    for column, value in (("razorpay_payment_id", payment_id), ("razorpay_invoice_id", invoice_id)):
        if not value:
            continue
        seen = await run_db(
            lambda col=column, val=value: get_db()
            .table("partner_payments")
            .select("id")
            .eq(col, val)
            .maybe_single()
            .execute()
        )
        if seen and seen.data:
            return True
    return False


async def set_subscription_status(subscription_id: str, status: str) -> None:
    """
    Mirror the mandate's state onto the partner.

    Deliberately does NOT touch `stay_plan_expires_at`: a halted mandate means
    the next charge failed, not that the month already paid for is void. The
    expiry job takes the listings down when that month actually runs out.
    """
    try:
        await run_db(
            lambda: get_db()
            .table("partners")
            .update({"subscription_status": status})
            .eq("subscription_id", subscription_id)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("[autopay] status update failed for %s", subscription_id)
