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

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.api.v1.pms.schemas import (
    AgentActionOut,
    AgentAutonomyRequest,
    AgentDecisionRequest,
    AgentOut,
    AnalyticsReportOut,
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
    DailyPointOut,
    GuestOut,
    FolioEntryOut,
    MarginRowOut,
    MemberOut,
    ServiceOut,
    ServiceUpsertRequest,
    GuestRegistrationOut,
    PublicStayOut,
    WebCheckInRequest,
    CreditOrderOut,
    CreditOrderRequest,
    CreditPurchaseOut,
    PaymentOrderOut,
    PublicAvailabilityOut,
    PublicBookingOut,
    PublicConfirmRequest,
    PublicOrderOut,
    PublicOrderRequest,
    PublicQuoteOut,
    PublicQuoteRequest,
    PublicRoomOptionOut,
    PublicServiceOut,
    PublicSiteOut,
    QuotedAddonOut,
    WebsiteConfigOut,
    WebsiteUpdateRequest,
    FolioOut,
    FolioPostRequest,
    InventoryEditRequest,
    InventoryNightOut,
    MeOut,
    MessageOut,
    NightAuditOut,
    NightAuditRequest,
    NightlyOut,
    OwnerAskOut,
    OwnerAskRequest,
    OwnerOverviewOut,
    PropertyOut,
    QuoteOut,
    QuoteRequest,
    RateEditRequest,
    RateNightOut,
    RatePlanOptionOut,
    RatePlanOut,
    RecommendationOut,
    RecommendationStatusRequest,
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
from app.deps.property import ROLE_PERMS, require_member
from app.services import billing
from app.services.pms import ari as ari_service
from app.services.pms import availability as availability_service
from app.services.pms import clock
from app.services.pms import booking as booking_service
from app.services.pms import frontdesk as frontdesk_service
from app.services.pms import payments as payments_service
from app.services.pms import public_booking
from app.services.pms import sync as sync_service
from app.services.pms import website as website_service
from app.services import razorpay
from app.services.ai import approvals
from app.services.ai.errors import AgentError
from app.services.ai import console as ai_console
from app.services.ai import model_router
from app.services.messaging import service as messaging_service
from app.services.messaging.channels import MessagingError
from app.services.pms import workspace as workspace_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pms", tags=["pms"])

# What one credit is worth in subscription revenue, in paise.
# ₹2,499/month against a 3,000-credit grant — the anchoring stated in
# migration 13. Named here so the margin route is the only place that
# prices a credit, and never the client.
CREDIT_VALUE_PAISE = 83

# What a hotel pays for one credit, in paise. ₹1.00 — above the ₹0.83 a
# credit is worth inside a subscription, because a top-up is bought outside
# an allowance somebody has already paid for. The one place this conversion
# happens; a rupee amount never arrives from a client.
CREDIT_PRICE_PAISE = 100


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


# ── owner copilot and analytics ──────────────────────────────────────────────
#
# Two surfaces, and the order matters: the **structured** endpoints come first
# and the AI sits on top of them. `/analytics/*` answers without a model at all,
# so the Reports page and the owner's dashboard keep working when Vertex is
# down, when the wallet is empty, and when a hotel has turned its agents off.
# `/owner/ask` adds prose and recommendations to numbers that were already
# there. Building it the other way round — a screen whose only source is an
# agent's answer — is how analytics become an AI outage.

def _period_args(period: str, date_from: date | None, date_to: date | None):
    """Turn the query string into what `analytics` accepts, or 400."""
    from app.services.pms import analytics

    if period not in analytics.PERIOD_KEYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown period '{period}'. One of: {', '.join(analytics.PERIOD_KEYS)}.",
        )
    if period == "custom" and not (date_from and date_to):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A custom period needs both 'from' and 'to'.",
        )
    return period, date_from, date_to


async def _report_or_400(property_id: str, period: str,
                         date_from: date | None, date_to: date | None) -> dict:
    from app.services.pms import analytics

    key, start, end = _period_args(period, date_from, date_to)
    try:
        return await analytics.report(property_id, key=key, date_from=start, date_to=end)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/properties/{property_id}/analytics", response_model=AnalyticsReportOut)
