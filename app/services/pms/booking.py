"""
Quotes, holds and reservations — the write path for selling a room.

The sequence is the whole design:

    quote  ──► hold_inventory()   stock is claimed BEFORE the guest is told a price
           ──► quotes row         immutable, expiring, with the per-night trail
    book   ──► convert_hold()     units_held → units_booked, atomically
           ──► reservations row
           ──► folio token entry

Why the hold comes first: between "₹11,400, shall I hold it?" and the guest
paying, that room has to be unsellable, or the promise is a guess. And why the
quote is immutable: a guest disputing an invoice six weeks later is asking what
they were told, and `nightly_paise` is the only honest answer.

Compensation rather than transactions. Supabase's REST interface gives no
cross-statement transaction, so every failure path after a stock move explicitly
undoes it. A room held by a request that then failed is a room nobody can sell
and nobody knows why — worse than the error the caller was going to see anyway.
"""
import logging
import random
import string
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status

from app.db.client import get_db, run_db
from app.services.pms.availability import nightly_for_plan
from app.services.pms.pricing import (
    NightlyRate,
    PricedStay,
    nights_between,
    price_stay,
    resolve_property_limits,
)

logger = logging.getLogger(__name__)

# Matches the frontend's hold countdown and the quote TTL. A quote outliving its
# hold would promise a room that has already been released.
HOLD_TTL_SECONDS = 15 * 60

# Ambiguous characters removed: a ref is read aloud over a phone and typed into
# a web check-in form by someone who has just got off a flight.
_REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_ref() -> str:
    return "TB-" + "".join(random.choices(_REF_ALPHABET, k=6))


@dataclass
class QuoteResult:
    id: str
    hold_id: str | None
    priced: PricedStay
    expires_at: datetime
    room_type_id: str
    rate_plan_id: str
    check_in: date
    check_out: date
    adults: int
    children: int


def _load_context(property_id: str, rate_plan_id: str):
    db = get_db()
    prop = (
        db.table("properties")
        .select("id,partner_id,settings,tax_inclusive,status")
        .eq("id", property_id)
        .limit(1)
        .execute()
    )
    plan = (
        db.table("rate_plans")
        .select("id,room_type_id,property_id,audience,is_active")
        .eq("id", rate_plan_id)
        .limit(1)
        .execute()
    )
    return (prop.data[0] if prop.data else None), (plan.data[0] if plan.data else None)


