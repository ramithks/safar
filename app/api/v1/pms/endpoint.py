"""
PMS API — the hotel-facing operational endpoints.

    PWA ──► GET  /api/v1/pms/properties/{id}/availability   what can be sold
        ──► POST /api/v1/pms/properties/{id}/quotes         price it + hold it
        ──► POST /api/v1/pms/properties/{id}/reservations   quote → booking
        ──► POST .../reservations/{id}/check-in             front desk
        ──► POST .../reservations/{id}/folio                append to the account
        ──► POST /api/v1/pms/properties/{id}/tasks          raise a ticket

Every route resolves membership from the JWT before the handler runs
(`deps/property.require_member`). The property id in the path is checked against
the caller's membership there, so a guessed id 404s rather than reading someone
else's arrivals list. No route accepts a partner_id.

These write through the service-role client because several of them call stock
RPCs that are revoked from browser roles by design. RLS remains the backstop for
anything that reaches the database another way — the same posture as
marketplace/partner_ops.py.
"""
# NOTE: deliberately no `from __future__ import annotations` — PEP 563 breaks
# FastAPI body resolution through slowapi's wrapper. Same note as
# api/v1/admin/endpoint.py.
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.v1.pms.schemas import (
    BookingRequest,
    CheckInRequest,
    FolioEntryOut,
    FolioOut,
    FolioPostRequest,
    MeOut,
    NightlyOut,
    PropertyOut,
    QuoteOut,
    QuoteRequest,
    RatePlanOptionOut,
    RatePlanOut,
    ReservationOut,
    RoomOut,
    RoomStatusRequest,
    RoomTypeOut,
    StayOptionOut,
    TaskCreateRequest,
    TaskOut,
    TaskStatusRequest,
    TodayStatsOut,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.auth import require_user
from app.deps.property import require_member
from app.services.pms import availability as availability_service
from app.services.pms import booking as booking_service
from app.services.pms import frontdesk as frontdesk_service
from app.services.pms import workspace as workspace_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pms", tags=["pms"])


# ── workspace ────────────────────────────────────────────────────────────────
# What the PWA asks before it can render anything: who am I, and which hotels
# may I open. Both derive from the JWT alone. Note these two take `require_user`
# rather than `require_member` — there is no property in the path yet, which is
# exactly why they must do their own scoping and never take a hint from the
# client about which partner to look at.

@router.get("/me", response_model=MeOut)
@limiter.limit("120/minute")
async def get_me(request: Request, user=Depends(require_user)):
    """
    The caller, and their role at each partner they belong to.

    `permissions` is flattened from the role table plus per-member grants so the
    client can decide which buttons to draw. It is not a security boundary and
    must never be treated as one — every route re-derives the same answer
    server-side through `require_member`.
    """
    return await workspace_service.me(str(user.id))


@router.get("/properties", response_model=list[PropertyOut])
@limiter.limit("120/minute")
async def list_properties(request: Request, user=Depends(require_user)):
    """Every property this person may open, and only those."""
    rows = await workspace_service.list_properties(str(user.id))
    return [PropertyOut.from_row(r) for r in rows]


@router.get("/properties/{property_id}/today", response_model=TodayStatsOut)
@limiter.limit("120/minute")
async def get_today(request: Request, property_id: str, member=Depends(require_member())):
    """Occupancy, ADR, RevPAR and the arrival/departure counts for today."""
    return await workspace_service.today_stats(property_id)


@router.get("/properties/{property_id}/room-types", response_model=list[RoomTypeOut])
@limiter.limit("120/minute")
async def list_room_types(request: Request, property_id: str, member=Depends(require_member())):
    rows = await workspace_service.list_room_types(property_id)
    return [RoomTypeOut.from_row(r) for r in rows]


@router.get("/properties/{property_id}/rate-plans", response_model=list[RatePlanOut])
@limiter.limit("120/minute")
async def list_rate_plans(request: Request, property_id: str, member=Depends(require_member())):
    rows = await workspace_service.list_rate_plans(property_id)
    return [RatePlanOut.from_row(r) for r in rows]


@router.get("/properties/{property_id}/tasks", response_model=list[TaskOut])
@limiter.limit("120/minute")
async def list_tasks(request: Request, property_id: str, member=Depends(require_member("tasks"))):
    rows = await workspace_service.list_tasks(property_id)
    return [TaskOut.from_row(r) for r in rows]


# ── availability ─────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/availability", response_model=list[StayOptionOut])
@limiter.limit("120/minute")
async def get_availability(
    request: Request,
    property_id: str,
    check_in: date = Query(..., alias="checkIn"),
    check_out: date = Query(..., alias="checkOut"),
    units: int = Query(1, ge=1, le=20),
    audience: str | None = Query(None),
    member=Depends(require_member()),
):
    """
    What this property can sell for these dates.

    `audience` is honoured but not trusted to widen access: the front desk may
    legitimately see agent rates, because it books on behalf of agents. The
    public booking engine and the agent portal are separate endpoints with their
    own audience decided server-side, not this one with a query parameter.
    """
    options = await availability_service.stay_options(
        property_id, check_in, check_out, units=units, audience=audience
    )
    return [
        StayOptionOut(
            roomTypeId=o.room_type_id,
            roomTypeCode=o.room_type_code,
            roomTypeName=o.room_type_name,
            unitsLeft=o.units_left,
            maxOccupancy=o.max_occupancy,
            ratePlans=[
                RatePlanOptionOut(
                    ratePlanId=p.rate_plan_id,
                    code=p.code,
                    name=p.name,
                    mealPlan=p.meal_plan,
                    audience=p.audience,
                    refundable=p.refundable,
                    inclusions=p.inclusions,
                    totalPaise=p.total_paise,
                    perNightPaise=p.per_night_paise,
                    minStayViolation=p.min_stay_violation,
                    closed=p.closed,
                )
                for p in o.rate_plans
            ],
        )
        for o in options
    ]


# ── quotes ───────────────────────────────────────────────────────────────────

@router.post("/properties/{property_id}/quotes", response_model=QuoteOut)
@limiter.limit("60/minute")
async def create_quote(
    request: Request,
    property_id: str,
    body: QuoteRequest,
    member=Depends(require_member("frontdesk")),
):
    """
    Price a stay and hold the stock.

    The hold comes first and is why this is a POST rather than a GET: asking for
    a price takes a room off sale for fifteen minutes, which is a side effect
    and should look like one.
    """
    result = await booking_service.create_quote(
        property_id=property_id,
        rate_plan_id=body.ratePlanId,
        check_in=body.checkIn,
        check_out=body.checkOut,
        units=body.units,
        adults=body.adults,
        children=body.children,
        requested_discount_pct=body.discountPct,
        discount_reason=body.discountReason,
        issued_by="employee",
        issued_by_member=member.member_id,
        conversation_id=body.conversationId,
    )

    return QuoteOut(
        id=result.id,
        holdId=result.hold_id,
        propertyId=property_id,
        roomTypeId=result.room_type_id,
        ratePlanId=result.rate_plan_id,
        checkIn=result.check_in,
        checkOut=result.check_out,
        units=result.priced.units,
        adults=result.adults,
        children=result.children,
        nightly=[NightlyOut(date=n.date, pricePaise=n.price_paise) for n in result.priced.nightly],
        grossPaise=result.priced.gross_paise,
        discountPaise=result.priced.discount_paise,
        discountReason=body.discountReason if result.priced.discount_paise else None,
        taxPaise=result.priced.tax_paise,
        totalPaise=result.priced.total_paise,
        tokenPaise=result.priced.token_paise,
        balancePaise=result.priced.balance_paise,
        issuedBy="employee",
        expiresAt=result.expires_at,
        clampedFromPct=result.priced.clamped_from_pct,
    )


# ── reservations ─────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/reservations", response_model=list[ReservationOut])
@limiter.limit("120/minute")
async def list_reservations(
    request: Request,
    property_id: str,
    bucket: str | None = Query(None, description="arrivals | departures | inhouse"),
    search: str | None = Query(None, max_length=80),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=500),
    member=Depends(require_member()),
):
    """
    The reservation list, and the three front-desk buckets.

    The bucket definitions are duplicated in the PWA's offline cache
    (src/data/filters.ts) so an offline arrivals list means the same thing. If
    these two ever disagree, the list changes the moment a connection drops and
    the desk has no way to tell which one is right.
    """
    today = date.today().isoformat()

    def _read():
        db = get_db()
        q = db.table("reservations").select("*").eq("property_id", property_id)

        if bucket == "arrivals":
            q = q.eq("check_in", today).in_("status", ["confirmed", "checked_in"])
        elif bucket == "departures":
            q = q.eq("check_out", today).in_("status", ["checked_in", "checked_out"])
        elif bucket == "inhouse":
            q = q.eq("status", "checked_in")

        # Half-open overlap: a stay is in the window if it has not ended before
        # it and has not started after it.
        if date_from:
            q = q.gte("check_out", date_from.isoformat())
        if date_to:
            q = q.lte("check_in", date_to.isoformat())
        if search:
            escaped = search.replace("%", r"\%").replace(",", " ")
            q = q.or_(f"guest_name.ilike.%{escaped}%,ref.ilike.%{escaped}%,guest_phone.ilike.%{escaped}%")

        return q.order("check_in").limit(limit).execute().data or []

    rows = await run_db(_read)
    return [ReservationOut.from_row(r) for r in rows]


