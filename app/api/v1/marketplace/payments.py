"""
The money path — checkout, verification, subscriptions, the Razorpay webhook.

Moved here from the website so the web tier holds no service-role key. See the
website's docs/MARKETPLACE_BACKEND_MIGRATION.md.

Authorization model, per endpoint:

  /checkout            optional JWT. A traveller need not have an account; when
                       a token IS present it stamps `user_id`. Nothing about
                       the AMOUNT comes from the caller — it is read from the
                       DB row and multiplied by the traveller count here.
  /checkout/verify     no JWT. The Razorpay HMAC over `order_id|payment_id` is
                       the authentication; a valid signature is the only thing
                       that can settle a booking.
  /subscribe (+PUT)    JWT required, and the partner is resolved from it. The
                       order→partner link is stored server-side, so a valid
                       signature for someone else's order cannot upgrade this
                       partner's plan.
  /webhook             no JWT. HMAC over the RAW body. Razorpay is not a
                       browser and sends no Origin.
"""
# NOTE: deliberately no `from __future__ import annotations` — see the note in
# app/api/v1/admin/endpoint.py.
import logging
import secrets
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.api.v1.marketplace.payment_schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PublicBooking,
    QuoteRequest,
    QuoteResponse,
    SubscribeResponse,
    VerifyRequest,
    VerifyResponse,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.auth import optional_user, require_user