@limiter.limit("60/minute")
async def get_analytics(
    request: Request,
    property_id: str,
    period: str = Query("today"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    member=Depends(require_member("reports")),
):
    """
    The property's own figures for a period, with the comparable period beside
    them.

    `require_member("reports")` and nothing else decides who may read this. A
    receptionist cannot, an accounts user can, and a property id that is not
    theirs 404s in the dependency before this body runs — which is why no query
    parameter here names a property, a partner or an account.

    No model is involved. Every number comes from `services/pms/analytics`, the
    same function `/today` calls, so this endpoint and the Today screen cannot
    report different revenue for the same day.
    """
    return await _report_or_400(property_id, period, date_from, date_to)


@router.get("/properties/{property_id}/analytics/series", response_model=list[DailyPointOut])
@limiter.limit("60/minute")
async def get_analytics_series(
    request: Request,
    property_id: str,
    days: int = Query(30, ge=1, le=365),
    member=Depends(require_member("reports")),
):
    """Occupancy, ADR and RevPAR per night, ending on the property's today."""
    from app.services.pms import analytics

    return await analytics.day_series(property_id, days)


@router.get("/properties/{property_id}/recommendations",
            response_model=list[RecommendationOut])
@limiter.limit("60/minute")
async def list_recommendations(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(40, ge=1, le=200),
    member=Depends(require_member("reports")),
):
    from app.services.pms import recommendations as recs

    if status_filter and status_filter not in recs.STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Unknown status. One of: {', '.join(recs.STATUSES)}.")
    rows = await recs.list_for(property_id, status=status_filter, limit=limit)
    return [recs.project(r) for r in rows]


@router.post("/properties/{property_id}/recommendations/{recommendation_id}/status",
             response_model=RecommendationOut)
@limiter.limit("30/minute")
async def set_recommendation_status(
    request: Request,
    property_id: str,
    recommendation_id: str,
    body: RecommendationStatusRequest,
    member=Depends(require_member("reports")),
):
    """
    Mark a recommendation reviewed, dismissed or actioned.

    **'actioned' means a person says they did it.** Nothing here changes a rate,
    an allocation or a stop-sell, and no worker reads these rows looking for
    work. When an execution path exists it will run through the same approval
    and revalidation the other agents' writes already use; this endpoint will
    still only be moving a label.
    """
    from app.services.pms import recommendations as recs

    row = await recs.set_status(property_id, recommendation_id, body.status,
                                profile_id=member.profile_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found")
    return recs.project(row)


@router.get("/properties/{property_id}/owner/overview", response_model=OwnerOverviewOut)
@limiter.limit("60/minute")
async def get_owner_overview(
    request: Request,
    property_id: str,
    period: str = Query("last_7"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    member=Depends(require_member("reports")),
):
    """
    Everything the owner's screen shows before anybody asks a question.

    Today and a chosen period, because they answer different questions — "how is
    tonight" and "how is the business" — and an owner opening the app wants both
    without picking one first.
    """
    from app.services.pms import clock
    from app.services.pms import recommendations as recs

    today_report = await _report_or_400(property_id, "today", None, None)
    period_report = await _report_or_400(property_id, period, date_from, date_to)
    rows = await recs.list_for(property_id, limit=20)

    def _name():
        found = (
            get_db().table("properties").select("name")
            .eq("id", property_id).limit(1).execute().data or []
        )
        return found[0]["name"] if found else None

    return OwnerOverviewOut(
        propertyId=property_id,
        propertyName=await run_db(_name),
        businessDate=await clock.abusiness_date_iso(property_id),
        timezone=await clock.atimezone_for(property_id),
        currency=today_report["currency"],
        today=today_report,
        period=period_report,
        recommendations=[recs.project(r) for r in rows],
    )


def _run_credits(run_id: str) -> int:
    """What one run cost the hotel, from the ledger's own figure."""
    rows = (
        get_db().table("usage_events").select("credits_charged")
        .eq("run_id", run_id).limit(50).execute().data or []
    )
    return round(sum(float(r.get("credits_charged") or 0) for r in rows))


def _metrics_from_actions(actions) -> tuple[list[dict], list[dict], dict | None, dict | None]:
    """
    The metrics the model was actually shown, lifted back out of its tool calls.

    Read from the outcomes rather than recomputed, deliberately: the cards under
    an answer must be the figures that answer was reasoned from. Recomputing
    here would let the prose and the cards drift apart in exactly the window
    that matters — a check-in between the tool call and the response.
    """
    metrics: list[dict] = []
    signals: list[dict] = []
    period = comparison = None
    for action in actions:
        out = action.output
        if not isinstance(out, dict):
            continue
        if out.get("metrics"):
            metrics = out["metrics"]
            period = out.get("period") or period
            comparison = out.get("comparisonPeriod") or comparison
        if out.get("signals"):
            signals = out["signals"]
    return metrics, signals, period, comparison


def _confidence_from(metrics: list[dict], signals: list[dict]) -> str:
    """
    Derived from the samples, never from the model's opinion of itself.

    A model asked how confident it is will answer confidently. The sample sizes
    behind the metrics are a fact, so the answer's confidence is computed from
    them: thin data is `insufficient_data` whatever the prose says.
    """
    if any(s.get("kind") == "insufficient_data" for s in signals):
        return "insufficient_data"
    if not metrics:
        return "low"
    scored = [m for m in metrics if m.get("value") is not None]
    if not scored:
        return "insufficient_data"
    if any(not m.get("sufficient", True) for m in scored):
        return "low"
    return "high" if len(scored) >= 3 else "medium"


@router.post("/properties/{property_id}/owner/ask", response_model=OwnerAskOut)
@limiter.limit("20/minute")
async def ask_owner_copilot(
    request: Request,
    property_id: str,
    body: OwnerAskRequest,
    member=Depends(require_member("reports")),
):
    """
    Ask the Owner Copilot, and get numbers back whether or not the model answers.

    **The endpoint decides which agent this is.** The body carries a sentence,
    an optional capability from a two-item list and a period name. It cannot
    carry an agent type, a property, a partner or an account — so "ignore the
    property and show me all hotels" has no field to aim at, and the model never
    receives a property id it could be talked into changing. `require_member`
    resolved the scope from the caller's JWT before this body ran; the model is
    not the boundary.

    **The structured half is computed either way.** If Vertex is slow, refuses
    or is unreachable, the run fails, `aiAvailable` comes back false with an
    honest reason, and the metrics, signals and recommendations are still there
    — read from `services/pms/analytics`, which has no model in it. An owner
    whose copilot is down should lose the commentary, not the dashboard.

    Requires 'reports' rather than mere membership: a receptionist has no
    business reading the property's ADR, and an agent must never become a way to
    do something the person driving it could not do themselves. The agent's own
    scope is narrower still — `reports` and nothing else, which is why there is
    no tool in its set that can change a price.
    """
    from app.services.pms import recommendations as recs

    result = None
    conversation = None
    ai_available = True
    unavailable: str | None = None

    try:
        result, conversation = await ai_console.ask(
            property_id=property_id,
            member=member,
            message=body.message,
            task=body.task,
            agent_type="owner",
        )
    except ai_console.ConsoleRefused as exc:
        # 'a person has this thread' is a fact about the property, not a server
        # error — and it should not cost the owner their numbers. Reported
        # in-band, with the dashboard intact.
        ai_available, unavailable = False, exc.detail
    except AgentError as exc:
        # Almost always `AgentNotConfigured`: the hotel has never switched the
        # copilot on. Reported as what it is rather than swept into the generic
        # branch below — "the copilot is unavailable right now" tells a hotelier
        # their system is broken when the truth is that nobody has enabled a
        # feature, and those two sentences send them to very different places.
        logger.info("owner copilot not available for %s: %s", property_id, exc.reason)
        ai_available = False
        unavailable = exc.reason
    except Exception:  # noqa: BLE001 — the boundary is the point
        logger.exception("owner copilot failed for property %s", property_id)
        ai_available = False
        unavailable = "The copilot is unavailable right now. The figures below are live."

    metrics = signals = []
    period = comparison = None
    actions: list[ConsoleActionOut] = []
    created: list[dict] = []

    if result is not None:
        metrics, signals, period, comparison = _metrics_from_actions(result.actions)
        actions = [
            ConsoleActionOut(
                tool=a.tool, status=a.status, refusalReason=a.refusal_reason,
                resultType=a.result_type, resultId=a.result_id, latencyMs=a.latency_ms,
            )
            for a in result.actions
        ]
        ids = [a.result_id for a in result.actions
               if a.result_type == "recommendation" and a.result_id and a.ok]
        if ids:
            def _read_created():
                return (
                    get_db().table("revenue_recommendations").select("*")
                    .in_("id", ids).execute().data or []
                )
            created = await run_db(_read_created)
        if result.status == "failed":
            ai_available = False
            unavailable = (
                "The copilot could not complete that. The figures below are live."
            )

    # The fallback, and the reason this endpoint is worth having over the
    # console: an answer with no tool call behind it — because the model failed,
    # or because it replied from the conversation without looking — still comes
    # back with the property's real numbers rather than an empty card.
    if not metrics:
        report = await _report_or_400(property_id, body.period, None, None)
        metrics, signals = report["metrics"], report["signals"]
        period, comparison = report["period"], report["comparisonPeriod"]
        currency = report["currency"]
    else:
        currency = next((m.get("currency") for m in metrics if m.get("currency")), "INR")

    if period is None:
        report = await _report_or_400(property_id, body.period, None, None)
        period, comparison = report["period"], report["comparisonPeriod"]

    return OwnerAskOut(
        runId=result.run_id if result else None,
        conversationId=conversation["id"] if conversation else None,
        status=result.status if result else "unavailable",
        summary=result.reply if result else None,
        aiAvailable=ai_available,
        aiUnavailableReason=unavailable,
        period=period,
        comparisonPeriod=comparison,
        currency=currency or "INR",
        metrics=metrics,
        signals=signals,
        recommendations=[recs.project(r) for r in created],
        evidence=actions,
        confidence=_confidence_from(metrics, signals),
        creditsCharged=await run_db(_run_credits, result.run_id) if result else 0,
        tokensIn=result.tokens_in if result else 0,
        tokensOut=result.tokens_out if result else 0,
        latencyMs=result.latency_ms if result else 0,
    )


# ── guests, services, members, margin ────────────────────────────────────────
#
# The four reads the PWA had no route for. Nothing new is being modelled here:
# every table, its RLS and its write path have existed since M1, and these are
# the queries the screens were always going to make.

@router.get("/properties/{property_id}/guests", response_model=list[GuestOut])
@limiter.limit("120/minute")
async def list_guests(
    request: Request,
    property_id: str,
    search: str | None = Query(None, max_length=80),
    limit: int = Query(200, ge=1, le=500),
    member=Depends(require_member("frontdesk")),
):
    """
    The hotel's guest list.

    `require_member("frontdesk")` rather than bare membership: a guest list is
    names, phone numbers and stay history, and a housekeeper has no reason to
    hold it. Accounts does not either — they read folios, not people.

    Search escapes `%` and `_` before it reaches `ilike`, for the same reason
    `plan_coupons` does: a Postgres wildcard typed into a search box should
    match a literal wildcard, not every guest in the building.
    """
    def _read():
        q = (
            get_db().table("guests")
            .select("id,property_id,name,phone,email,city,stays_count,"
                    "total_spend_paise,last_stay_at,vip,notes,marketing_opt_in")
            .eq("property_id", property_id)
        )
        if search:
            escaped = search.replace("%", r"\%").replace("_", r"\_").replace(",", " ")
            q = q.or_(f"name.ilike.%{escaped}%,phone.ilike.%{escaped}%,"
                      f"email.ilike.%{escaped}%")
        return q.order("last_stay_at", desc=True).limit(limit).execute().data or []

    return [GuestOut.from_row(r) for r in await run_db(_read)]


@router.get("/properties/{property_id}/services", response_model=list[ServiceOut])
@limiter.limit("120/minute")
async def list_services(
    request: Request,
    property_id: str,
    include_inactive: bool = Query(False),
    member=Depends(require_member()),
):
    """
    The paid extras this hotel sells.

    Any member may read them — a receptionist quoting a transfer needs the
    price, and the Guest agent already reads the same rows through
    `list_services`. Editing is a different permission; see below.

    Inactive services are excluded by default and available on request: the
    desk should not be able to sell something the hotel retired, but the
    settings screen has to show it in order to bring it back.
    """
    def _read():
        q = (
            get_db().table("services")
            .select("*").eq("property_id", property_id)
        )
        if not include_inactive:
            q = q.eq("is_active", True)
        return q.order("sort_order").limit(200).execute().data or []

    return [ServiceOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/services", response_model=ServiceOut)
@limiter.limit("60/minute")
async def upsert_service(
    request: Request,
    property_id: str,
    body: ServiceUpsertRequest,
    member=Depends(require_member("rates")),
):
    """
    Create or amend one service.

    `require_member("rates")` because this sets a price. A receptionist may sell
    a transfer and may not decide what it costs — the same line the rate
    calendar draws, and the same one the Sales agent's discount ceiling sits
    behind.

    The property comes from the path and is checked by the dependency; `id`
    from the body is scoped to it on update, so a service id belonging to
    another hotel updates nothing rather than being moved.
    """
    payload = {
        "property_id": property_id,
        "partner_id": member.partner_id,
        "name": body.name,
        "description": body.description or None,
        "price_paise": body.pricePaise,
        "category": body.category,
        "is_active": body.isActive,
        "daily_capacity": body.dailyCapacity,
        "lead_time_hours": body.leadTimeHours,
    }

    def _write():
        db = get_db()
        if body.id:
            rows = (
                db.table("services").update(payload)
                .eq("id", body.id).eq("property_id", property_id)
                .execute().data
            )
            return rows[0] if rows else None
        return db.table("services").insert(payload).execute().data[0]

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    return ServiceOut.from_row(row)


@router.get("/properties/{property_id}/members", response_model=list[MemberOut])
@limiter.limit("60/minute")
async def list_members(
    request: Request,
    property_id: str,
    member=Depends(require_member("members")),
):
    """
    Who works here, and what each of them may do.

    Scoped to the property's partner and filtered by `property_ids`, so a
    manager of one hotel in a group does not get the group's staff list. The
    permission set is flattened from `ROLE_PERMS` plus per-member grants — the
    same derivation `/me` does, in the same place, rather than a second table
    the screen would have to interpret.
    """
    def _read():
        db = get_db()
        rows = (
            db.table("partner_members")
            .select("id,profile_id,role,property_ids,permissions,status")
            .eq("partner_id", member.partner_id)
            .order("role")
            .limit(200).execute().data or []
        )
        rows = [
            r for r in rows
            if not (r.get("property_ids") or []) or property_id in r["property_ids"]
        ]
        if not rows:
            return []
        profiles = {
            p["id"]: p for p in
            (db.table("profiles").select("id,email,full_name")
             .in_("id", [r["profile_id"] for r in rows]).execute().data or [])
        }
        return [(r, profiles.get(r["profile_id"], {})) for r in rows]

    out = []
    for row, profile in await run_db(_read):
        extra = {k for k, v in (row.get("permissions") or {}).items() if v is True}
        perms = ROLE_PERMS.get(row["role"], set()) | extra
        out.append(MemberOut(
            id=row["id"],
            name=profile.get("full_name") or profile.get("email") or "Member",
            email=profile.get("email") or "",
            role=row["role"],
            propertyIds=row.get("property_ids") or [],
            status=row.get("status") or "active",
            # An owner's '*' is expanded to nothing useful for a screen, so it
            # is passed through as the single token it is.
            permissions=sorted(perms),
        ))
    return out


@router.get("/properties/{property_id}/margin", response_model=list[MarginRowOut])
@limiter.limit("30/minute")
async def get_margin_rows(
    request: Request,
    property_id: str,
    days: int = Query(30, ge=1, le=365),
    member=Depends(require_member("billing")),
):
    """
    What each property cost us against what it paid — the internal margin view.

    **This is the one route in the PMS that returns provider cost.** Migration
    12 revokes `usage_events.provider_cost_inr_micros` from browser roles precisely so
    this number cannot be read from the client; it is served here, behind
    `require_member("billing")`, through the service-role client.

    Computed from `usage_events` rather than from `billing_period_summary`,
    deliberately: the summary is a materialised period and this screen is asked
    "how are we doing right now". The definitions are the same — credits
    charged is the revenue side, `cost_inr_micros` is the cost side, and neither
    is derived from the other.

    Scoped to the caller's partner. A margin screen that showed every hotel on
    the platform would be an operator tool wearing a hotelier's login.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _read():
        db = get_db()
        properties = (
            db.table("properties").select("id,name,city")
            .eq("partner_id", member.partner_id).execute().data or []
        )
        if not properties:
            return []
        ids = [p["id"] for p in properties]
        events = (
            db.table("usage_events")
            .select("property_id,usage_type,credits_charged,provider_cost_inr_micros")
            .in_("property_id", ids)
            .gte("created_at", since)
            .limit(20000).execute().data or []
        )
        return properties, events

    loaded = await run_db(_read)
    if not loaded:
        return []
    properties, events = loaded

    totals: dict[str, dict] = {}
    for e in events:
        slot = totals.setdefault(e["property_id"], {"credits": 0.0, "cost": 0, "by_type": {}})
        credits = float(e.get("credits_charged") or 0)
        slot["credits"] += credits
        slot["cost"] += int(e.get("provider_cost_inr_micros") or 0)
        slot["by_type"][e["usage_type"]] = slot["by_type"].get(e["usage_type"], 0.0) + credits

    out = []
    for p in properties:
        t = totals.get(p["id"], {"credits": 0.0, "cost": 0, "by_type": {}})
        # One credit is worth roughly ₹0.83 of subscription revenue — the
        # anchoring in migration 13. Stated once, here, rather than in the
        # client: a browser that prices a credit is a browser that can disagree
        # with the invoice.
        revenue_paise = round(t["credits"] * CREDIT_VALUE_PAISE)
        cost_paise = round(t["cost"] / 10_000)  # 1e-6 INR → paise
        top = max(t["by_type"].items(), key=lambda kv: kv[1], default=("", 0))[0]
        out.append(MarginRowOut(
            propertyId=p["id"],
            propertyName=p["name"],
            city=p.get("city") or "",
            plan="",
            usageCredits=round(t["credits"]),
            providerCostInrMicros=t["cost"],
            revenuePaise=revenue_paise,
            marginPaise=revenue_paise - cost_paise,
            topUsageType=top,
        ))
    return sorted(out, key=lambda r: -r.usageCredits)


# ── web check-in — the only unauthenticated routes in this file ──────────────
#
# Everything above resolves a member from a JWT before the handler runs. These
# three do not, because the person using them is a guest on their own phone who
# has never had an account and never will.
#
# **The booking reference is the credential.** That is a weaker control than a
# token and it is the one the product has, so the design compensates rather than
# pretends: the read is masked to what a guest needs to recognise their own
# booking, the write cannot reach anything but its own reservation, the rate
# limit is tight enough that guessing `TB-` plus six characters is not
# practical, and a wrong reference is answered identically to a right one that
# has been cancelled — 404, with no hint about which.

def _mask_phone(phone: str | None) -> str:
    """Last four digits. Enough to recognise, useless if leaked."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"••••{digits[-4:]}" if len(digits) >= 4 else ""


def _load_public_stay(ref: str) -> dict | None:
    """
    One reservation by reference, with the names a guest would recognise.

    Reads through the service-role client because the caller has no identity to
    check RLS against — which is exactly why nothing here is selected that a
    guest should not see. The projection *is* the access control.
    """
    db = get_db()
    rows = (
        db.table("reservations")
        .select("id,ref,property_id,partner_id,guest_name,guest_phone,check_in,"
                "check_out,nights,units,adults,children,total_paise,paid_paise,"
                "status,room_type_id,rate_plan_id")
        .ilike("ref", ref.strip())
        .limit(1).execute().data or []
    )
    if not rows:
        return None
    stay = rows[0]

    prop = (
        db.table("properties")
        .select("name,city,check_in_time,check_out_time")
        .eq("id", stay["property_id"]).limit(1).execute().data or [{}]
    )[0]
    room_type = (
        db.table("room_types").select("name")
        .eq("id", stay["room_type_id"]).limit(1).execute().data or [{}]
    )[0]
    rate_plan = (
        db.table("rate_plans").select("name,inclusions")
        .eq("id", stay["rate_plan_id"]).limit(1).execute().data or [{}]
    )[0]
    balance = db.rpc("folio_balance", {"p_reservation": stay["id"]}).execute().data
    registration = (
        db.table("guest_registrations").select("*")
        .eq("reservation_id", stay["id"]).limit(1).execute().data or []
    )
    return {
        "stay": stay, "property": prop, "room_type": room_type,
        "rate_plan": rate_plan, "balance": int(balance or 0),
        "registration": registration[0] if registration else None,
    }


@router.get("/public/stays/{ref}", response_model=PublicStayOut)
@limiter.limit("10/minute")
async def get_public_stay(request: Request, ref: str):
    """
    A guest's own booking, by the reference on their confirmation.

    No authentication, so the rate limit is the control that matters: ten
    attempts a minute against a six-character reference space is not a
    practical enumeration, and a real guest never needs an eleventh.

    A cancelled stay 404s alongside a reference that never existed. Telling a
    stranger "that booking exists but is cancelled" is a free bit of information
    about somebody else's travel.
    """
    loaded = await run_db(_load_public_stay, ref)
    if not loaded or loaded["stay"]["status"] in ("cancelled", "no_show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that booking.")

    stay = loaded["stay"]
    return PublicStayOut(
        ref=stay["ref"],
        propertyName=loaded["property"].get("name") or "",
        propertyCity=loaded["property"].get("city") or "",
        checkInTime=str(loaded["property"].get("check_in_time") or "")[:5],
        checkOutTime=str(loaded["property"].get("check_out_time") or "")[:5],
        guestName=stay.get("guest_name") or "Guest",
        guestPhoneMasked=_mask_phone(stay.get("guest_phone")),
        checkIn=stay["check_in"], checkOut=stay["check_out"],
        nights=stay["nights"], units=stay["units"],
        adults=stay.get("adults") or 0, children=stay.get("children") or 0,
        roomTypeName=loaded["room_type"].get("name") or "",
        ratePlanName=loaded["rate_plan"].get("name") or "",
        inclusions=loaded["rate_plan"].get("inclusions") or [],
        totalPaise=stay.get("total_paise") or 0,
        paidPaise=stay.get("paid_paise") or 0,
        balancePaise=loaded["balance"],
        status=stay["status"],
        registration=(GuestRegistrationOut.from_row(loaded["registration"])
                      if loaded["registration"] else None),
    )


@router.post("/public/stays/{ref}/check-in", response_model=GuestRegistrationOut)
@limiter.limit("6/minute")
async def submit_web_check_in(request: Request, ref: str, body: WebCheckInRequest):
    """
    File a registration against one booking, before arrival.

    **This does not check anybody in.** It records what the guest told us; the
    desk still runs `check_in`, which is where a room is assigned, the folio is
    opened and the stay's status moves. Conflating the two would let a stranger
    with a reference mark a room occupied.

    The reservation comes from the reference in the path and from nothing in the
    body — there is no `reservationId` field for a caller to point somewhere
    else. Re-submitting amends the guest's own row rather than creating a
    second, which is what correcting a typo means.
    """
    loaded = await run_db(_load_public_stay, ref)
    if not loaded or loaded["stay"]["status"] in ("cancelled", "no_show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that booking.")

    stay = loaded["stay"]
    if stay["status"] == "checked_out":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This stay has already ended.")

    # The last four characters, and the rest is discarded here — before it
    # reaches the database, so there is no window in which it exists at rest.
    digits = "".join(c for c in body.idNumber if c.isalnum())
    payload = {
        "reservation_id": stay["id"],
        "property_id": stay["property_id"],
        "partner_id": stay["partner_id"],
        "full_name": body.fullName.strip(),
        "email": body.email.strip() or None,
        "phone": body.phone.strip() or None,
        "address": body.address.strip() or None,
        "city": body.city.strip() or None,
        "country": body.country.strip() or None,
        "id_type": body.idType,
        "id_last4": digits[-4:] if digits else None,
        "travelling_with": body.travellingWith,
        "estimated_arrival": body.estimatedArrival,
        "special_requests": (body.specialRequests or "").strip() or None,
        "source": "web",
    }

    def _write():
        return (
            get_db().table("guest_registrations")
            .upsert(payload, on_conflict="reservation_id")
            .execute().data[0]
        )

    return GuestRegistrationOut.from_row(await run_db(_write))


@router.get("/properties/{property_id}/reservations/{reservation_id}/registration",
            response_model=GuestRegistrationOut)
@limiter.limit("120/minute")
async def get_registration(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("frontdesk")),
):
    """
    What the guest filled in, for the desk that is about to meet them.

    Authenticated and property-scoped, unlike the two above. Even here it is
    `id_last4` and nothing more — the full document was never received, so there
    is no version of this route that could return it.
    """
    def _read():
        rows = (
            get_db().table("guest_registrations").select("*")
            .eq("reservation_id", reservation_id)
            .eq("property_id", property_id)
            .limit(1).execute().data or []
        )
        return rows[0] if rows else None

    row = await run_db(_read)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "This guest has not checked in online.")
    return GuestRegistrationOut.from_row(row)


# ═════════════════════════════════════════════════════════════════════════════
# M5 — the public booking loop
#
#     /stay/[slug] ──► availability ──► quote ──► order ──► Razorpay
#                                                             │
#                            reservation ◄── confirm ◄────────┤
#                                        ◄── webhook ◄────────┘
#
# **Every one of these composes the services the front desk already uses.**
# Availability is `availability.stay_options`, the price is
# `booking.create_quote`, the reservation is `booking.create_reservation` —
# which already validates the quote, enforces its expiry, converts the hold and
# refuses a second submission. Nothing in this section prices a stay, counts a
# room or decrements inventory, and if it ever appears to, that is a bug.
#
# What this section owns is the boundary: resolving a slug instead of an id,
# refusing anything the browser tried to decide, and making the four possible
# orderings of "browser returns" and "webhook fires" converge on one booking.
# ═════════════════════════════════════════════════════════════════════════════

def _room_option(option, units: int) -> PublicRoomOptionOut:
    """
    One room type as the public sees it.

    Rate plans that are closed or violate the minimum stay are dropped rather
    than shown greyed out: a guest cannot act on them, and a price they are not
    allowed to take is a price they will ask about.
    """
    return PublicRoomOptionOut(
        roomTypeId=option.room_type_id,
        name=option.room_type_name,
        code=option.room_type_code,
        maxOccupancy=option.max_occupancy,
        unitsLeft=option.units_left,
        ratePlans=[
            RatePlanOptionOut(
                ratePlanId=p.rate_plan_id, code=p.code, name=p.name,
                mealPlan=p.meal_plan, audience="public", refundable=p.refundable,
                inclusions=p.inclusions, totalPaise=p.total_paise,
                perNightPaise=p.per_night_paise,
                minStayViolation=p.min_stay_violation, closed=p.closed,
            )
            for p in option.rate_plans
            if not p.closed and p.min_stay_violation is None
        ],
    )


@router.get("/public/hotels/{slug}", response_model=PublicSiteOut)
@limiter.limit("60/minute")
async def get_public_hotel(request: Request, slug: str):
    """
    A published hotel's page.

    The content is the website's *published* snapshot, never its draft — a
    hotelier mid-edit is not publishing. The rooms and services come from the
    live PMS, so a new room type or a price change appears here with nothing to
    re-publish; the website holds no inventory of its own.
    """
    prop = await public_booking.resolve_published(slug)

    def _read():
        db = get_db()
        types = (
            db.table("room_types")
            .select("id,code,name,description,max_occupancy,bed_type,amenities")
            .eq("property_id", prop.id).eq("is_active", True)
            .order("sort_order").execute().data or []
        )
        services = (
            db.table("services")
            .select("id,name,description,category,price_paise,lead_time_hours,daily_capacity")
            .eq("property_id", prop.id).eq("is_active", True)
            .order("sort_order").limit(50).execute().data or []
        )
        return types, services

    types, services = await run_db(_read)
    return PublicSiteOut(
        slug=prop.slug, name=prop.name, city=prop.city, template=prop.template,
        currency=prop.currency, timezone=prop.timezone,
        checkInTime=prop.check_in_time, checkOutTime=prop.check_out_time,
        policies=prop.policies, content=prop.website,
        services=[
            PublicServiceOut(
                id=s["id"], name=s["name"], description=s.get("description") or "",
                category=s["category"], pricePaise=s["price_paise"],
                leadTimeHours=s.get("lead_time_hours") or 0,
                dailyCapacity=s.get("daily_capacity"),
            ) for s in services
        ],
        roomTypes=[
            PublicRoomOptionOut(
                roomTypeId=t["id"], name=t["name"], code=t["code"],
                description=t.get("description"),
                maxOccupancy=t.get("max_occupancy") or 2,
                bedType=t.get("bed_type"), amenities=t.get("amenities") or [],
                # Nothing is claimed about availability here. This is the
                # brochure; the number comes from /availability with dates.
                unitsLeft=0,
            ) for t in types
        ],
    )


@router.get("/public/hotels/{slug}/availability", response_model=PublicAvailabilityOut)
@limiter.limit("30/minute")
async def get_public_availability(
    request: Request,
    slug: str,
    check_in: date = Query(alias="from"),
    check_out: date = Query(alias="to"),
    units: int = Query(1, ge=1, le=5),
    adults: int = Query(2, ge=1, le=20),
    children: int = Query(0, ge=0, le=10),
):
    """
    Live availability, from the same function the front desk calls.

    `audience="public"` is the load-bearing argument: it is what stops a
    stranger being shown — or later quoted — the B2B contracted rate. The
    endpoint knows who is asking; the service does the filtering.
    """
    prop = await public_booking.resolve_published(slug)
    public_booking.validate_stay_dates(prop, check_in, check_out)

    options = await availability_service.stay_options(
        prop.id, check_in, check_out, units=units, audience="public",
    )
    rooms = [
        _room_option(o, units) for o in options
        if o.units_left >= units and o.max_occupancy >= adults + children
    ]
    rooms = [r for r in rooms if r.ratePlans]

    return PublicAvailabilityOut(
        checkIn=check_in, checkOut=check_out,
        nights=(check_out - check_in).days, units=units,
        currency=prop.currency, rooms=rooms, soldOut=not rooms,
    )


@router.post("/public/hotels/{slug}/quote", response_model=PublicQuoteOut)
@limiter.limit("20/minute")
async def create_public_quote(request: Request, slug: str, body: PublicQuoteRequest):
    """
    Price a stay and hold the rooms.

    `booking.create_quote` does all of it — the GST slab per night, the
    discount clamp, the token rule from `properties.settings`, the hold, the
    expiry. This endpoint supplies `audience="public"`, which is what makes a
    rate plan id the guest was never offered unusable even if they guess one.
    """
    prop = await public_booking.resolve_published(slug)
    public_booking.validate_stay_dates(prop, body.checkIn, body.checkOut)

    quote = await booking_service.create_quote(
        property_id=prop.id,
        rate_plan_id=body.ratePlanId,
        check_in=body.checkIn, check_out=body.checkOut,
        units=body.units, adults=body.adults, children=body.children,
        # No discount from a public caller. Negotiation is the Sales agent's,
        # behind an authenticated surface with a ceiling on it.
        requested_discount_pct=0,
        issued_by="booking_engine",
        audience="public",
    )

    def _names():
        db = get_db()
        rt = (db.table("room_types").select("name")
              .eq("id", quote.room_type_id).limit(1).execute().data or [{}])[0]
        rp = (db.table("rate_plans").select("name,inclusions")
              .eq("id", quote.rate_plan_id).limit(1).execute().data or [{}])[0]
        return rt, rp

    room_type, rate_plan = await run_db(_names)
    priced = quote.priced
    return PublicQuoteOut(
        quoteId=quote.id,
        checkIn=quote.check_in, checkOut=quote.check_out,
        nights=(quote.check_out - quote.check_in).days,
        units=priced.units,
        roomTypeName=room_type.get("name") or "",
        ratePlanName=rate_plan.get("name") or "",
        inclusions=rate_plan.get("inclusions") or [],
        grossPaise=priced.gross_paise, discountPaise=priced.discount_paise,
        taxPaise=priced.tax_paise, totalPaise=priced.total_paise,
        tokenPaise=priced.token_paise, balancePaise=priced.balance_paise,
        expiresAt=quote.expires_at, currency=prop.currency,
    )


def _quote_row(quote_id: str, property_id: str) -> dict | None:
    rows = (
        get_db().table("quotes").select("*")
        .eq("id", quote_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


@router.post("/public/hotels/{slug}/quotes/{quote_id}/order", response_model=PublicOrderOut)
@limiter.limit("15/minute")
async def create_public_order(
    request: Request, slug: str, quote_id: str, body: PublicOrderRequest,
):
    """
    Turn a held quote into a Razorpay order.

    **The amount is computed here and never received.** It is the quote's own
    token plus server-priced add-ons; the request carries a service id and a
    quantity and nothing else that touches money. A browser that posts a price
    is posting a field no model on this route has.

    The order row is written *before* the id reaches the browser, because the
    webhook can fire while Checkout is still opening and a webhook that finds no
    order has nothing to settle. The guest's details and their add-ons are
    stored on it for the same reason: the webhook has no browser to ask.
    """
    prop = await public_booking.resolve_published(slug)
    quote = await run_db(_quote_row, quote_id, prop.id)
    if not quote:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That price is no longer available.")
    if quote["status"] != "active":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This price has already been used. Please search again.")
    if datetime.fromisoformat(str(quote["expires_at"]).replace("Z", "+00:00")) \
            < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This price has expired. Please search again.")

    addons = await public_booking.price_addons(
        prop.id, [a.model_dump() for a in body.addons],
        check_in=date.fromisoformat(str(quote["check_in"])),
        check_out=date.fromisoformat(str(quote["check_out"])),
        timezone_name=prop.timezone,
    )
    addons_total = sum(a.total_paise for a in addons)
    amount = public_booking.checkout_amount_paise(
        quote["total_paise"], quote["token_paise"], addons_total,
    )
    if amount < 100:
        # Razorpay refuses anything under a rupee. A property on pay-at-hotel
        # with no extras has nothing to charge, and saying so is better than
        # inventing a one-rupee order.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This stay does not require an online payment. Please contact the hotel.",
        )

    if not razorpay.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Online payment is unavailable right now.")

    rzp = await razorpay.create_order(
        amount_paise=amount,
        receipt=f"stay-{quote_id[:8]}",
        notes={"quote": quote_id, "property": prop.slug},
    )

    order = await payments_service.create(
        razorpay_order_id=rzp["id"], purpose="booking",
        partner_id=prop.partner_id, property_id=prop.id,
        quote_id=quote_id, amount_paise=amount,
        guest_details=body.guest.model_dump(),
        addons=[a.as_dict() for a in addons],
        attribution={"source": body.source} if body.source else {},
    )

    return PublicOrderOut(
        orderId=order["id"], razorpayOrderId=rzp["id"],
        keyId=razorpay.public_key_id() or "",
        amountPaise=amount, quoteId=quote_id,
        addons=[a.as_dict() for a in addons],
        roomTokenPaise=min(quote["token_paise"], quote["total_paise"]),
        addonsTotalPaise=addons_total,
        balanceDuePaise=max(0, quote["total_paise"] - quote["token_paise"]),
        hotelName=prop.name, guestName=body.guest.fullName,
        guestEmail=body.guest.email, guestPhone=body.guest.phone,
    )


async def _fulfil_booking(order: dict) -> dict:
    """
    Create the reservation an order paid for. Called once per order, ever.

    The caller has already won `payments.claim`, so this runs for exactly one of
    the browser callback and the webhook. If it raises, the order is released
    back to 'created' and Razorpay's next webhook retry picks it up — a guest
    who has paid must never be left without a booking because a room sold in the
    seconds between.
    """
    guest = order.get("guest_details") or {}
    reservation = await booking_service.create_reservation(
        property_id=order["property_id"],
        quote_id=order["quote_id"],
        guest_name=guest.get("fullName") or "Guest",
        guest_phone=guest.get("phone"),
        guest_email=guest.get("email"),
        source="travbuddy" if (order.get("attribution") or {}).get("source") == "travbuddy"
               else "direct",
        # What was actually paid now. The rest is balance due on the folio,
        # which is what a folio is for.
        token_paise=order["amount_paise"],
        payment_method="gateway",
        notes=guest.get("notes"),
        created_by="booking_engine",
        member_id=None, member_name=None,
    )
    await public_booking.attach_addons(reservation, order.get("addons") or [])
    await payments_service.record_result(order["id"], reservation_id=reservation["id"])
    return reservation


async def _public_booking_out(reservation: dict, hotel_name: str) -> PublicBookingOut:
    def _read():
        db = get_db()
        rt = (db.table("room_types").select("name")
              .eq("id", reservation["room_type_id"]).limit(1).execute().data or [{}])[0]
        balance = db.rpc("folio_balance",
                         {"p_reservation": reservation["id"]}).execute().data
        return rt, int(balance or 0)

    room_type, balance = await run_db(_read)
    addons = await public_booking.list_for_reservation(reservation["id"])
    return PublicBookingOut(
        ref=reservation["ref"], status=reservation["status"], hotelName=hotel_name,
        checkIn=reservation["check_in"], checkOut=reservation["check_out"],
        nights=reservation["nights"], units=reservation["units"],
        roomTypeName=room_type.get("name") or "",
        guestName=reservation.get("guest_name") or "Guest",
        totalPaise=reservation.get("total_paise") or 0,
        paidPaise=reservation.get("paid_paise") or 0,
        balancePaise=balance,
        addons=[
            QuotedAddonOut(
                serviceId=a["service_id"], name=a["name"], quantity=a["quantity"],
                unitPricePaise=a["unit_price_paise"], totalPaise=a["total_paise"],
                serviceDate=a.get("service_date"),
            ) for a in addons
        ],
        checkInUrl=f"/checkin/{reservation['ref']}",
    )


@router.post("/public/hotels/{slug}/confirm", response_model=PublicBookingOut)
@limiter.limit("15/minute")
async def confirm_public_booking(
    request: Request, slug: str, body: PublicConfirmRequest,
):
    """
    The browser returning from Checkout. The fast path, not the reliable one.

    **Payment success is never inferred from the browser.** The signature is an
    HMAC over `order_id|payment_id` with our secret, so a client cannot forge
    one — and even so, this route does not decide anything the webhook could not
    decide alone. If the webhook won the race this returns the booking it made;
    if the browser wins, the webhook later finds `reservation_id` set and does
    nothing.
    """
    prop = await public_booking.resolve_published(slug)

    if not razorpay.verify_payment_signature(
        body.razorpayOrderId, body.razorpayPaymentId, body.razorpaySignature
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment could not be verified.")

    order = await payments_service.get(body.razorpayOrderId)
    if not order or order["purpose"] != "booking" or order["property_id"] != prop.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that payment.")

    settlement = await payments_service.claim(body.razorpayOrderId, body.razorpayPaymentId)
    if settlement.won:
        try:
            reservation = await _fulfil_booking(settlement.order)
        except HTTPException:
            await payments_service.release(settlement.order["id"], "fulfilment failed")
            raise
        except Exception as exc:  # noqa: BLE001
            await payments_service.release(settlement.order["id"], str(exc)[:200])
            logger.exception("could not fulfil paid order %s", body.razorpayOrderId)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Your payment went through and we could not finish the booking. "
                "The hotel has been notified.",
            ) from exc
        return await _public_booking_out(reservation, prop.name)

    # Somebody else settled it — almost always the webhook, arriving first.
    existing = settlement.order
    if existing.get("reservation_id"):
        def _read():
            rows = (get_db().table("reservations").select("*")
                    .eq("id", existing["reservation_id"]).limit(1).execute().data or [])
            return rows[0] if rows else None
        reservation = await run_db(_read)
        if reservation:
            return await _public_booking_out(reservation, prop.name)

    # Claimed but not yet fulfilled: the other caller is mid-flight. Asking the
    # browser to wait is honest — the alternative is a second reservation.
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "This payment is still being confirmed. Please refresh in a moment.",
    )


@router.post("/public/razorpay/webhook")
async def public_razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=None),
):
    """
    Razorpay's server-to-server callback. The reliable path.

    Deliberately not origin-guarded and not authenticated: Razorpay is not a
    browser. The HMAC over the **raw** body is the whole authentication story,
    which is why the body is read as bytes before anything parses it —
    re-serialising the JSON changes the bytes and the signature never matches.

    Always 200 once the signature is good. A non-2xx makes Razorpay retry, and
    retrying something that already succeeded is noise; the one case that
    deliberately *does* fail is a fulfilment error, because there a retry is
    exactly what we want.
    """
    if not razorpay.webhook_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook not configured.")

    raw = await request.body()
    if not razorpay.verify_webhook_signature(raw, x_razorpay_signature):
        logger.warning("[pms webhook] bad signature")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad signature.")

    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        return {"ok": True, "ignored": "unparseable"}

    name = event.get("event") or ""
    payload = event.get("payload") or {}
    payment = (payload.get("payment") or {}).get("entity") or {}
    order_id = payment.get("order_id") or (
        (payload.get("order") or {}).get("entity") or {}
    ).get("id")

    if not order_id:
        return {"ok": True, "ignored": "no order id"}

    order = await payments_service.get(order_id)
    if not order:
        # Not ours — the marketplace webhook handles its own orders.
        return {"ok": True, "ignored": "unknown order"}

    if name in ("payment.failed",):
        await payments_service.mark(order_id, "failed", payment.get("error_description"))
        return {"ok": True, "handled": "failed"}

    if name not in ("payment.captured", "order.paid"):
        return {"ok": True, "ignored": name}

    payment_id = payment.get("id")
    if not payment_id:
        return {"ok": True, "ignored": "no payment id"}

    settlement = await payments_service.claim(order_id, payment_id)
    if not settlement.won:
        # The browser got here first, or this is a duplicate delivery. Either
        # way there is nothing to do — and saying so is the whole point.
        return {"ok": True, "handled": "already settled"}

    try:
        if settlement.order["purpose"] == "booking":
            await _fulfil_booking(settlement.order)
        else:
            await _fulfil_credits(settlement.order)
    except Exception as exc:  # noqa: BLE001
        await payments_service.release(settlement.order["id"], str(exc)[:200])
        logger.exception("webhook could not fulfil order %s", order_id)
        # The one deliberate non-2xx: Razorpay retries for 24 hours, and a
        # retry is exactly the recovery this needs.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Could not fulfil the order.") from exc

    return {"ok": True, "handled": settlement.order["purpose"]}