@router.post("/properties/{property_id}/reservations", response_model=ReservationOut)
@limiter.limit("60/minute")
async def create_reservation(
    request: Request,
    property_id: str,
    body: BookingRequest,
    member=Depends(require_member("frontdesk")),
):
    """Turn a quote into a booking. The quote is consumed, so a double-submit 409s."""
    row = await booking_service.create_reservation(
        property_id=property_id,
        quote_id=body.quoteId,
        guest_name=body.guestName,
        guest_phone=body.guestPhone,
        guest_email=body.guestEmail,
        source=body.source,
        token_paise=body.tokenPaise,
        payment_method=body.paymentMethod,
        notes=body.notes,
        created_by="employee",
        member_id=member.member_id,
        member_name=member.display_name,
    )
    return ReservationOut.from_row(row)


@router.get("/properties/{property_id}/reservations/{reservation_id}",
            response_model=ReservationOut)
@limiter.limit("240/minute")
async def get_reservation(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member()),
):
    """
    One reservation, for the detail drawer.

    Scoped by property in the query rather than fetched by id alone: a
    reservation id from another hotel must 404 here exactly as a property id
    does, or the drawer becomes the enumeration oracle the rest of the API is
    careful not to be.
    """
    row = await workspace_service.get_reservation(property_id, reservation_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
    return ReservationOut.from_row(row)


@router.post("/properties/{property_id}/reservations/{reservation_id}/check-in",
             response_model=ReservationOut)
@limiter.limit("120/minute")
async def check_in(
    request: Request,
    property_id: str,
    reservation_id: str,
    body: CheckInRequest,
    member=Depends(require_member("frontdesk")),
):
    row = await frontdesk_service.check_in(property_id, reservation_id, body.roomId, member)
    return ReservationOut.from_row(row)


@router.post("/properties/{property_id}/reservations/{reservation_id}/check-out",
             response_model=ReservationOut)
@limiter.limit("120/minute")
async def check_out(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("frontdesk")),
):
    row = await frontdesk_service.check_out(property_id, reservation_id, member)
    return ReservationOut.from_row(row)


