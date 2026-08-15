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
import json
import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.v1.pms.schemas import (
    AgentActionOut,
    AgentAutonomyRequest,
    AgentDecisionRequest,
    AgentOut,
    AriEditOut,
    BillingSummaryOut,
    BookingRequest,
    CheckInRequest,
    ConsoleActionOut,
    CampaignOut,
    ConsoleAskOut,
    ConsoleAskRequest,
    ConversationHandlerRequest,
    ConversationOut,
    FolioEntryOut,
    FolioOut,
    FolioPostRequest,
    InventoryEditRequest,
    InventoryNightOut,
    MeOut,
    MessageOut,
    NightAuditOut,
    NightAuditRequest,
    NightlyOut,
    PropertyOut,
    QuoteOut,
    QuoteRequest,
    RateEditRequest,
    RateNightOut,
    RatePlanOptionOut,
    RatePlanOut,
    ReservationOut,
    RoomOut,
    RoomStatusRequest,
    RoomTypeOut,
    SendMessageRequest,
    StayOptionOut,
    SyncOut,
    TaskCreateRequest,
    TaskOut,
    TaskStatusRequest,
    TodayStatsOut,
    UsageEventOut,
    UsageGroupOut,
    UsageMeterOut,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.auth import require_user
from app.deps.property import require_member
from app.services import billing
from app.services.pms import ari as ari_service
from app.services.pms import availability as availability_service
from app.services.pms import clock
from app.services.pms import booking as booking_service
from app.services.pms import frontdesk as frontdesk_service
from app.services.pms import sync as sync_service
from app.services.ai import approvals
from app.services.ai import console as ai_console
from app.services.ai import model_router
from app.services.messaging import service as messaging_service
from app.services.messaging.channels import MessagingError
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


# ── sync ─────────────────────────────────────────────────────────────────────

# Property-scoped in the path, like every other route here, rather than
# `/pms/sync?propertyId=`. `require_member` resolves the property from the path
# parameter, and giving one endpoint a second way to name its property means a
# second way to get that check wrong.
@router.get("/properties/{property_id}/sync", response_model=SyncOut)
@limiter.limit("60/minute")
async def sync(
    request: Request,
    property_id: str,
    since: datetime | None = Query(None),
    member=Depends(require_member()),
):
    """
    Everything that changed for this property since a cursor — including what
    was deleted.

    The deletions are the point. The PWA refreshes IndexedDB by merging, and a
    merge can add or update but never remove, so a reservation deleted on the
    server would stay on an offline arrivals list indefinitely. Absence from a
    normal list response cannot stand in for deletion — that response is
    filtered and windowed, and half the rows are legitimately missing from it.
    Only the server knows the difference, so only the server can say.

    `fullResync` is set when this answer cannot be trusted to be complete: no
    cursor, a truncated page, or a cursor older than the oldest deletion still
    on record. The client's job then is to discard its local copy, not to merge
    what it got. A sync that cannot admit it lost track will eventually hand
    back a confident, wrong answer instead.

    `serverTime` is what to send as the next `since` — the server's clock, so a
    desk PC that is running fast cannot skip a window of changes.
    """
    changes = await sync_service.changes_since(
        property_id, since.isoformat() if since else None
    )
    return SyncOut(
        serverTime=changes["serverTime"],
        fullResync=changes["fullResync"],
        reservations=[ReservationOut.from_row(r) for r in changes["reservations"]],
        rooms=[RoomOut.from_row(r) for r in changes["rooms"]],
        tasks=[TaskOut.from_row(r) for r in changes["tasks"]],
        folioEntries=[FolioEntryOut.from_row(r) for r in changes["folioEntries"]],
        deleted=changes["deleted"],
    )


# ── calendar (ARI) ───────────────────────────────────────────────────────────
#
# Reads are open to any member: a receptionist needs to see what is left tonight.
# Writes need `rates`, which is the permission a manager has and a receptionist
# does not — pricing the property is not a front-desk decision.

@router.get("/properties/{property_id}/inventory", response_model=list[InventoryNightOut])
@limiter.limit("120/minute")
async def get_inventory(
    request: Request,
    property_id: str,
    from_date: date = Query(..., alias="from"),
    days: int = Query(30, ge=1, le=120),
    member=Depends(require_member()),
):
    """Per-night stock for the calendar grid, with sparse inheritance resolved."""
    return await ari_service.get_inventory(property_id, from_date, days)


@router.get("/properties/{property_id}/rates", response_model=list[RateNightOut])
@limiter.limit("120/minute")
async def get_rates(
    request: Request,
    property_id: str,
    from_date: date = Query(..., alias="from"),
    days: int = Query(30, ge=1, le=120),
    member=Depends(require_member()),
):
    """Per-night price and restrictions, per rate plan."""
    return await ari_service.get_rates(property_id, from_date, days)


@router.patch("/properties/{property_id}/inventory", response_model=AriEditOut)
@limiter.limit("60/minute")
async def edit_inventory(
    request: Request,
    property_id: str,
    body: InventoryEditRequest,
    member=Depends(require_member("rates")),
):
    """
    Change stock over a date range.

    One call for the whole range rather than one per night: a half-applied
    "block the last week of December" leaves a property with a hole nobody
    typed, and thirty round trips from a browser is thirty chances to be
    interrupted. Cutting stock below what is already sold is refused with the
    night named, never clamped.
    """
    nights = await ari_service.set_inventory(
        property_id,
        room_type_id=body.roomTypeId,
        from_date=body.fromDate,
        to_date=body.toDate,
        units_total=body.unitsTotal,
        units_blocked=body.unitsBlocked,
        stop_sell=body.stopSell,
        oversell=body.oversellAllowance,
    )
    return AriEditOut(nights=nights)


@router.patch("/properties/{property_id}/rates", response_model=AriEditOut)
@limiter.limit("60/minute")
async def edit_rates(
    request: Request,
    property_id: str,
    body: RateEditRequest,
    member=Depends(require_member("rates")),
):
    """Change price and restrictions over a date range. Null means leave alone."""
    nights = await ari_service.set_rates(
        property_id,
        rate_plan_id=body.ratePlanId,
        from_date=body.fromDate,
        to_date=body.toDate,
        price_paise=body.pricePaise,
        min_stay=body.minStay,
        max_stay=body.maxStay,
        closed_to_arrival=body.closedToArrival,
        closed_to_departure=body.closedToDeparture,
        closed=body.closed,
    )
    return AriEditOut(nights=nights)


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

    The buckets, the search and the balance map all come from
    `services/pms/workspace.list_reservations` — the same function the Front
    Desk agent's `list_arrivals` calls, and the same status sets Today's counts
    use. One definition, so the headline number, the list underneath it and
    whatever the AI reads out are the same query with different projections.
    They are mirrored again in the PWA's offline cache (src/data/filters.ts),
    because a list that changes the moment a connection drops leaves the desk
    with no way to tell which version is right.
    """
    rows, balances = await workspace_service.list_reservations(
        property_id,
        bucket=bucket,
        search=search,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        limit=limit,
    )
    return [ReservationOut.from_row(r, folio_balance=balances.get(r["id"], 0)) for r in rows]


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
    found = await workspace_service.get_reservation(property_id, reservation_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
    row, balance = found
    return ReservationOut.from_row(row, folio_balance=balance)


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


@router.post("/properties/{property_id}/night-audit", response_model=NightAuditOut)
@limiter.limit("6/minute")
async def run_night_audit(
    request: Request,
    property_id: str,
    body: NightAuditRequest | None = None,
    member=Depends(require_member("folio")),
):
    """
    Bring every in-house folio up to date.

    Normally pg_cron's job. Exposed because a property that was offline for two
    days needs a way to catch up, and because "press this and see" is a far
    better answer than telling a manager to wait until 01:30. Safe to press
    repeatedly — each night posts exactly once, enforced by a unique key on the
    ledger rather than by a check the caller could race.

    The business date is accepted so a property can close a day it has already
    passed. It cannot be used to post the future: nights are only ever posted up
    to the reservation's own departure.
    """
    return await frontdesk_service.run_night_audit(
        property_id, business_date=(body.businessDate.isoformat() if body and body.businessDate else None)
    )


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
    rooms, occupants = await workspace_service.list_rooms(property_id)
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


# ─────────────────────────────────────────────────────────────────────────────
# Inbox
#
# The screens that were on seed data until slice 2. Everything here reads the
# same `conversations` and `conversation_messages` rows the WhatsApp webhook
# writes, so a receptionist watching the inbox sees a guest's message arrive and
# the agent's answer follow it — one thread, not two systems.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/conversations", response_model=list[ConversationOut])
@limiter.limit("120/minute")
async def list_conversations(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=300),
    member=Depends(require_member("messaging")),
):
    def _read():
        db = get_db()
        q = (
            db.table("conversations")
            .select("*")
            .eq("property_id", property_id)
            .order("last_message_at", desc=True)
            .limit(limit)
        )
        if status_filter:
            q = q.eq("status", status_filter)
        rows = q.execute().data or []

        # One lookup for every assignee rather than one per row.
        member_ids = {r["assignee_member"] for r in rows if r.get("assignee_member")}
        names: dict[str, str] = {}
        if member_ids:
            members = (
                db.table("partner_members").select("id,profile_id")
                .in_("id", sorted(member_ids)).execute().data or []
            )
            profile_ids = {m["profile_id"] for m in members if m.get("profile_id")}
            profiles = (
                db.table("profiles").select("id,full_name")
                .in_("id", sorted(profile_ids)).execute().data or []
            ) if profile_ids else []
            by_profile = {p["id"]: p.get("full_name") for p in profiles}
            names = {m["id"]: by_profile.get(m["profile_id"]) for m in members}
        return rows, names

    rows, names = await run_db(_read)
    return [
        ConversationOut.from_row(
            r,
            window_open=messaging_service.window_open(r),
            assignee_name=names.get(r.get("assignee_member")),
        )
        for r in rows
    ]


@router.get(
    "/properties/{property_id}/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
@limiter.limit("240/minute")
async def list_messages(
    request: Request,
    property_id: str,
    conversation_id: str,
    member=Depends(require_member("messaging")),
):
    def _read():
        db = get_db()
        rows = (
            db.table("conversation_messages")
            .select("*")
            .eq("property_id", property_id)
            .eq("conversation_id", conversation_id)
            .order("sent_at")
            .limit(300)
            .execute()
            .data
            or []
        )
        # Opening a thread is reading it. The count is the receptionist's, not
        # the guest's, so it clears here rather than on a separate call nobody
        # would remember to make.
        if rows:
            db.table("conversations").update({"unread_count": 0}).eq(
                "id", conversation_id
            ).eq("property_id", property_id).execute()
        return rows

    return [MessageOut.from_row(r) for r in await run_db(_read)]


@router.post(
    "/properties/{property_id}/conversations/{conversation_id}/messages",
    response_model=MessageOut,
)
@limiter.limit("60/minute")
async def send_conversation_message(
    request: Request,
    property_id: str,
    conversation_id: str,
    body: SendMessageRequest,
    member=Depends(require_member("messaging")),
):
    """
    A staff reply.

    Goes through the same `messaging.send` the agent uses, so a receptionist
    typing outside the 24-hour window is refused for exactly the same reason and
    with exactly the same message. Meta's rule does not care who is typing, and
    a second code path would be a second chance to forget that.

    **Sending takes the conversation over.** A person typing a reply is a person
    handling it, and leaving the agent running underneath would let it answer
    the guest's next message over the top of someone mid-conversation. The
    explicit Take over button exists for picking a thread up without saying
    anything yet; this is the same decision made by doing rather than declaring.
    """
    conversation = await run_db(_load_conversation, property_id, conversation_id)
    if not conversation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    try:
        sent = await messaging_service.send(
            conversation,
            body=body.body,
            author="staff",
            author_name=member.display_name,
            author_member=member.member_id,
        )
    except MessagingError as exc:
        # 409 rather than 500: the request was well formed and the refusal is a
        # fact about the conversation, which is what the desk needs to be told.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": exc.code, "detail": exc.detail},
        ) from exc

    if (conversation.get("handler") or "ai") != "human":
        await messaging_service.take_over(conversation_id, property_id, member)

    return MessageOut.from_row(sent.message)


@router.post(
    "/properties/{property_id}/conversations/{conversation_id}/handler",
    response_model=ConversationOut,
)
@limiter.limit("60/minute")
async def set_conversation_handler(
    request: Request,
    property_id: str,
    conversation_id: str,
    body: ConversationHandlerRequest,
    member=Depends(require_member("messaging")),
):
    """
    Take over, or hand back.

    Taking over stops the agent immediately — the pipeline checks `handler`
    before it opens a run, so nothing in flight can talk over the person who
    just picked it up. Handing back is deliberately a separate action: a
    conversation never drifts back to the AI because a human stopped typing.
    """
    if body.handler == "human":
        row = await messaging_service.take_over(conversation_id, property_id, member)
    else:
        row = await messaging_service.resume_ai(conversation_id, property_id)

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return ConversationOut.from_row(row, window_open=messaging_service.window_open(row))


def _load_action(action_id: str, property_id: str) -> dict | None:
    rows = (
        get_db().table("agent_actions").select("*")
        .eq("id", action_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _load_conversation(property_id: str, conversation_id: str) -> dict | None:
    rows = (
        get_db().table("conversations").select("*")
        .eq("id", conversation_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# Agents, and the approval queue
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/agents", response_model=list[AgentOut])
@limiter.limit("60/minute")
async def list_agents(
    request: Request,
    property_id: str,
    member=Depends(require_member()),
):
    """
    Every agent configured for this property.

    Any member may see *that* an agent exists and what it may do — a
    receptionist working beside it should be able to. The system prompt is not
    here: reading how the agent was told to behave needs the 'agents'
    permission, which is enforced by RLS on `agent_configs` and mirrored by
    simply not selecting it.
    """
    def _read():
        db = get_db()
        agents = (
            db.table("agents").select("*")
            .eq("property_id", property_id).order("agent_type").execute().data or []
        )
        if not agents:
            return [], {}, {}, {}, {}, {}

        ids = [a["id"] for a in agents]
        configs = {
            c["id"]: c for c in (
                db.table("agent_configs").select("id,model,provider,version")
                .in_("id", [a["active_config_id"] for a in agents if a.get("active_config_id")])
                .execute().data or []
            )
        } if any(a.get("active_config_id") for a in agents) else {}

        tools: dict[str, list[str]] = {}
        for t in (db.table("agent_tools").select("agent_id,tool_name,enabled")
                  .in_("agent_id", ids).execute().data or []):
            if t.get("enabled", True):
                tools.setdefault(t["agent_id"], []).append(t["tool_name"])

        perms: dict[str, list[str]] = {}
        for p in (db.table("agent_permissions").select("agent_id,scope")
                  .in_("agent_id", ids).execute().data or []):
            perms.setdefault(p["agent_id"], []).append(p["scope"])

        runs: dict[str, dict] = {}
        run_ids: dict[str, str] = {}
        for r in (db.table("agent_runs").select("id,agent_id,started_at,conversation_id")
                  .in_("agent_id", ids).order("started_at", desc=True)
                  .limit(500).execute().data or []):
            slot = runs.setdefault(r["agent_id"], {
                "last": None, "count": 0, "conversations": set(),
            })
            slot["last"] = slot["last"] or r["started_at"]
            slot["count"] += 1
            if r.get("conversation_id"):
                slot["conversations"].add(r["conversation_id"])
            run_ids[r["id"]] = r["agent_id"]

        # What each agent has cost the hotel, from the ledger's own figure.
        #
        # Summed from `usage_events.credits_charged` rather than derived from a
        # run count times a rate: the two agree today and would stop agreeing
        # the first time a rate changed mid-month, and the one that is right is
        # the one the hotel was actually billed.
        stats: dict[str, dict] = {}
        for e in (db.table("usage_events").select("agent_id,credits_charged")
                  .in_("agent_id", ids).limit(2000).execute().data or []):
            if not e.get("agent_id"):
                continue
            slot = stats.setdefault(e["agent_id"], {"credits": 0.0})
            slot["credits"] += float(e.get("credits_charged") or 0)

        for a in (db.table("agent_actions").select("agent_id,status")
                  .in_("agent_id", ids).limit(2000).execute().data or []):
            slot = stats.setdefault(a["agent_id"], {"credits": 0.0})
            slot["actions"] = slot.get("actions", 0) + 1
            if a["status"] == "awaiting_approval":
                slot["pending"] = slot.get("pending", 0) + 1

        return agents, configs, tools, perms, runs, stats

    agents, configs, tools, perms, runs, stats = await run_db(_read)
    out = []
    for a in agents:
        config = configs.get(a.get("active_config_id")) or {}
        activity = runs.get(a["id"]) or {}
        meter = stats.get(a["id"]) or {}
        # The *effective* model, not the configured one. Most properties leave
        # `agent_configs.model` empty and let the router choose by tier, and a
        # screen that showed "—" there would be telling a hotelier their agent
        # has no model when it plainly has one.
        effective = config.get("model") or model_router.select(
            agent_type=a["agent_type"], task="guest_conversation"
        ).model
        out.append(AgentOut(
            id=a["id"],
            propertyId=a["property_id"],
            agentType=a["agent_type"],
            name=a["name"],
            autonomyLevel=a["autonomy_level"],
            model=effective,
            provider=config.get("provider") or model_router.PROVIDER_VERTEX,
            isActive=a["status"] == "active",
            status=a["status"],
            channels=a.get("channels") or [],
            tools=sorted(tools.get(a["id"], [])),
            permissions=sorted(perms.get(a["id"], [])),
            lastRunAt=activity.get("last"),
            runsToday=activity.get("count", 0),
            creditsCharged=round(meter.get("credits", 0.0)),
            actionsCount=meter.get("actions", 0),
            conversationsCount=len(activity.get("conversations") or ()),
            pendingApprovals=meter.get("pending", 0),
        ))
    return out


@router.patch("/properties/{property_id}/agents/{agent_id}", response_model=AgentOut)
@limiter.limit("30/minute")
async def set_agent_autonomy(
    request: Request,
    property_id: str,
    agent_id: str,
    body: AgentAutonomyRequest,
    member=Depends(require_member("agents")),
):
    """
    Move the dial.

    The only control a hotel has over what its agent may do unattended, and the
    reason the tools are granted at full scope and held back by level — see
    services/ai/agents/guest.py. Requires the 'agents' permission; a receptionist
    cannot give the AI more authority than they have themselves.
    """
    def _write():
        rows = (
            get_db().table("agents")
            .update({"autonomy_level": body.autonomyLevel})
            .eq("id", agent_id).eq("property_id", property_id)
            .execute().data
        )
        return rows[0] if rows else None

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    # The agent row alone. The tool and permission lists come from `list_agents`
    # and the client refetches; duplicating those four queries here to return a
    # fully-populated object would be a second place they can drift.
    return AgentOut(
        id=row["id"], propertyId=row["property_id"], agentType=row["agent_type"],
        name=row["name"], autonomyLevel=row["autonomy_level"], model="", provider="",
        isActive=row["status"] == "active", status=row["status"],
        channels=row.get("channels") or [],
    )


@router.post("/properties/{property_id}/frontdesk/ask", response_model=ConsoleAskOut)
@limiter.limit("30/minute")
async def ask_frontdesk_agent(
    request: Request,
    property_id: str,
    body: ConsoleAskRequest,
    member=Depends(require_member("frontdesk")),
):
    """
    Ask the Front Desk agent something, and let it act.

    **The authorisation happens here and nowhere else that matters.**
    `require_member("frontdesk")` resolves the caller against this property from
    their JWT; everything downstream — the agent context, every tool, every PMS
    call — is bounded by the `property_id` that check produced. The request body
    carries a sentence and, optionally, a task name from a fixed list. It cannot
    carry a property, a partner, a guest or a reservation, so there is no field
    in it that a prompt injection could aim at. The model is not the boundary;
    this dependency is.

    An absent task is classified rather than defaulted — see
    `services/ai/classify`. That classifier is given no tools and never the
    property id, so the worst a message can talk it into is the wrong model.

    Requires 'frontdesk' rather than merely membership: a housekeeper has no
    business checking guests in, and the agent must not become a way to do
    something the person driving it could not do themselves. The agent's own
    scopes are a separate and narrower set — a manager with every permission
    still only gets what `agent_permissions` grants.
    """
    try:
        result, conversation = await ai_console.ask(
            property_id=property_id,
            member=member,
            message=body.message,
            task=body.task,
        )
    except ai_console.ConsoleRefused as exc:
        # 409: the request was well formed and the refusal is a fact about the
        # thread — a person has it, or no agent is configured. Both are things
        # the desk needs told, not a server error.
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "detail": exc.detail}
        ) from exc

    actions = [
        ConsoleActionOut(
            tool=a.tool, status=a.status, refusalReason=a.refusal_reason,
            resultType=a.result_type, resultId=a.result_id, latencyMs=a.latency_ms,
        )
        for a in result.actions
    ]
    return ConsoleAskOut(
        runId=result.run_id,
        conversationId=conversation["id"],
        status=result.status,
        reply=result.reply,
        handoff=result.handoff,
        actions=actions,
        pendingApproval=any(a.status == "awaiting_approval" for a in actions),
        tokensIn=result.tokens_in,
        tokensOut=result.tokens_out,
        latencyMs=result.latency_ms,
    )


@router.post("/properties/{property_id}/marketing/ask", response_model=ConsoleAskOut)
@limiter.limit("30/minute")
async def ask_marketing_agent(
    request: Request,
    property_id: str,
    body: ConsoleAskRequest,
    member=Depends(require_member("marketing")),
):
    """
    Ask the Marketing agent to plan something.

    The same console, the same runtime and the same conversation model as the
    front desk — what differs is one argument. Two agents share the `internal`
    channel now, and channel routing puts the front desk first, so the caller
    names which one it wants. That is the honest model for a staff surface:
    which agent you are talking to is a screen somebody opened.

    `require_member("marketing")` rather than mere membership. A receptionist
    has no business planning campaigns to the guest list, and the agent must not
    become a way to do something the person driving it could not do themselves.
    Nothing is sent from here either way: `execute_campaign` always requires a
    separate approval, which is the 'agents' permission and a different person's
    decision.
    """
    try:
        result, conversation = await ai_console.ask(
            property_id=property_id,
            member=member,
            message=body.message,
            task=body.task,
            agent_type="marketing",
        )
    except ai_console.ConsoleRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "detail": exc.detail}
        ) from exc

    actions = [
        ConsoleActionOut(
            tool=a.tool, status=a.status, refusalReason=a.refusal_reason,
            resultType=a.result_type, resultId=a.result_id, latencyMs=a.latency_ms,
        )
        for a in result.actions
    ]
    return ConsoleAskOut(
        runId=result.run_id,
        conversationId=conversation["id"],
        status=result.status,
        reply=result.reply,
        handoff=result.handoff,
        actions=actions,
        pendingApproval=any(a.status == "awaiting_approval" for a in actions),
        tokensIn=result.tokens_in,
        tokensOut=result.tokens_out,
        latencyMs=result.latency_ms,
    )


@router.get("/properties/{property_id}/campaigns", response_model=list[CampaignOut])
@limiter.limit("120/minute")
async def list_campaigns(
    request: Request,
    property_id: str,
    limit: int = Query(40, ge=1, le=200),
    member=Depends(require_member("marketing")),
):
    """
    Every campaign at this property, with the counts a screen needs.

    The counts come from `campaign_recipients` rather than from columns on the
    campaign — one place, computed the same way the run computes it, so a screen
    and a worker can never disagree about how many people were reached.
    """
    def _read():
        db = get_db()
        campaigns = (
            db.table("marketing_campaigns").select("*")
            .eq("property_id", property_id)
            .order("created_at", desc=True).limit(limit).execute().data or []
        )
        if not campaigns:
            return [], {}, {}

        ids = [c["id"] for c in campaigns]
        counts: dict[str, dict] = {}
        for row in (db.table("campaign_recipients").select("campaign_id,status,reason")
                    .in_("campaign_id", ids).limit(5000).execute().data or []):
            slot = counts.setdefault(row["campaign_id"], {"reasons": {}})
            slot[row["status"]] = slot.get(row["status"], 0) + 1
            if row.get("reason"):
                slot["reasons"][row["reason"]] = slot["reasons"].get(row["reason"], 0) + 1

        # What each campaign has cost the hotel, from the ledger's own figure —
        # the same argument as the per-agent credits on /agents: a count times a
        # rate stops being right the first time a rate changes mid-month.
        spend: dict[str, dict] = {}
        for e in (db.table("usage_events")
                  .select("external_ref,credits_charged,provider_cost_inr_micros")
                  .eq("usage_type", "marketing.campaign_send")
                  .in_("external_ref", ids).limit(5000).execute().data or []):
            slot = spend.setdefault(e["external_ref"], {"credits": 0.0, "cost": 0})
            slot["credits"] += float(e.get("credits_charged") or 0)
            slot["cost"] += int(e.get("provider_cost_inr_micros") or 0)

        return campaigns, counts, spend

    campaigns, counts, spend = await run_db(_read)
    out = []
    for c in campaigns:
        n = counts.get(c["id"]) or {}
        money = spend.get(c["id"]) or {}
        out.append(CampaignOut(
            id=c["id"],
            name=c["name"],
            campaignType=c["campaign_type"],
            channel=c["channel"],
            status=c["status"],
            offer=c.get("offer") or {},
            body=c.get("body"),
            templatePurpose=c.get("template_purpose"),
            version=c["version"],
            createdBy=c["created_by"],
            approvedBy=c.get("approved_by"),
            approvedAt=c.get("approved_at"),
            scheduledAt=c.get("scheduled_at"),
            startedAt=c.get("started_at"),
            completedAt=c.get("completed_at"),
            cancelledAt=c.get("cancelled_at"),
            outcomeReason=c.get("outcome_reason"),
            audienceSize=sum(v for k, v in n.items() if k != "reasons"),
            sent=n.get("sent", 0),
            skipped=n.get("skipped", 0),
            failed=n.get("failed", 0),
            blocked=n.get("blocked", 0),
            pending=n.get("pending", 0),
            skippedBy=n.get("reasons") or {},
            optedOut=(n.get("reasons") or {}).get("MARKETING_OPT_OUT", 0),
            creditsCharged=money.get("credits"),
            providerCostMicros=money.get("cost"),
            createdAt=c["created_at"],
        ))
    return out


@router.post("/properties/{property_id}/campaigns/{campaign_id}/cancel",
             response_model=CampaignOut)
@limiter.limit("30/minute")
async def cancel_campaign_endpoint(
    request: Request,
    property_id: str,
    campaign_id: str,
    member=Depends(require_member("marketing")),
):
    """
    Stop a campaign, including one that is part-way through.

    The running worker reloads the campaign before every recipient, so this
    takes effect within one message rather than at the end of the batch. It
    cannot un-send what has already gone, and the response says how many that
    was.
    """
    from app.services.marketing import campaigns as campaign_module

    try:
        await campaign_module.cancel(campaign_id, property_id, f"cancelled by {member.display_name}")
    except campaign_module.CampaignRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "detail": exc.detail}
        ) from exc

    campaigns = await list_campaigns(request, property_id, limit=200, member=member)
    for c in campaigns:
        if str(c.id) == str(campaign_id):
            return c
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")


@router.get("/properties/{property_id}/agent-actions", response_model=list[AgentActionOut])
@limiter.limit("120/minute")
async def list_agent_actions(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(60, ge=1, le=200),
    member=Depends(require_member()),
):
    def _read():
        db = get_db()
        q = (
            db.table("agent_actions").select("*")
            .eq("property_id", property_id)
            .order("created_at", desc=True).limit(limit)
        )
        if status_filter:
            q = q.eq("status", status_filter)
        rows = q.execute().data or []

        run_ids = {r["run_id"] for r in rows}
        runs = {
            r["id"]: r for r in (
                db.table("agent_runs").select("id,conversation_id")
                .in_("id", sorted(run_ids)).execute().data or []
            )
        } if run_ids else {}

        conv_ids = {r.get("conversation_id") for r in runs.values() if r.get("conversation_id")}
        convs = {
            c["id"]: c for c in (
                db.table("conversations").select("id,guest_name,guest_phone")
                .in_("id", sorted(conv_ids)).execute().data or []
            )
        } if conv_ids else {}

        # What each action cost, from the ledger rather than from a rate times a
        # guess. Keyed on `action_id`, which `usage.record_run` sets for the
        # per-tool events — so a read shows 0 because reads are priced at 0, not
        # because nobody looked.
        credits: dict[str, float] = {}
        if run_ids:
            for e in (db.table("usage_events").select("action_id,credits_charged")
                      .in_("run_id", sorted(run_ids)).limit(1000).execute().data or []):
                if e.get("action_id"):
                    credits[e["action_id"]] = (
                        credits.get(e["action_id"], 0.0) + float(e.get("credits_charged") or 0)
                    )
        return rows, runs, convs, credits

    rows, runs, convs, credits = await run_db(_read)
    out = []
    for r in rows:
        run = runs.get(r["run_id"]) or {}
        conv = convs.get(run.get("conversation_id")) or {}
        # The output is prose for a human to skim, not the raw payload. A
        # refusal shows its reason, which is the thing a manager reviewing the
        # queue actually needs.
        summary = r.get("refusal_reason") or ""
        if not summary and r.get("output"):
            summary = json.dumps(r["output"], default=str)[:400]
        out.append(AgentActionOut(
            id=r["id"], agentId=r["agent_id"], runId=r["run_id"],
            conversationId=run.get("conversation_id"),
            tool=r["tool"], input=r.get("input") or {}, output=summary,
            refusalReason=r.get("refusal_reason"),
            resultType=r.get("result_type"), resultId=r.get("result_id"),
            status=r["status"], createdAt=r["created_at"],
            latencyMs=r.get("latency_ms") or 0,
            creditsCharged=round(credits.get(r["id"], 0.0)),
            guestName=conv.get("guest_name") or conv.get("guest_phone"),
            approvedBy=r.get("approved_by"), approvedAt=r.get("approved_at"),
        ))
    return out


@router.post(
    "/properties/{property_id}/agent-actions/{action_id}/decision",
    response_model=AgentActionOut,
)
@limiter.limit("60/minute")
async def decide_agent_action(
    request: Request,
    property_id: str,
    action_id: str,
    body: AgentDecisionRequest,
    member=Depends(require_member("agents")),
):
    """
    Approve or reject a proposed action — and, on approval, run it.

    Recorded server-side with who decided and when: a decision that only existed
    in a browser is not a decision anyone can audit, and this row is what an
    owner reads six months later when they ask why a discount went out.

    **Approving executes, after revalidating.** A proposal and its approval are
    separated by however long a manager takes to look at their phone, and in
    that time a room sells or a quote expires. `services/ai/approvals` re-derives
    every check the runtime made and adds the tool's own staleness test, then
    runs it. A stale proposal comes back as a 409 naming what changed, and the
    action is terminal — the recovery is a fresh proposal, not another press.
    """
    if body.decision == "rejected":
        def _reject():
            db = get_db()
            current = (
                db.table("agent_actions").select("*")
                .eq("id", action_id).eq("property_id", property_id)
                .limit(1).execute().data or []
            )
            if not current:
                return None, "not_found"
            if current[0]["status"] != "awaiting_approval":
                return current[0], "already_decided"
            rows = db.table("agent_actions").update({
                "status": "rejected",
                "refusal_reason": body.note or "Rejected by a manager",
                "approved_by": member.profile_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", action_id).execute().data
            return (rows[0] if rows else None), None

        row, problem = await run_db(_reject)
        if problem == "not_found" or row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
        if problem == "already_decided":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This action is already {row['status'].replace('_', ' ')}",
            )
    else:
        result = await approvals.execute_approved(
            action_id=action_id, property_id=property_id, member=member
        )
        if not result.ok:
            if result.code == "ACTION_NOT_FOUND":
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
            # 409, not 500. The request was fine; the hotel moved. The code and
            # the detail both go back so the desk is told what changed rather
            # than that something went wrong.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": result.code, "detail": result.detail},
            )

        found = await run_db(_load_action, action_id, property_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
        row = found

    return AgentActionOut(
        id=row["id"], agentId=row["agent_id"], runId=row["run_id"],
        tool=row["tool"], input=row.get("input") or {},
        output=row.get("refusal_reason") or "",
        refusalReason=row.get("refusal_reason"),
        resultType=row.get("result_type"), resultId=row.get("result_id"),
        status=row["status"], createdAt=row["created_at"],
        latencyMs=row.get("latency_ms") or 0,
        approvedBy=row.get("approved_by"), approvedAt=row.get("approved_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Usage — measurement, not billing
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/usage", response_model=list[UsageMeterOut])
@limiter.limit("60/minute")
async def get_usage_meters(
    request: Request,
    property_id: str,
    days: int = Query(30, ge=1, le=365),
    member=Depends(require_member("reports")),
):
    """
    What this property has consumed, in units a hotel recognises.

    Grouped by usage type and labelled from `usage_types` — "AI conversation
    turns", "WhatsApp messages" — never tokens. Tokens are our cost problem, and
    a hotelier should not have to learn a new unit to read their own bill.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _read():
        db = get_db()
        events = (
            db.table("usage_events").select("usage_type,quantity")
            .eq("property_id", property_id).gte("created_at", since)
            .limit(5000).execute().data or []
        )
        types = {t["code"]: t for t in (db.table("usage_types").select("*").execute().data or [])}
        return events, types

    events, types = await run_db(_read)
    totals: dict[str, float] = {}
    for e in events:
        totals[e["usage_type"]] = totals.get(e["usage_type"], 0) + float(e["quantity"])

    return [_meter(code, used, types.get(code) or {}) for code, used in sorted(totals.items())]


def _meter(code: str, used: float, spec: dict) -> UsageMeterOut:
    return UsageMeterOut(
        code=code,
        label=spec.get("name") or code,
        unit=spec.get("unit") or "unit",
        used=used,
        billable=bool(spec.get("billable", True)),
        group=spec.get("display_group") or "Usage",
        category=spec.get("category") or "platform",
    )


@router.get("/properties/{property_id}/billing", response_model=BillingSummaryOut)
@limiter.limit("60/minute")
async def get_billing_summary(
    request: Request,
    property_id: str,
    member=Depends(require_member("billing")),
):
    """
    The usage meters, and how much room is left in them.

    **Usage against an allowance, never a credit count.** The hotel sees "AI
    Usage 1,240 / 2,000, resets 1 September"; the credits underneath are an
    internal monetization abstraction and surface only in the purchase flow and
    the ledger export.

    The period is the subscription's, not a rolling 30 days, because "resets on
    the 1st" is a promise and a rolling window is not one.

    Nothing here is a gate. It is what the banner reads; the decision to stop an
    AI reply is taken in the pipeline, against the same wallet, before a run
    opens.
    """
    account = await billing.account_for_property(property_id)
    if not account:
        return BillingSummaryOut(tier="unknown")

    def _read():
        db = get_db()
        sub = (
            db.table("subscriptions")
            .select("current_period_start,current_period_end,"
                    "subscription_plans(code,name,included_allowances)")
            .eq("billing_account_id", account)
            .in_("status", ["trialing", "active"])
            .order("current_period_start", desc=True)
            .limit(1).execute().data or []
        )
        types = {t["code"]: t for t in (db.table("usage_types").select("*").execute().data or [])}
        return (sub[0] if sub else None), types

    subscription, types = await run_db(_read)
    state = await billing.wallet_state(account)

    period_start = _as_date(subscription and subscription.get("current_period_start"))
    period_end = _as_date(subscription and subscription.get("current_period_end"))
    plan = ((subscription or {}).get("subscription_plans") or {})
    allowances = plan.get("included_allowances") or {}

    since = (period_start.isoformat() if period_start
             else (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat())

    def _events():
        return (
            get_db().table("usage_events").select("usage_type,quantity")
            .eq("property_id", property_id).gte("created_at", since)
            .neq("status", "reversed")
            .limit(10000).execute().data or []
        )

    events = await run_db(_events)
    totals: dict[str, float] = {}
    for e in events:
        totals[e["usage_type"]] = totals.get(e["usage_type"], 0) + float(e["quantity"])

    # Grouped by what the hotel recognises, which is `display_group`. The
    # allowance is keyed by category, because an allowance is a commercial
    # promise about a family of things and the display grouping is a label.
    groups: dict[str, UsageGroupOut] = {}
    for code, used in sorted(totals.items()):
        spec = types.get(code) or {}
        if not spec.get("billable", True):
            continue
        meter = _meter(code, used, spec)
        group = groups.get(meter.group)
        if group is None:
            group = UsageGroupOut(
                group=meter.group, category=meter.category, used=0,
                allowance=_as_float(allowances.get(meter.category)), items=[],
            )
            groups[meter.group] = group
        group.used += used
        group.items.append(meter)

    return BillingSummaryOut(
        plan=plan.get("name") or plan.get("code"),
        period_start=period_start,
        period_end=period_end,
        tier=str(state.get("tier") or "unknown"),
        balance=int(state.get("balance") or 0),
        available=int(state.get("available") or 0),
        groups=list(groups.values()),
    )


@router.get("/properties/{property_id}/usage/events", response_model=list[UsageEventOut])
@limiter.limit("60/minute")
async def list_usage_events(
    request: Request,
    property_id: str,
    limit: int = Query(100, ge=1, le=500),
    member=Depends(require_member("reports")),
):
    """
    The line items behind the meters.

    What was consumed, when, by which agent, and what it was charged. **Not
    what it cost us** — `provider_cost_inr_micros` is not granted to the
    browser role at all, and it is not selected here either. That column is the
    margin on this hotel's account, and printing it beside their own usage
    would be a pricing disclosure rather than transparency.
    """
    def _read():
        db = get_db()
        rows = (
            db.table("usage_events")
            .select("id,usage_type,quantity,credits_charged,provider,model,"
                    "agent_id,conversation_id,status,created_at")
            .eq("property_id", property_id)
            .order("created_at", desc=True)
            .limit(limit).execute().data or []
        )
        types = {t["code"]: t for t in (db.table("usage_types").select("*").execute().data or [])}
        agents = {
            a["id"]: a for a in
            (db.table("agents").select("id,name,agent_type")
             .eq("property_id", property_id).execute().data or [])
        }
        return rows, types, agents

    rows, types, agents = await run_db(_read)
    return [
        UsageEventOut(
            id=r["id"],
            usageType=r["usage_type"],
            displayName=(types.get(r["usage_type"]) or {}).get("name") or r["usage_type"],
            group=(types.get(r["usage_type"]) or {}).get("display_group") or "Usage",
            agentName=(agents.get(r.get("agent_id")) or {}).get("name"),
            conversationId=r.get("conversation_id"),
            units=float(r.get("quantity") or 0),
            creditsCharged=int(r.get("credits_charged") or 0),
            provider=r.get("provider"),
            model=r.get("model"),
            status=r.get("status") or "charged",
            createdAt=r["created_at"],
        )
        for r in rows
    ]


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