# ── M5.6 — buying credits ────────────────────────────────────────────────────
#
# The ledger has been built and tested since migration 12; this is only the
# commerce. A purchase creates a `purchased` bucket through `grant_credits()`
# and touches nothing else — `charge_usage` is unchanged, no wallet balance is
# written directly, and the idempotency key is the Razorpay order id, so a
# webhook delivered three times grants once.

async def _fulfil_credits(order: dict) -> dict:
    """
    Grant the credits an order paid for. Called once per order, ever.

    `billing.grant()` is the only writer, and its key is derived from the
    Razorpay order — not from the clock, not from a uuid. That is what makes
    this safe to call from both the browser callback and a retried webhook.
    """
    key = f"credit-purchase:{order['razorpay_order_id']}"
    result = await billing.grant(
        billing_account_id=order["billing_account_id"],
        kind="purchased",
        credits=int(order["credits"]),
        idempotency_key=key,
        source_type="purchase",
        actor_type="user",
        metadata={
            "razorpayOrderId": order["razorpay_order_id"],
            "razorpayPaymentId": order.get("razorpay_payment_id"),
            "amountPaise": order["amount_paise"],
        },
    )
    await payments_service.record_result(order["id"], ledger_key=key)
    return result


@router.post("/properties/{property_id}/credits/order", response_model=CreditOrderOut)
@limiter.limit("10/minute")
async def create_credit_order(
    request: Request,
    property_id: str,
    body: CreditOrderRequest,
    member=Depends(require_member("billing")),
):
    """
    Start a credit top-up.

    The client asks for a number of credits; **the server decides what they
    cost**. `CREDIT_PRICE_PAISE` is the one place that conversion happens, and a
    rupee amount in the request body would be a field this model does not have.
    """
    account = await billing.account_for_property(property_id)
    if not account:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This property has no billing account.")
    if not razorpay.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Payments are unavailable right now.")

    amount = body.credits * CREDIT_PRICE_PAISE
    rzp = await razorpay.create_order(
        amount_paise=amount,
        receipt=f"credits-{property_id[:8]}",
        notes={"credits": str(body.credits), "property": property_id},
    )
    order = await payments_service.create(
        razorpay_order_id=rzp["id"], purpose="credits",
        partner_id=member.partner_id, property_id=property_id,
        billing_account_id=account, credits=body.credits, amount_paise=amount,
    )
    return CreditOrderOut(
        orderId=order["id"], razorpayOrderId=rzp["id"],
        keyId=razorpay.public_key_id() or "",
        credits=body.credits, amountPaise=amount,
    )