# ── folio ────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/reservations/{reservation_id}/folio",
            response_model=FolioOut)
@limiter.limit("120/minute")
async def get_folio(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("folio")),
):
    folio = await frontdesk_service.get_folio(property_id, reservation_id)
    return FolioOut(
        entries=[FolioEntryOut.from_row(r) for r in folio["entries"]],
        balancePaise=folio["balance_paise"],
    )


@router.post("/properties/{property_id}/reservations/{reservation_id}/folio",
             response_model=FolioEntryOut)
@limiter.limit("120/minute")
async def post_folio(
    request: Request,
    property_id: str,
    reservation_id: str,
    body: FolioPostRequest,
    member=Depends(require_member("folio")),
):
    """
    Append a charge or a payment.

    Idempotent on `idempotencyKey`, which the PMS generates before it queues the
    operation. That is the whole reason a receptionist can take a cash payment
    with the network down and not charge the guest twice when it drains.
    """
    row = await frontdesk_service.post_folio_entry(
        property_id,
        reservation_id,
        kind=body.kind,
        description=body.description,
        amount_paise=body.amountPaise,
        method=body.method,
        idempotency_key=body.idempotencyKey,
        member=member,
    )
    return FolioEntryOut.from_row(row)


# ── rooms and tasks ──────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/rooms", response_model=list[RoomOut])
@limiter.limit("120/minute")
async def list_rooms(
    request: Request,
    property_id: str,
    member=Depends(require_member()),
):
    """
    Every room in the property, with whoever is currently in it.

    Read by two screens that want opposite things: housekeeping wants the
    status board, and check-in wants the list of rooms it may actually assign.
    Both need the occupant, because a room can be clean and still unavailable —
    resolving that in the client would mean shipping the arrivals list to a
    housekeeper who has no business reading it.
    """
    def _read():
        db = get_db()
        rooms = (
            db.table("rooms")
            .select("*")
            .eq("property_id", property_id)
            .order("number")
            .execute()
            .data
            or []
        )
        if not rooms:
            return [], {}
        # One query for the whole floor rather than one per room.
        stays = (
            db.table("reservations")
            .select("id,room_id,guest_name,check_out")
            .eq("property_id", property_id)
            .eq("status", "checked_in")
            .not_.is_("room_id", "null")
            .execute()
            .data
            or []
        )
        return rooms, {s["room_id"]: s for s in stays}

    rooms, occupants = await run_db(_read)
    return [RoomOut.from_row(r, occupants.get(r["id"])) for r in rooms]


