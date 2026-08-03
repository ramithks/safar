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
import re
import secrets
from datetime import date, datetime, timezone

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.api.v1.marketplace.payment_schemas import (
    CheckoutRequest,
    CheckoutResponse,
    PlanCouponRequest,
    PlanCouponResponse,
    PublicBooking,
    QuoteRequest,
    QuoteResponse,
    SubscribeRequest,
    SubscribeResponse,
    SubscriptionStatus,
    SubscriptionVerifyRequest,
    VerifyRequest,
    VerifyResponse,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.auth import optional_user, require_user
from app.services import marketplace_availability as availability
from app.services import marketplace_pricing as pricing
from app.services import plan_coupons
from app.services import razorpay
from app.services.booking_emails import send_booking_emails
from app.services.marketplace_bookings import (
    PLAN_KEY,
    activate_stay_plan,
    activate_stay_plan_for_payment,
    cancel_unpaid_order,
    finalise_booking,
    mark_refunded,
    mark_refunded_by_payment,
    record_subscription_charge,
    set_subscription_status,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketplace", tags=["marketplace"])

# Mirrors KIND_RULES in the website's lib/marketplace/types.ts. The partner is
# shown this price on the pricing page and charged it here, from one constant.
#
# Founding-partner pricing (2026-08-03): ₹499 buys a YEAR, not a month.
STAY_PLAN_YEARLY_RUPEES = 499
STAY_PLAN_YEARLY_PAISE = STAY_PLAN_YEARLY_RUPEES * 100

# Cycles a mandate is authorised for. Razorpay demands a finite number, and its
# ceiling for a yearly plan is 100 — ten years is plenty and still shows the
# partner a bounded authorisation when they approve it.
PLAN_TOTAL_CYCLES = 10

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
                "content, booking_mode, departure_date, deposit_type, deposit_value, "
                "bookings_closed"
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
        # The trip is still published — travellers already on it keep their page
        # — but it sells nothing further. Set by expire_stay_plans() when the
        # organiser's plan lapses on a trip that already has bookings.
        if row.get("bookings_closed"):
            raise PricingError(409, "Bookings for this trip are closed.")
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
            "booking_mode, kind, deposit_type, deposit_value"
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


DEPARTURE_COLS = "id, departure_date, return_date, label, seats_total, seats_left, price_from"


async def _resolve_departure(trip: dict, departure_id: str | None) -> dict | None:
    """
    Which batch of a group trip is being booked, or None for a trip that has no
    departure rows at all (the pre-departures shape, where the trip row IS the
    departure).

    A trip with exactly one open date resolves to it without the client saying
    so — that is the old single-batch flow, and breaking it would break every
    link already in the wild. Two or more and the choice must be explicit: the
    seats come off one specific date, and guessing would sell the wrong one.
    """
    res = await run_db(
        lambda: get_db()
        .table("trip_departures")
        .select(DEPARTURE_COLS)
        .eq("trip_id", trip["id"])
        .eq("status", "open")
        .gte("departure_date", datetime.now(timezone.utc).date().isoformat())
        .order("departure_date")
        .execute()
    )
    departures = (res.data or []) if res else []
    if not departures:
        return None

    if not departure_id:
        if len(departures) == 1:
            return departures[0]
        raise PricingError(409, "Choose a departure date.")

    for d in departures:
        if str(d["id"]) == str(departure_id):
            return d
    raise PricingError(409, "That departure is no longer available. Please pick another date.")


async def _apply_departure(listing: dict, departure_id: str | None) -> dict | None:
    """
    Resolve the departure and let it override the trip's base price for that
    date. A sharing tier still wins over both — same precedence tiers already
    have, just with one more rung under them.
    """
    departure = await _resolve_departure(listing, departure_id)
    if departure and departure.get("price_from"):
        listing["price_from"] = departure["price_from"]
    return departure


async def _season_rates(listing_id: str) -> list[dict]:
    res = await run_db(
        lambda: get_db()
        .table("listing_season_rates")
        .select("starts_on, ends_on, price")
        .eq("listing_id", listing_id)
        .execute()
    )
    return res.data or [] if res else []


async def _resolve_room_type(listing_id: str, room_type_id: str | None) -> str | None:
    """
    Bind a requested room type to THIS listing, or fall back to its only one.

    The id arrives in the request body, so it cannot be trusted: a room type
    belonging to another property would otherwise be written onto the booking
    and decrement that partner's inventory when the payment settles. Anything
    that does not belong to this listing is dropped rather than rejected —
    booking without a room type simply claims nothing, which is the same
    behaviour a listing that has not set up rooms already gets.

    A single-room-type listing needs no choice from the traveller, so it is
    resolved implicitly. That is what makes the whole feature work for the
    homestays that have exactly one room and never touch the picker.
    """
    types = await availability.room_types(listing_id)
    if not types:
        return None
    if room_type_id:
        for t in types:
            if t["id"] == room_type_id:
                return t["id"]
        logger.warning(
            "[checkout] room type %s does not belong to listing %s",
            room_type_id,
            listing_id,
        )
        return None
    return types[0]["id"] if len(types) == 1 else None


def _last10(phone: str | None) -> str:
    """Digits only, last ten — "+91 98765 43210" and "9876543210" are one person."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-10:]


# Coupons are written as ^[A-Z0-9]{3,20}$ and stored upper-cased (the website's
# couponSchema in lib/marketplace/schemas.ts enforces it, and the DB has a
# unique index on (partner_id, upper(code))). Redemption normalises to the same
# shape before looking anything up.
_CODE_RE = re.compile(r"^[A-Z0-9]{3,20}$")


def normalise_coupon_code(code: str | None) -> str | None:
    """
    A submitted code as it can safely be matched, or None if it isn't one.

    This exists because the lookup used to be a LIKE: `%` and `_` are wildcards
    to Postgres, so `HIMALAYA__` matched HIMALAYA10 and applied a discount to a
    traveller who never knew the code, and a bare `%` matched every coupon the
    partner had (which then blew up `maybe_single` with a 500). Anything that
    isn't a real code is rejected here rather than being handed to the matcher.
    """
    if not code:
        return None
    candidate = code.strip().upper()
    return candidate if _CODE_RE.match(candidate) else None


async def _resolve_coupon(
    partner_id: str | None,
    item_type: str,
    item_id: str,
    code: str | None,
    phone: str | None = None,
) -> dict | None:
    """
    The coupon row for a code, scoped to this partner and applicable to this
    item, or None. Returns the row unvalidated — the pricing module decides
    whether it applies, so checkout and quote reject it identically.

    A code with `target_phone` set belongs to one person. That check lives here,
    server-side, against the phone on the booking: a targeted code posted by
    anyone else simply doesn't resolve, and the traveller is told the code
    doesn't apply rather than who it was for.
    """
    normalised = normalise_coupon_code(code)
    if not normalised or not partner_id:
        return None
    # Exact match, never LIKE — see normalise_coupon_code. Codes are stored
    # upper-cased, so equality on the normalised form is both correct and the
    # shape the (partner_id, upper(code)) unique index is built for.
    res = await run_db(
        lambda: get_db()
        .table("listing_coupons")
        .select("*")
        .eq("partner_id", partner_id)
        .eq("code", normalised)
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

    target = coupon.get("target_phone")
    if target and target != _last10(phone):
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
    # The deposit policy is read from the DB row like every other money input —
    # a client cannot ask to pay less up front than the partner allows.
    deposit = {
        "type": listing.get("deposit_type") or "none",
        "value": listing.get("deposit_value"),
    }
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
            deposit=deposit,
        )
        meta = {
            "nights": nights,
            "rooms": body.rooms,
            "base_price": base,
            "coupon": coupon.get("code") if coupon else None,
            "deposit": deposit if deposit["type"] != "none" else None,
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
        deposit=deposit,
    )
    meta = {
        "tier": getattr(body, "tier_title", None),
        "unit_price": unit_price,
        "coupon": coupon.get("code") if coupon else None,
        "deposit": deposit if deposit["type"] != "none" else None,
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
        if body.item_type == "group_trip":
            await _apply_departure(listing, body.departure_id)
        if body.item_type == "hotel":
            body._season_rates = await _season_rates(listing["id"])
        coupon = await _resolve_coupon(
            listing.get("partner_id"),
            body.item_type,
            body.item_id,
            body.coupon_code,
            getattr(body, "phone", None),
        )
        q, meta = _price_booking(body, listing, coupon)
    except PricingError as e:
        return QuoteResponse(ok=False, error=e.detail)

    # Advisory only, and read after pricing so a failure here costs the
    # traveller a greyed-out date rather than the whole quote. The authoritative
    # guard is claim_stay_nights at finalise time.
    stock: dict = {}
    if body.item_type == "hotel":
        stock = await availability.check_stay(
            listing["id"], body.check_in, body.check_out, body.rooms, body.room_type_id
        )

    return QuoteResponse(
        ok=True,
        sold_out_nights=stock.get("sold_out_nights") or [],
        units_left=stock.get("units_left"),
        min_nights=stock.get("min_nights"),
        gross_paise=q.gross_paise,
        discount_paise=q.discount_paise,
        total_paise=q.total_paise,
        amount_paise=q.amount_paise,
        balance_paise=q.balance_paise,
        commission_paise=q.commission_paise,
        nights=meta.get("nights"),
        coupon_applied=q.discount_paise > 0,
        # A code that resolved but broke a rule reports that rule. A code that
        # never resolved (typo, wrong property, targeted at another number)
        # reports the generic refusal — without this branch the traveller typed
        # a code, watched the total not move, and was told nothing at all.
        coupon_message=(
            pricing.COUPON_REJECTIONS.get(q.coupon_rejection)
            if q.coupon_rejection
            else pricing.COUPON_REJECTIONS["unknown"]
            if body.coupon_code and q.discount_paise == 0
            else None
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

    # Trips track seats; check before we take money. Once a trip has departures
    # the inventory lives on the chosen date, not on the trip row — the trip's
    # own count is only a mirror of the next one.
    departure = (
        await _apply_departure(listing, body.departure_id)
        if body.item_type == "group_trip"
        else None
    )
    seats_left = None
    if body.item_type == "group_trip":
        seats_left = departure["seats_left"] if departure else listing.get("seats_left")
    if seats_left is not None and seats_left < body.travellers:
        raise HTTPException(409, f"Only {max(0, seats_left)} seats left.")

    # Stays check stock before we take money, for the same reason trips check
    # seats. It cannot be authoritative — another traveller can take the last
    # room between here and the payment landing, which finalise_booking handles
    # by refunding — but refusing an order we can already see is doomed beats
    # charging for it and refunding a minute later.
    room_type_id = None
    if body.item_type == "hotel":
        room_type_id = await _resolve_room_type(listing["id"], body.room_type_id)
        stock = await availability.check_stay(
            listing["id"], body.check_in, body.check_out, body.rooms, room_type_id
        )
        if stock.get("min_nights"):
            raise HTTPException(
                409, f"This property needs a minimum stay of {stock['min_nights']} nights."
            )
        if not stock["ok"]:
            nights = stock.get("sold_out_nights") or []
            shown = ", ".join(nights[:3]) + ("…" if len(nights) > 3 else "")
            raise HTTPException(409, f"Not available on {shown}. Please pick other dates.")

    if body.item_type == "hotel":
        body._season_rates = await _season_rates(listing["id"])
    coupon = await _resolve_coupon(
        partner_id, body.item_type, body.item_id, body.coupon_code, body.phone
    )
    q, meta = _price_booking(body, listing, coupon)
    meta["seats_at_checkout"] = seats_left
    if departure:
        meta["departure"] = {
            "id": departure["id"],
            "date": departure["departure_date"],
            "label": departure.get("label"),
        }

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
        # The departure's own date beats anything the client sent: the seat came
        # off that batch, so the booking must say so.
        "travel_date": (
            departure["departure_date"] if departure else (body.travel_date or body.check_in)
        ),
        "departure_id": departure["id"] if departure else None,
        "travellers": body.travellers,
        "lead_name": body.name,
        "lead_phone": body.phone,
        "lead_email": body.email,
        "notes": body.notes,
        "source": "online",
        "gross_paise": q.gross_paise,
        "discount_paise": q.discount_paise,
        "total_paise": q.total_paise,
        "balance_paise": q.balance_paise,
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
        booking_row["rooms"] = body.rooms
        # Resolved against the listing above, never taken from the body as-is:
        # a room type id belonging to someone else's property would otherwise
        # decrement their inventory.
        booking_row["room_type_id"] = room_type_id

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
        # Warning, not error: this endpoint is unauthenticated, so a rejected
        # signature is the guard doing its job, not a fault. At error level any
        # curl with a made-up signature becomes a Sentry issue. A *missing*
        # secret is logged as an error inside razorpay.verify_payment_signature.
        logger.warning("[verify] signature mismatch for order %s", body.razorpay_order_id)
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


def _mask_name(name: str | None) -> str | None:
    """
    "Rishi Arora" → "Rishi A." — enough for the payer to recognise their own
    booking, not enough to be worth harvesting.

    Single-word names are returned whole: there is no surname to drop, and
    truncating a first name helps nobody.
    """
    parts = (name or "").strip().split()
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


@router.get("/bookings/{ref}", response_model=PublicBooking)
@limiter.limit("120/hour")
async def get_booking_by_ref(request: Request, ref: str) -> PublicBooking:
    """
    Public-safe booking view for the confirmation page.

    Unauthenticated on purpose: a traveller who paid without an account has only
    the reference, and that is the credential. So the honest question is not
    "who can call this" but "what does a *guessed* ref hand over" — refs are 6
    chars from a 32-symbol alphabet, and at scale someone will eventually hit a
    live one.

    Two things narrow that. The response model carries no payment ids, no
    commission, no payout, no partner id and no contact details; and the name is
    masked to "Rishi A." on the way out, so a hit yields a trip, a date and an
    amount rather than an identifiable person. The rate limit is the other half,
    and it only became real once the limiter stopped bucketing on an unverified
    JWT `sub` — see app/core/limiter.py.
    """
    res = await run_db(
        lambda: get_db()
        .table("bookings")
        .select(
            "ref, item_type, item_title, item_slug, travel_date, travellers, "
            "total_paise, balance_paise, "
            "check_in, check_out, nights, gross_paise, discount_paise, "
            "amount_paise, status, lead_name, created_at"
        )
        .eq("ref", ref)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    if not row:
        raise HTTPException(404, "Booking not found.")
    row["lead_name"] = _mask_name(row.get("lead_name"))
    return PublicBooking(**row)


# ── stay subscription ────────────────────────────────────────────────────────


async def _require_partner(user) -> dict:
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select(
            "id, brand_name, email, whatsapp, status, subscription_id, "
            "subscription_status, stay_plan_status, stay_plan_expires_at"
        )
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


@router.post("/subscribe/coupon", response_model=PlanCouponResponse)
@limiter.limit("30/hour")
async def check_plan_coupon(
    request: Request,
    body: PlanCouponRequest,
    user=Depends(require_user),
) -> PlanCouponResponse:
    """
    Validate a plan coupon before Checkout opens, so the partner sees what
    they're getting rather than discovering it on a bank statement.

    Answers only with what the code is worth. Rate-limited and deliberately
    vague on failure — this is the endpoint someone would point a script at to
    discover live campaign codes, and a uniform rejection makes a spent code
    indistinguishable from one that never existed.
    """
    partner = await _require_partner(user)

    coupon, reason = await plan_coupons.resolve(body.code, partner["id"])
    if not coupon:
        return PlanCouponResponse(ok=False, error=reason or plan_coupons.GENERIC_REJECTION)

    return PlanCouponResponse(
        ok=True,
        code=coupon["code"],
        kind=coupon["kind"],
        description=plan_coupons.describe(coupon),
        free_months=plan_coupons.free_months(coupon),
    )


@router.post("/subscribe", response_model=SubscribeResponse)
@limiter.limit("30/hour")
async def start_subscription(
    request: Request,
    body: SubscribeRequest | None = None,
    user=Depends(require_user),
) -> SubscribeResponse:
    """
    Start the ₹499/year founding-partner plan as an AUTOPAY MANDATE.

    This used to mint a one-off order every cycle, which meant renewal depended
    on a partner remembering to come back and pay a listing fee. They don't, so
    listings lapsed and it looked like our fault. Razorpay Subscriptions moves
    the charge server-side: the partner approves a mandate once (UPI Autopay,
    card or e-NACH) and the ₹499 is collected each year without them.

    Reuses the partner's existing mandate when there is a live one, so a double
    click can't leave someone paying twice.

    A plan coupon changes HOW the mandate is created, never whether the money
    is real: `free_months` defers the first charge with `start_at`, percent and
    flat attach a Razorpay offer. The coupon is only PARKED here — it is
    redeemed when the mandate authenticates, so abandoning Checkout costs the
    campaign nothing.
    """
    if not razorpay.configured():
        raise HTTPException(503, "Payments aren't switched on yet. Please try again shortly.")

    partner = await _require_partner(user)

    existing = partner.get("subscription_id")
    if existing and partner.get("subscription_status") in ("active", "authenticated", "pending"):
        raise HTTPException(
            409, "You already have an active plan. Manage it from the Plan page."
        )

    plan_id = await _ensure_plan()

    # ── coupon ───────────────────────────────────────────────────────────
    coupon = None
    submitted = (body.coupon_code if body else None) or None
    if submitted:
        coupon, reason = await plan_coupons.resolve(submitted, partner["id"])
        if not coupon:
            # A bad code must not quietly become a full-price mandate — the
            # partner clicked subscribe expecting a discount.
            raise HTTPException(400, reason or plan_coupons.GENERIC_REJECTION)

    months_free = plan_coupons.free_months(coupon) if coupon else 0
    start_at = None
    first_charge_at = None
    if months_free:
        # Razorpay bills from here; we grant the free months ourselves once the
        # mandate authenticates, so the two periods meet exactly.
        billing_from = datetime.now(timezone.utc) + relativedelta(months=months_free)
        start_at = int(billing_from.timestamp())
        first_charge_at = billing_from.isoformat()

    try:
        subscription = await razorpay.create_subscription(
            plan_id=plan_id,
            total_count=PLAN_TOTAL_CYCLES,
            notes={
                "partner_id": partner["id"],
                "brand": partner.get("brand_name") or "",
                **({"coupon": coupon["code"]} if coupon else {}),
            },
            start_at=start_at,
            offer_id=(coupon or {}).get("razorpay_offer_id"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("[subscribe] subscription create failed")
        raise HTTPException(502, "Could not start the subscription.") from None

    if coupon:
        # Parked, not redeemed. `pending_plan_coupon_id` is a guarded column, so
        # this is the only path that can set it.
        await plan_coupons.park_pending(partner["id"], coupon["id"])

    # Stored BEFORE the browser is handed the id: the webhook can arrive before
    # the callback does, and it resolves the partner by subscription id.
    await run_db(
        lambda: get_db()
        .table("partners")
        .update(
            {
                "subscription_id": subscription["id"],
                "subscription_status": subscription.get("status") or "created",
            }
        )
        .eq("id", partner["id"])
        .execute()
    )

    blurb = f"TravBuddy plan — ₹{STAY_PLAN_YEARLY_RUPEES}/year, cancel anytime"
    if coupon:
        blurb = f"{plan_coupons.describe(coupon)}, then {blurb}"

    return SubscribeResponse(
        ok=True,
        subscription_id=subscription["id"],
        amount=STAY_PLAN_YEARLY_PAISE,
        currency="INR",
        key_id=razorpay.public_key_id(),
        name="TravBuddy",
        description=blurb,
        prefill={
            "name": partner.get("brand_name") or "",
            "contact": partner.get("whatsapp") or "",
            "email": partner.get("email") or "",
        },
        coupon_code=(coupon or {}).get("code"),
        coupon_description=plan_coupons.describe(coupon) if coupon else None,
        first_charge_at=first_charge_at,
    )


async def _ensure_plan() -> str:
    """
    The Razorpay plan id, created on first use and remembered.

    Kept in `platform_plans` rather than an env var so deploying this needs no
    manual ops step — and so we never create a second plan for the same product
    and split the reporting.
    """
    res = await run_db(
        lambda: get_db()
        .table("platform_plans")
        .select("razorpay_plan_id")
        .eq("key", PLAN_KEY)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    if row and row.get("razorpay_plan_id"):
        return row["razorpay_plan_id"]

    plan = await razorpay.create_plan(
        amount_paise=STAY_PLAN_YEARLY_PAISE,
        name=f"TravBuddy plan (₹{STAY_PLAN_YEARLY_RUPEES}/year)",
        period="yearly",
    )
    await run_db(
        lambda: get_db()
        .table("platform_plans")
        .upsert(
            {
                "key": PLAN_KEY,
                "razorpay_plan_id": plan["id"],
                "amount_paise": STAY_PLAN_YEARLY_PAISE,
                "period": "yearly",
            },
            on_conflict="key",
        )
        .execute()
    )
    logger.info("[subscribe] created Razorpay plan %s", plan["id"])
    return plan["id"]


@router.put("/subscribe", response_model=VerifyResponse)
@limiter.limit("60/hour")
async def verify_subscription(
    request: Request,
    body: SubscriptionVerifyRequest,
    user=Depends(require_user),
) -> VerifyResponse:
    """
    Verify the mandate callback and switch the plan on.

    The signature here is over `payment_id|subscription_id` — the REVERSE of the
    order flow. The mandate is also re-checked against the partner we stored it
    against, so a valid signature for someone else's subscription can't upgrade
    this account.
    """
    partner = await _require_partner(user)

    if not razorpay.verify_subscription_signature(
        body.razorpay_subscription_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        logger.warning(
            "[subscribe] signature mismatch for subscription %s", body.razorpay_subscription_id
        )
        raise HTTPException(400, "Payment could not be verified.")

    if partner.get("subscription_id") != body.razorpay_subscription_id:
        raise HTTPException(404, "Subscription not found.")

    # The webhook is the reliable path and may already have run. This is the
    # fast one, so the partner sees an active plan before the tab settles.
    await settle_authenticated_mandate(
        partner["id"], body.razorpay_subscription_id, body.razorpay_payment_id
    )
    return VerifyResponse(ok=True)


async def _partner_for_subscription(subscription_id):
    """The partner a mandate belongs to, or None."""
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("id")
        .eq("subscription_id", subscription_id)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    return row["id"] if row else None


async def grant_pending_plan_coupon(partner_id, subscription_id):
    """
    Redeem a parked free-months coupon now that the mandate is real, and give
    the partner the plan time. Returns the months granted, 0 when there was no
    free-months coupon to apply.

    Safe to call from every path that learns a mandate authenticated, as often
    as they like: `redeem_plan_coupon` hands out exactly one redemption per
    (coupon, partner), so only one caller ever gets a non-zero answer here.

    There is no ₹499 to record on this path — `start_at` pushed the first
    invoice past the free period, so `subscription.charged` genuinely will not
    fire for months. The ₹0 ledger row exists so the payments page can still
    explain where the free time came from, and it is keyed on the subscription
    rather than a payment id because the authentication event doesn't always
    carry one.
    """
    months = await plan_coupons.apply_pending(partner_id, subscription_id)
    if not months:
        return 0

    # Grant first, ledger second. A failed ledger insert is a cosmetic gap in
    # the payments page; a lost grant is a partner who redeemed a code and got
    # nothing for it.
    await set_subscription_status(subscription_id, "active")
    await activate_stay_plan(partner_id, months=months)

    now = datetime.now(timezone.utc)
    try:
        await run_db(
            lambda: get_db()
            .table("partner_payments")
            .insert(
                {
                    "partner_id": partner_id,
                    "kind": "stay_subscription",
                    "amount_paise": 0,
                    "status": "paid",
                    # Deterministic, so the unique index makes a second insert
                    # a no-op no matter which path writes it.
                    "razorpay_payment_id": f"coupon_{subscription_id}",
                    "razorpay_subscription_id": subscription_id,
                    "period_start": now.date().isoformat(),
                    "period_end": (now + relativedelta(months=months)).date().isoformat(),
                }
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning("[plan-coupon] could not ledger ₹0 row for %s", subscription_id)

    return months


async def settle_authenticated_mandate(partner_id, subscription_id, payment_id):
    """
    The browser callback's version: apply a free-months coupon if there is one,
    otherwise record the ₹499 that was just charged.

    The `granted_free_period` check is what keeps the two apart when the
    webhook won the race. Without it this would see "no coupon to apply", take
    the charge branch, and ledger a ₹499 nobody paid — the double-month bug of
    2026-07-25 wearing a new hat.
    """
    if await grant_pending_plan_coupon(partner_id, subscription_id):
        return
    if await plan_coupons.granted_free_period(subscription_id):
        return

    await record_subscription_charge(
        subscription_id, payment_id, invoice_id=None, amount_paise=STAY_PLAN_YEARLY_PAISE
    )


@router.post("/subscribe/cancel", response_model=SubscriptionStatus)
@limiter.limit("30/hour")
async def cancel_plan(request: Request, user=Depends(require_user)) -> SubscriptionStatus:
    """
    Stop the mandate at the end of the current cycle.

    Not immediately: the partner has paid for the year they're in, and taking
    their listings down the moment they cancel would be stealing days they
    bought. `stay_plan_expires_at` is left exactly as it is — the expiry job
    retires the listings when the paid period genuinely runs out.
    """
    partner = await _require_partner(user)
    subscription_id = partner.get("subscription_id")
    if not subscription_id:
        raise HTTPException(404, "You don't have an active plan.")

    result = await razorpay.cancel_subscription(subscription_id, at_cycle_end=True)
    if result is None:
        raise HTTPException(502, "Could not cancel the plan. Please try again.")

    await run_db(
        lambda: get_db()
        .table("partners")
        .update(
            {
                "subscription_status": result.get("status") or "cancelled",
                "subscription_ends_at": partner.get("stay_plan_expires_at"),
            }
        )
        .eq("id", partner["id"])
        .execute()
    )

    return SubscriptionStatus(
        ok=True,
        status=result.get("status") or "cancelled",
        plan_status=partner.get("stay_plan_status") or "none",
        expires_at=partner.get("stay_plan_expires_at"),
    )


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
        # Anyone can POST here; an unsigned body is expected background noise.
        logger.warning("[webhook] bad signature")
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

        # ── autopay ──────────────────────────────────────────────────────
        # `subscription.charged` is both the first payment and every renewal,
        # so one handler covers activation and the years after it. This is the
        # path that keeps a plan alive without anyone logging in.
        if name == "subscription.charged":
            sub = ((payload.get("subscription") or {}).get("entity") or {})
            invoice = ((payload.get("invoice") or {}).get("entity") or {})
            subscription_id = sub.get("id") or payment.get("subscription_id")
            payment_id = payment.get("id")
            if not subscription_id or not payment_id:
                return {"ok": True, "ignored": "no subscription ids"}
            await record_subscription_charge(
                subscription_id,
                payment_id,
                invoice_id=invoice.get("id"),
                amount_paise=int(payment.get("amount") or STAY_PLAN_YEARLY_PAISE),
            )
            return {"ok": True, "handled": name}

        if name in (
            "subscription.activated",
            "subscription.authenticated",
            "subscription.pending",
            "subscription.halted",
            "subscription.cancelled",
            "subscription.completed",
            "subscription.paused",
            "subscription.resumed",
        ):
            sub = ((payload.get("subscription") or {}).get("entity") or {})
            subscription_id = sub.get("id")
            if subscription_id:
                # Trust the entity's own status over the event name — Razorpay
                # sends both, and the entity is what the dashboard shows.
                await set_subscription_status(
                    subscription_id, sub.get("status") or name.split(".", 1)[1]
                )
                # A free-months mandate defers its first charge, so
                # `subscription.charged` won't arrive for months and this is
                # the only server-side event that says "the mandate is real".
                # Without it, a partner who closed the tab mid-redirect would
                # have an authenticated mandate and no free period.
                #
                # Only the coupon is applied here, never a charge: on a normal
                # mandate the authentication payment IS the first ₹499, and
                # `subscription.charged` records it under the same payment id.
                # Recording anything here would buy a second month.
                if name in ("subscription.authenticated", "subscription.activated"):
                    owner = await _partner_for_subscription(subscription_id)
                    if owner:
                        await grant_pending_plan_coupon(owner, subscription_id)
            return {"ok": True, "handled": name}

        if name in ("refund.processed", "refund.created"):
            refund = (payload.get("refund") or {}).get("entity") or {}
            payment_id = refund.get("payment_id") or payment.get("id")
            if payment_id:
                # The amount decides whether this closes the booking — a partial
                # refund must leave the partner's remaining payout intact.
                amount = refund.get("amount")
                await mark_refunded_by_payment(
                    payment_id, int(amount) if isinstance(amount, (int, float)) else None
                )
            return {"ok": True, "handled": name}

        if name == "payment.failed":
            order_id = payment.get("order_id")
            if order_id:
                await cancel_unpaid_order(order_id)
            return {"ok": True, "handled": name}
    except Exception:  # noqa: BLE001
        # Ack-on-error was the wrong trade. The events that reach here are
        # signature-verified and money-bearing, and the usual reason a handler
        # throws is transient — a DB blip, a pool timeout. Swallowing that
        # returned 200, Razorpay never retried, and the booking stayed unpaid
        # forever with the money captured: no seat claimed, no receipt, nothing
        # in the partner's dashboard, and no error anywhere anyone would look.
        #
        # A retry is safe precisely because every handler below finalise_booking
        # and record_subscription_charge is idempotent, so 500 here and let
        # Razorpay redeliver. Razorpay backs off and gives up after ~24h, so a
        # genuinely poisonous event cannot loop forever.
        logger.exception("[webhook] handler threw for %s — asking Razorpay to retry", name)
        raise HTTPException(500, "Handler failed; please retry.") from None

    return {"ok": True, "ignored": name}