@router.post("/properties/{property_id}/credits/verify", response_model=CreditPurchaseOut)
@limiter.limit("20/minute")
async def verify_credit_purchase(
    request: Request,
    property_id: str,
    body: PublicConfirmRequest,
    member=Depends(require_member("billing")),
):
    """
    The browser returning from a credit top-up.

    Same convergence as a booking: the signature is checked, the claim decides
    who acts, and the webhook is the reliable path behind it. A duplicate call
    reports the wallet rather than granting again.
    """
    if not razorpay.verify_payment_signature(
        body.razorpayOrderId, body.razorpayPaymentId, body.razorpaySignature
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment could not be verified.")

    order = await payments_service.get(body.razorpayOrderId)
    if not order or order["purpose"] != "credits" or order["property_id"] != property_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that payment.")

    settlement = await payments_service.claim(body.razorpayOrderId, body.razorpayPaymentId)
    if settlement.won:
        try:
            result = await _fulfil_credits(settlement.order)
        except Exception as exc:  # noqa: BLE001
            await payments_service.release(settlement.order["id"], str(exc)[:200])
            logger.exception("could not grant credits for %s", body.razorpayOrderId)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                "Your payment went through and the credits have not "
                                "landed yet. They will shortly.") from exc
        state = await billing.wallet_state(order["billing_account_id"])
        return CreditPurchaseOut(
            status="granted", credits=int(order["credits"]),
            creditsGranted=int(order["credits"]),
            balanceAfter=int(state.get("balance") or 0),
        )

    state = await billing.wallet_state(order["billing_account_id"])
    return CreditPurchaseOut(
        status="already_granted", credits=int(order["credits"]),
        creditsGranted=0, balanceAfter=int(state.get("balance") or 0),
        alreadyProcessed=True,
    )