from app.services import marketplace_pricing as pricing
from app.services import razorpay
from app.services.booking_emails import send_booking_emails
from app.services.marketplace_bookings import (
    activate_stay_plan_for_payment,
    cancel_unpaid_order,
    finalise_booking,
    mark_refunded,
    mark_refunded_by_payment,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketplace", tags=["marketplace"])

# Mirrors KIND_RULES in the website's lib/marketplace/types.ts. The partner is
# shown this price on the pricing page and charged it here, from one constant.
STAY_PLAN_MONTHLY_RUPEES = 499
STAY_PLAN_MONTHLY_PAISE = STAY_PLAN_MONTHLY_RUPEES * 100

REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O/0, I/1


def booking_ref() -> str:
    return "TB-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(6))


# split_commission lives in one place now — the pricing module — so the website,
# this endpoint and the payout ledger cannot disagree. Re-exported for the tests
# and callers that imported it from here historically.
split_commission = pricing.split_commission


def resolve_tier_price(content: dict | None, tier_title: str | None) -> int | None:
    """
    A tier title only sets the price if it genuinely exists on the row. An
    unknown tier falls through to `price_from` rather than being honoured.
    """
    if not tier_title or not content:
        return None
    tiers = content.get("pricing_tiers") or content.get("pricing")
    if not isinstance(tiers, list):
        return None
    for raw in tiers:
        if not isinstance(raw, dict):
            continue
        title = (raw.get("title") or "").strip().lower()
        if title == tier_title.strip().lower():
            amount = raw.get("amount")
            return amount if isinstance(amount, (int, float)) and amount > 0 else None
    return None


# ── price resolution ─────────────────────────────────────────────────────────
#
# One place resolves a listing into the facts the pricing pipeline needs, and
# one place runs the pipeline. Both /checkout and /quote call them, so the
# summary a traveller sees and the amount we charge are computed by identical
# code — the whole point of routing everything through here.


class PricingError(HTTPException):
    """A 4xx the traveller should see verbatim (sold out, no price, bad dates)."""


async def _resolve_listing(item_type: str, item_id: str) -> dict:
    """
    Fetch a published, bookable listing and normalise it to a common shape.

    Raises PricingError for anything that makes it un-bookable so the caller
    doesn't repeat the checks. Never returns a price — that's the pipeline's job.
    """
    if item_type == "group_trip":
        res = await run_db(
            lambda: get_db()
            .table("group_trips")
            .select(
                "id, slug, title, price_from, partner_id, commission_pct, seats_left, "
                "content, booking_mode, departure_date"
            )
            .eq("id", item_id)
            .eq("status", "published")
            .maybe_single()
            .execute()
        )
        row = res.data if res else None
        if not row:
            raise PricingError(404, "That trip is no longer available.")
        if row.get("booking_mode") != "instant":
            raise PricingError(409, "This trip is booked over WhatsApp. Use the enquiry form.")
        departure = row.get("departure_date")
        if departure:
            try:
                if date.fromisoformat(str(departure)[:10]) < datetime.now(timezone.utc).date():
                    raise PricingError(409, "That departure has already left.")
            except ValueError:
                pass
        row["kind"] = "group_trip"
        return row

    kind = "hotel" if item_type == "hotel" else "experience"
    res = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .select(
            "id, slug, title, price_from, partner_id, commission_pct, content, "
            "booking_mode, kind"
        )
        .eq("id", item_id)
        .eq("status", "published")
        .eq("kind", kind)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    if not row:
        noun = "stay" if kind == "hotel" else "experience"
        raise PricingError(404, f"That {noun} is no longer available.")
    if row.get("booking_mode") != "instant":
        noun = "stay" if kind == "hotel" else "experience"
        raise PricingError(409, f"This {noun} is booked over WhatsApp. Use the enquiry form.")
    return row


async def _season_rates(listing_id: str) -> list[dict]:
    res = await run_db(
        lambda: get_db()
        .table("listing_season_rates")
        .select("starts_on, ends_on, price")
        .eq("listing_id", listing_id)
        .execute()
    )
    return res.data or [] if res else []


async def _resolve_coupon(
    partner_id: str | None, item_type: str, item_id: str, code: str | None
) -> dict | None:
    """
    The coupon row for a code, scoped to this partner and applicable to this
    item, or None. Returns the row unvalidated — the pricing module decides
    whether it applies, so checkout and quote reject it identically.
    """
    if not code or not partner_id:
        return None
    res = await run_db(
        lambda: get_db()
        .table("listing_coupons")
        .select("*")
        .eq("partner_id", partner_id)
        .ilike("code", code.strip())
        .eq("active", True)
        .maybe_single()
        .execute()
    )
    coupon = res.data if res else None
    if not coupon:
        return None
    # A coupon scoped to a specific listing/trip only applies to that item; a
    # null scope is partner-wide.
    scope = coupon.get("listing_id") if item_type != "group_trip" else coupon.get("trip_id")
    if scope and scope != item_id:
        return None
    return coupon


def _validate_stay(body, content: dict) -> int:
    """
    Stay-only checks: dates present and ordered, minimum stay, maximum
    occupancy. Returns the night count. Raises PricingError on any failure.
    """
    if not body.check_in or not body.check_out:
        raise PricingError(422, "Please choose your check-in and check-out dates.")
    nights = pricing.nights_between(body.check_in, body.check_out)
    if nights < 1:
        raise PricingError(422, "Check-out must be after check-in.")
    if date.fromisoformat(body.check_in) < datetime.now(timezone.utc).date():
        raise PricingError(409, "Check-in can't be in the past.")

    min_nights = content.get("min_nights")
    if isinstance(min_nights, int) and nights < min_nights:
        raise PricingError(409, f"This stay has a {min_nights}-night minimum.")

    per_room = content.get("max_occupancy_per_room")
    rooms = getattr(body, "rooms", 1) or 1
    guests = (getattr(body, "adults", 1) or 1) + (getattr(body, "children", 0) or 0)
    if isinstance(per_room, int) and per_room > 0 and guests > per_room * rooms:
        raise PricingError(
            409, f"That's {guests} guests in {rooms} room(s); this stay allows "
            f"{per_room} per room. Add more rooms."
        )
    return nights


def _price_booking(body, listing: dict, coupon: dict | None) -> tuple[pricing.Quote, dict]:
    """
    Run the pipeline for any kind and return (quote, meta).

    The commission rate is read from the listing row, falling back to the
    single-source-of-truth table — never a per-endpoint constant. `meta` is the
    audit trail we stamp on the booking so a dispute can be reconstructed.
    """
    content = listing.get("content") or {}
    commission_pct = float(
        listing.get("commission_pct")
        if listing.get("commission_pct") is not None
        else pricing.commission_pct_for(listing["kind"])
    )

    if listing["kind"] == "hotel":
        nights = _validate_stay(body, content)
        base = listing.get("price_from")
        if not base or base < 1:
            raise PricingError(409, "This stay has no price set. Please use the enquiry form.")
        rates = getattr(body, "_season_rates", [])
        q = pricing.quote_stay(
            base_price_rupees=float(base),
            season_rates=rates,
            check_in=body.check_in,
            check_out=body.check_out,
            rooms=body.rooms,
            commission_pct=commission_pct,
            coupon=coupon,
        )
        meta = {
            "nights": nights,
            "rooms": body.rooms,
            "base_price": base,
            "coupon": coupon.get("code") if coupon else None,
        }
        return q, meta

    # trip / experience — per-person, optional tier price
    unit_price = resolve_tier_price(content, getattr(body, "tier_title", None)) or listing.get(
        "price_from"
    )
    if not unit_price or unit_price < 1:
        raise PricingError(409, "This listing has no price set. Please use the enquiry form.")
    q = pricing.quote(
        unit_prices_paise=[pricing.to_paise(float(unit_price))],
        quantity=body.travellers,
        commission_pct=commission_pct,
        coupon=coupon,
    )
    meta = {
        "tier": getattr(body, "tier_title", None),
        "unit_price": unit_price,
        "coupon": coupon.get("code") if coupon else None,
    }
    return q, meta


# ── quote (price preview, charges nothing) ────────────────────────────────────


@router.post("/quote", response_model=QuoteResponse)
@limiter.limit("120/hour")
async def quote_price(request: Request, body: QuoteRequest) -> QuoteResponse:
    """
    The booking summary a traveller sees before paying — same pipeline as
    checkout, no Razorpay order, no row written. Keeping this on the server
    means the summary and the charge are computed by one code path.
    """
    try:
        listing = await _resolve_listing(body.item_type, body.item_id)
        if body.item_type == "hotel":
            body._season_rates = await _season_rates(listing["id"])
        coupon = await _resolve_coupon(
            listing.get("partner_id"), body.item_type, body.item_id, body.coupon_code
        )
        q, meta = _price_booking(body, listing, coupon)
    except PricingError as e:
        return QuoteResponse(ok=False, error=e.detail)

    return QuoteResponse(
        ok=True,
        gross_paise=q.gross_paise,
        discount_paise=q.discount_paise,
        amount_paise=q.amount_paise,
        commission_paise=q.commission_paise,
        nights=meta.get("nights"),
        coupon_applied=q.discount_paise > 0,
        coupon_message=(
            pricing.COUPON_REJECTIONS.get(q.coupon_rejection) if q.coupon_rejection else None
        ),
    )


# ── checkout ─────────────────────────────────────────────────────────────────


@router.post("/checkout", response_model=CheckoutResponse)
@limiter.limit("30/hour")
async def checkout(
    request: Request,
    body: CheckoutRequest,
    user=Depends(optional_user),
) -> CheckoutResponse:
    """
    Open a booking.

    The price is computed by the shared pricing pipeline from the DB row, the
    season table and the coupon row — nothing about the amount comes from the
    client. A tampered body can change *what* you book, never *what it costs*.

    Creates the booking in `created` plus a matching Razorpay order. It only
    becomes `paid` in /checkout/verify or the webhook, after the signature
    checks out.
    """
    if not razorpay.configured():
        raise HTTPException(
            503, "Online booking isn't available yet. Please use the enquiry form."
        )

    listing = await _resolve_listing(body.item_type, body.item_id)
    partner_id = listing.get("partner_id")
    title, slug = listing["title"], listing["slug"]

    # Trips track seats; check before we take money.
    seats_left = listing.get("seats_left") if body.item_type == "group_trip" else None
    if seats_left is not None and seats_left < body.travellers:
        raise HTTPException(409, f"Only {max(0, seats_left)} seats left.")

    if body.item_type == "hotel":
        body._season_rates = await _season_rates(listing["id"])
    coupon = await _resolve_coupon(partner_id, body.item_type, body.item_id, body.coupon_code)
    q, meta = _price_booking(body, listing, coupon)
    meta["seats_at_checkout"] = seats_left

    ref = booking_ref()
    booking_row = {
        "ref": ref,
        # From the verified token only. An anonymous traveller is fine; a
        # caller-supplied user_id is not accepted at all.
        "user_id": user.id if user else None,
        "partner_id": partner_id,
        "item_type": body.item_type,
        "item_id": body.item_id,
        "item_title": title,
        "item_slug": slug,
        "travel_date": body.travel_date or body.check_in,
        "travellers": body.travellers,
        "lead_name": body.name,
        "lead_phone": body.phone,
        "lead_email": body.email,
        "notes": body.notes,
        "source": "online",
        "gross_paise": q.gross_paise,
        "discount_paise": q.discount_paise,
        "coupon_code": coupon.get("code") if coupon else None,
        "coupon_id": coupon.get("id") if coupon else None,
        "amount_paise": q.amount_paise,
        "commission_paise": q.commission_paise,
        "payout_paise": q.payout_paise,
        "status": "created",
        "payout_status": "held" if partner_id else "na",
        "meta": meta,
    }
    if body.item_type == "hotel":
        booking_row["check_in"] = body.check_in
        booking_row["check_out"] = body.check_out
        booking_row["nights"] = meta.get("nights")

    insert = await run_db(
        lambda: get_db().table("bookings").insert(booking_row).execute()
    )
    if not insert.data:
        logger.error("[checkout] booking insert failed for %s", body.item_id)
        raise HTTPException(500, "Could not start your booking.")
    booking_id = insert.data[0]["id"]

    try:
        order = await razorpay.create_order(
            amount_paise=q.amount_paise,
            receipt=ref,
            notes={"booking_ref": ref, "item_type": body.item_type, "item_slug": slug},
        )
    except Exception:  # noqa: BLE001
        # Leave nothing half-open: the booking never becomes paid and nothing
        # was charged.
        logger.exception("[checkout] order creation failed")
        await run_db(
            lambda: get_db()
            .table("bookings")
            .update({"status": "cancelled"})
            .eq("id", booking_id)
            .execute()
        )
        raise HTTPException(502, "Could not start the payment. Please try again.") from None

    await run_db(
        lambda: get_db()
        .table("bookings")
        .update({"razorpay_order_id": order["id"]})
        .eq("id", booking_id)
        .execute()
    )

    return CheckoutResponse(
        ok=True,
        ref=ref,
        order_id=order["id"],
        amount=q.amount_paise,
        currency="INR",
        key_id=razorpay.public_key_id(),
        name=title,
        prefill={"name": body.name, "contact": body.phone, "email": body.email or ""},
    )


@router.post("/checkout/verify", response_model=VerifyResponse)
@limiter.limit("60/hour")
async def verify_checkout(request: Request, body: VerifyRequest) -> VerifyResponse:
    """
    Called by the browser the moment Checkout succeeds.

    The signature is the only thing that makes this trustworthy — without it,
    anyone could POST a made-up payment id and mark a booking paid. The webhook
    covers the same ground independently, so this is written to be safely
    repeatable rather than exactly-once.
    """
    if not razorpay.verify_payment_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        logger.error("[verify] signature mismatch for order %s", body.razorpay_order_id)
        raise HTTPException(400, "Payment could not be verified.")

    result = await finalise_booking(body.razorpay_order_id, body.razorpay_payment_id)

    if result["status"] == "not_found":
        raise HTTPException(404, "Booking not found.")

    if result["status"] == "sold_out":
        # We took the money and can't honour it. Refund immediately rather than
        # leaving the traveller to chase us for it.
        refunded = await razorpay.refund_payment(
            body.razorpay_payment_id,
            notes={"reason": "seats_unavailable", "booking_ref": result.get("ref") or ""},
        )
        await mark_refunded(
            body.razorpay_order_id, body.razorpay_payment_id, refunded, "seats_unavailable"
        )
        return VerifyResponse(
            ok=False,
            code="sold_out",
            error=(
                "Those seats went while you were paying. We've refunded you in full — "
                "it'll be back in 5–7 days."
                if refunded
                else "Those seats went while you were paying. Our team will refund you "
                "within 24 hours."
            ),
        )

    # Receipts go out only on the transition to paid, so the webhook arriving
    # after this callback cannot send a second copy.
    if result["status"] == "paid" and result.get("booking"):
        await send_booking_emails(result["booking"])

    return VerifyResponse(
        ok=True,
        ref=result.get("ref"),
        already=result["status"] == "already_paid",
    )


@router.get("/bookings/{ref}", response_model=PublicBooking)
@limiter.limit("120/hour")
async def get_booking_by_ref(request: Request, ref: str) -> PublicBooking:
    """
    Public-safe booking view for the confirmation page.

    Unauthenticated on purpose: a traveller who paid without an account has only
    the reference, and that is the credential. The response model is the control
    — it exposes no payment ids, commission or payout, so a guessed ref leaks
    nothing beyond what the payer already knows. Refs are 6 chars from a
    32-symbol alphabet, and the rate limit is what makes enumeration impractical.
    """
    res = await run_db(
        lambda: get_db()
        .table("bookings")
        .select(
            "ref, item_type, item_title, item_slug, travel_date, travellers, "
            "check_in, check_out, nights, gross_paise, discount_paise, "
            "amount_paise, status, lead_name, created_at, partner_id"
        )
        .eq("ref", ref)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    if not row:
        raise HTTPException(404, "Booking not found.")
    return PublicBooking(**row)


# ── stay subscription ────────────────────────────────────────────────────────


async def _require_partner(user) -> dict:
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("id, brand_name, email, whatsapp, status")
        .eq("owner_id", user.id)
        .maybe_single()
        .execute()
    )
    partner = res.data if res else None
    if not partner:
        raise HTTPException(403, "Create your partner profile first.")
    if partner.get("status") != "active":
        raise HTTPException(403, "Your account is suspended.")
    return partner


@router.post("/subscribe", response_model=SubscribeResponse)
@limiter.limit("30/hour")
async def start_subscription(request: Request, user=Depends(require_user)) -> SubscribeResponse:
    """Start the ₹499/month stay plan. The amount is a server-side constant."""
    if not razorpay.configured():
        raise HTTPException(503, "Payments aren't switched on yet. Please try again shortly.")

    partner = await _require_partner(user)

    insert = await run_db(
        lambda: get_db()
        .table("partner_payments")
        .insert(
            {
                "partner_id": partner["id"],
                "kind": "stay_subscription",
                "amount_paise": STAY_PLAN_MONTHLY_PAISE,
                "status": "created",
            }
        )
        .execute()
    )
    if not insert.data:
        raise HTTPException(500, "Could not start checkout.")
    payment_id = insert.data[0]["id"]

    try:
        order = await razorpay.create_order(
            amount_paise=STAY_PLAN_MONTHLY_PAISE,
            receipt=f"plan-{str(payment_id)[:26]}",
            notes={"partner_id": partner["id"], "kind": "stay_subscription"},
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscribe] order failed")
        await run_db(
            lambda: get_db()
            .table("partner_payments")
            .update({"status": "failed"})
            .eq("id", payment_id)
            .execute()
        )
        raise HTTPException(502, "Could not start the payment.") from None

    await run_db(
        lambda: get_db()
        .table("partner_payments")
        .update({"razorpay_order_id": order["id"]})
        .eq("id", payment_id)
        .execute()
    )

    return SubscribeResponse(
        ok=True,
        order_id=order["id"],
        amount=STAY_PLAN_MONTHLY_PAISE,
        currency="INR",
        key_id=razorpay.public_key_id(),
        name=f"TravBuddy Stay Plan — ₹{STAY_PLAN_MONTHLY_RUPEES}/month",
        prefill={
            "name": partner.get("brand_name") or "",
            "contact": partner.get("whatsapp") or "",
            "email": partner.get("email") or "",
        },
    )


@router.put("/subscribe", response_model=VerifyResponse)
@limiter.limit("60/hour")
async def verify_subscription(
    request: Request,
    body: VerifyRequest,
    user=Depends(require_user),
) -> VerifyResponse:
    """Verify the Checkout callback and switch the plan on."""
    partner = await _require_partner(user)

    if not razorpay.verify_payment_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        logger.error("[subscribe] signature mismatch for order %s", body.razorpay_order_id)
        raise HTTPException(400, "Payment could not be verified.")

    # The order → partner link is stored server-side, so a valid signature for
    # someone else's order still can't upgrade this partner's plan.
    owned = await run_db(
        lambda: get_db()
        .table("partner_payments")
        .select("id")
        .eq("razorpay_order_id", body.razorpay_order_id)
        .eq("partner_id", partner["id"])
        .maybe_single()
        .execute()
    )
    if not (owned.data if owned else None):
        raise HTTPException(404, "Payment not found.")

    done = await activate_stay_plan_for_payment(
        body.razorpay_order_id, body.razorpay_payment_id
    )
    if not done:
        raise HTTPException(404, "Payment not found.")
    return VerifyResponse(ok=True)


# ── webhook ──────────────────────────────────────────────────────────────────


@router.post("/webhook")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=None),
) -> dict:
    """
    Razorpay's server-to-server callback.

    The browser callback in /checkout/verify is the fast path; this is the one
    that's actually reliable. It fires even when the traveller closes the tab
    mid-redirect, and it turns "payment captured but our DB write failed" into a
    self-healing situation rather than a support ticket.

    Deliberately NOT origin-guarded — Razorpay is not a browser and sends no
    Origin. The HMAC over the raw body is the whole authentication story, so it
    is checked before anything is parsed.

    Always answers 200 once the signature is good: a non-2xx makes Razorpay
    retry, and retrying a request that already succeeded is pure noise.
    """
    if not razorpay.webhook_configured():
        raise HTTPException(503, "Webhook not configured.")

    # Must be the RAW body — re-serialising the parsed JSON changes the bytes
    # and the HMAC will never match.
    raw = await request.body()
    if not razorpay.verify_webhook_signature(raw, x_razorpay_signature):
        logger.error("[webhook] bad signature")
        raise HTTPException(400, "Bad signature.")

    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return {"ok": True, "ignored": "unparseable"}

    name = event.get("event") or ""
    payload = event.get("payload") or {}
    payment = (payload.get("payment") or {}).get("entity") or {}

    try:
        if name in ("payment.captured", "order.paid"):
            order_id = payment.get("order_id") or (
                (payload.get("order") or {}).get("entity") or {}
            ).get("id")
            payment_id = payment.get("id")
            if not order_id or not payment_id:
                return {"ok": True, "ignored": "no ids"}

            # An order is either a booking or a subscription. Try both; each is
            # a no-op when the order isn't theirs.
            result = await finalise_booking(order_id, payment_id)
            if result["status"] == "not_found":
                await activate_stay_plan_for_payment(order_id, payment_id)
            elif result["status"] == "paid" and result.get("booking"):
                # This is the path that settles when the traveller closed the
                # tab, so it owns the receipt in that case.
                await send_booking_emails(result["booking"])
            return {"ok": True, "handled": name}

        if name in ("refund.processed", "refund.created"):
            payment_id = (
                (payload.get("refund") or {}).get("entity") or {}
            ).get("payment_id") or payment.get("id")
            if payment_id:
                await mark_refunded_by_payment(payment_id)
            return {"ok": True, "handled": name}

        if name == "payment.failed":
            order_id = payment.get("order_id")
            if order_id:
                await cancel_unpaid_order(order_id)
            return {"ok": True, "handled": name}
    except Exception:  # noqa: BLE001
        # Signature was valid, so the event is genuine — log it and still ack,
        # or Razorpay will replay it every few minutes forever.
        logger.exception("[webhook] handler threw for %s", name)

    return {"ok": True, "ignored": name}