@router.post("/properties/{property_id}/rooms/{room_id}/status")
@limiter.limit("240/minute")
async def set_room_status(
    request: Request,
    property_id: str,
    room_id: str,
    body: RoomStatusRequest,
    member=Depends(require_member("housekeeping")),
):
    return await frontdesk_service.set_room_status(property_id, room_id, body.status)


@router.post("/properties/{property_id}/tasks", response_model=TaskOut)
@limiter.limit("120/minute")
async def create_task(
    request: Request,
    property_id: str,
    body: TaskCreateRequest,
    member=Depends(require_member("tasks")),
):
    """
    Raise a ticket, optionally linked to the guest message that caused it.

    The id is supplied by the caller. A retry, a double-tap, or an outbox replay
    all resolve to the same ticket rather than three.
    """
    row = await frontdesk_service.create_task(
        property_id,
        task_id=body.id,
        title=body.title,
        category=body.category,
        priority=body.priority,
        room_number=body.roomNumber,
        conversation_id=body.conversationId,
        source_channel=body.sourceChannel,
        guest_name=body.guestName,
        created_by=body.createdBy,
        member=member,
    )
    return TaskOut.from_row(row)


@router.post("/properties/{property_id}/tasks/{task_id}/status", response_model=TaskOut)
@limiter.limit("240/minute")
async def set_task_status(
    request: Request,
    property_id: str,
    task_id: str,
    body: TaskStatusRequest,
    member=Depends(require_member("tasks")),
):
    row = await frontdesk_service.set_task_status(property_id, task_id, body.status, member)
    return TaskOut.from_row(row)