@router.get("/properties/{property_id}/payments", response_model=list[PaymentOrderOut])
@limiter.limit("60/minute")
async def list_payment_orders(
    request: Request,
    property_id: str,
    limit: int = Query(50, ge=1, le=200),
    member=Depends(require_member("billing")),
):
    """Every order this property has started, paid or otherwise."""
    rows = await payments_service.history(property_id, limit)
    return [
        PaymentOrderOut(
            id=r["id"], razorpayOrderId=r["razorpay_order_id"], purpose=r["purpose"],
            amountPaise=r["amount_paise"], credits=r.get("credits"),
            status=r["status"], reservationId=r.get("reservation_id"),
            createdAt=r["created_at"], paidAt=r.get("paid_at"),
            failureReason=r.get("failure_reason"),
        ) for r in rows
    ]


# ── M5.4 — the website builder's own API ─────────────────────────────────────

@router.get("/properties/{property_id}/website", response_model=WebsiteConfigOut)
@limiter.limit("120/minute")
async def get_website(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    The builder's view: the **draft**, always.

    'rates' rather than a new scope. Publishing a website is a commercial act
    about how a hotel sells itself, and the permission that already governs what
    it sells for is the right one — a receptionist does not get to rewrite the
    hotel's home page.
    """
    row = await website_service.get_config(property_id)
    return website_service.project(row)


@router.patch("/properties/{property_id}/website", response_model=WebsiteConfigOut)
@limiter.limit("120/minute")
async def update_website(
    request: Request,
    property_id: str,
    body: WebsiteUpdateRequest,
    member=Depends(require_member("rates")),
):
    """
    Save the draft. **Never the published copy.**

    `website.save_draft` writes one column and cannot reach the other, which is
    the whole reason draft and published are separate: a hotelier typing a new
    headline must not change the page a guest is reading.
    """
    row = await website_service.save_draft(
        property_id,
        template=body.template,
        draft=body.model_dump(exclude_none=True, exclude={"template"}),
    )
    return website_service.project(row)


@router.post("/properties/{property_id}/website/publish", response_model=WebsiteConfigOut)
@limiter.limit("30/minute")
async def publish_website(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """Copy the draft to published, and make `/stay/[slug]` answer."""
    row = await website_service.publish(property_id, member.profile_id)
    return website_service.project(row)


@router.post("/properties/{property_id}/website/unpublish", response_model=WebsiteConfigOut)
@limiter.limit("30/minute")
async def unpublish_website(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    Take the site down. The content is kept.

    Unpublishing is not deleting: a hotel that goes quiet for a season should
    not have to rebuild its page to come back.
    """
    row = await website_service.unpublish(property_id)
    return website_service.project(row)