async def create_quote(
    *,
    property_id: str,
    rate_plan_id: str,
    check_in: date,
    check_out: date,
    units: int,
    adults: int,
    children: int,
    requested_discount_pct: int = 0,
    discount_reason: str | None = None,
    issued_by: str = "employee",
    issued_by_member: str | None = None,
    conversation_id: str | None = None,
) -> QuoteResult:
    """
    Price a stay, hold the stock, and record the quote.

    The discount is a *request*. It is clamped against the property's floor here,
    server-side, which is what makes it safe for an AI agent to negotiate: the
    worst outcome is the floor, and the guest is never shown a number the
    property did not authorise.
    """
    nights = nights_between(check_in, check_out)
    if nights <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Check-out must be after check-in")
    if nights > 90:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A single stay cannot exceed 90 nights")
    if units < 1:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "At least one room is required")

    prop, plan = await run_db(_load_context, property_id, rate_plan_id)
    if not prop or not plan or plan["property_id"] != property_id or not plan["is_active"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rate plan not found")
    if prop["status"] != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, "This property is not taking bookings")

    # Prices come from the calendar, never from the caller. This is the line
    # that stops a model-generated number reaching a guest.
    nightly = await nightly_for_plan(property_id, rate_plan_id, check_in, check_out)
    if nightly is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "These dates are closed on this rate plan")

    max_discount, token_pct = resolve_property_limits(prop.get("settings"))
    priced = price_stay(
        [NightlyRate(date=d, price_paise=p) for d, p in nightly],
        units=units,
        requested_discount_pct=requested_discount_pct,
        max_discount_pct=max_discount,
        token_pct=token_pct,
        tax_inclusive=bool(prop.get("tax_inclusive")),
    )

    if priced.clamped_from_pct is not None:
        logger.info(
            "quote discount clamped property=%s asked=%s applied=%s issued_by=%s",
            property_id, priced.clamped_from_pct, priced.discount_pct_applied, issued_by,
        )

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=HOLD_TTL_SECONDS)

    def _write():
        db = get_db()
        hold_id = db.rpc(
            "hold_inventory",
            {
                "p_room_type": plan["room_type_id"],
                "p_check_in": check_in.isoformat(),
                "p_check_out": check_out.isoformat(),
                "p_units": units,
                "p_ttl_seconds": HOLD_TTL_SECONDS,
                "p_rate_plan": rate_plan_id,
                "p_source": "ai" if issued_by == "ai" else "employee",
                "p_conversation": conversation_id,
            },
        ).execute().data

        if not hold_id:
            return None, None

        try:
            row = (
                db.table("quotes")
                .insert(
                    {
                        "property_id": property_id,
                        "room_type_id": plan["room_type_id"],
                        "rate_plan_id": rate_plan_id,
                        "partner_id": prop["partner_id"],
                        "hold_id": hold_id,
                        "check_in": check_in.isoformat(),
                        "check_out": check_out.isoformat(),
                        "units": units,
                        "adults": adults,
                        "children": children,
                        "nightly_paise": [
                            {"date": d.isoformat(), "price_paise": p} for d, p in nightly
                        ],
                        "gross_paise": priced.gross_paise,
                        "discount_paise": priced.discount_paise,
                        "discount_reason": discount_reason if priced.discount_paise else None,
                        "tax_paise": priced.tax_paise,
                        "total_paise": priced.total_paise,
                        "token_paise": priced.token_paise,
                        "balance_paise": priced.balance_paise,
                        "issued_by": issued_by,
                        "issued_by_member": issued_by_member,
                        "conversation_id": conversation_id,
                        "expires_at": expires_at.isoformat(),
                    }
                )
                .execute()
            )
        except Exception:
            # The stock is already held. Failing here without releasing it would
            # take the room off sale for 15 minutes for no reason.
            db.rpc("release_hold", {"p_hold": hold_id}).execute()
            raise

        return row.data[0], hold_id

    quote_row, hold_id = await run_db(_write)
    if quote_row is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Those dates are no longer available in the number of rooms requested",
        )

    return QuoteResult(
        id=quote_row["id"],
        hold_id=hold_id,
        priced=priced,
        expires_at=expires_at,
        room_type_id=plan["room_type_id"],
        rate_plan_id=rate_plan_id,
        check_in=check_in,
        check_out=check_out,
        adults=adults,
        children=children,
    )


def _payment_status(paid: int, total: int) -> str:
    if paid <= 0:
        return "unpaid"
    if paid >= total:
        return "paid"
    # A token is a deposit against a confirmed booking; a part payment is
    # someone paying down a balance. The distinction drives what the desk
    # chases, so it is worth keeping rather than collapsing to "partial".
    return "token_paid" if paid <= total * 0.35 else "partially_paid"


async def create_reservation(
    *,
    property_id: str,
    quote_id: str,
    guest_name: str,
    guest_phone: str | None,
    guest_email: str | None,
    source: str,
    token_paise: int,
    payment_method: str | None,
    notes: str | None,
    created_by: str,
    member_id: str | None,
    member_name: str | None,
) -> dict:
    """
    Turn a quote into a reservation.

    Consuming the quote is what stops a double-submit selling the same held room
    twice: the second call finds it already converted and is refused.
    """

    def _write():
        db = get_db()

        quote = (
            db.table("quotes")
            .select("*")
            .eq("id", quote_id)
            .eq("property_id", property_id)
            .limit(1)
            .execute()
        )
        if not quote.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")
        q = quote.data[0]

        if q["status"] != "active":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This quote has already been used. Request a new price.",
            )
        if datetime.fromisoformat(q["expires_at"].replace("Z", "+00:00")) < datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_409_CONFLICT, "This quote has expired. Request a new price.")

        check_in = date.fromisoformat(q["check_in"])
        check_out = date.fromisoformat(q["check_out"])

        # Convert the hold first. If it fails the stock was never ours and no
        # reservation should exist.
        if q.get("hold_id"):
            converted = db.rpc("convert_hold", {"p_hold": q["hold_id"], "p_reservation": None}).execute().data
            if not converted:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "The hold on these rooms expired. Request a new price.",
                )
        else:
            left = db.rpc(
                "book_inventory",
                {
                    "p_room_type": q["room_type_id"],
                    "p_check_in": q["check_in"],
                    "p_check_out": q["check_out"],
                    "p_units": q["units"],
                },
            ).execute().data
            if left is None or left < 0:
                raise HTTPException(status.HTTP_409_CONFLICT, "Those dates are no longer available")

        paid = max(0, min(int(token_paise or 0), q["total_paise"]))

        # A ref collision is a 1-in-30-million coincidence and a unique
        # constraint violation; retrying twice is cheaper than a wider ref.
        reservation = None
        for _ in range(3):
            try:
                reservation = (
                    db.table("reservations")
                    .insert(
                        {
                            "ref": _new_ref(),
                            "property_id": property_id,
                            "partner_id": q["partner_id"],
                            "room_type_id": q["room_type_id"],
                            "rate_plan_id": q["rate_plan_id"],
                            "quote_id": q["id"],
                            "guest_name": guest_name,
                            "guest_phone": guest_phone,
                            "guest_email": guest_email,
                            "check_in": q["check_in"],
                            "check_out": q["check_out"],
                            "nights": (check_out - check_in).days,
                            "units": q["units"],
                            "adults": q["adults"],
                            "children": q["children"],
                            "gross_paise": q["gross_paise"],
                            "discount_paise": q["discount_paise"],
                            "tax_paise": q["tax_paise"],
                            "total_paise": q["total_paise"],
                            "paid_paise": paid,
                            "source": source,
                            "status": "confirmed",
                            "payment_status": _payment_status(paid, q["total_paise"]),
                            "notes": notes,
                            "created_by": created_by,
                        }
                    )
                    .execute()
                ).data[0]
                break
            except Exception as exc:  # noqa: BLE001 — retried, then re-raised
                if "reservations_ref_key" not in str(exc):
                    # Compensate: the stock moved, the reservation did not.
                    if q.get("hold_id"):
                        db.rpc(
                            "release_booking",
                            {
                                "p_room_type": q["room_type_id"],
                                "p_check_in": q["check_in"],
                                "p_check_out": q["check_out"],
                                "p_units": q["units"],
                            },
                        ).execute()
                    raise
        if reservation is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not allocate a booking reference")

        db.table("quotes").update({"status": "converted"}).eq("id", q["id"]).execute()

        # The per-night trail, so a folio stays reconstructible and a partial
        # cancellation has something to work from.
        nights_rows = [
            {
                "reservation_id": reservation["id"],
                "date": n["date"],
                "rate_paise": n["price_paise"],
            }
            for n in (q.get("nightly_paise") or [])
        ]
        if nights_rows:
            db.table("reservation_nights").insert(nights_rows).execute()

        if paid > 0:
            db.table("folio_entries").insert(
                {
                    "reservation_id": reservation["id"],
                    "property_id": property_id,
                    "partner_id": q["partner_id"],
                    "kind": "payment",
                    "description": "Booking token" if paid < q["total_paise"] else "Payment received",
                    # Payments are stored negative. The sign is applied once,
                    # here, so no caller has to remember which way round it goes.
                    "amount_paise": -paid,
                    "method": payment_method or "cash",
                    "posted_by_member": member_id,
                    "posted_by_name": member_name or "Front desk",
                    "idempotency_key": f"token:{reservation['id']}",
                }
            ).execute()

        return reservation

    return await run_db(_write)
