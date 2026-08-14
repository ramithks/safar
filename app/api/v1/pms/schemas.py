"""
Request and response models for the PMS API.

Field names are camelCase because the consumer is the TravBuddy Partner PWA,
whose `PmsApi` interface these mirror one-for-one (see
Travbuddy_supply_os/src/data/api.ts). Converting at the edge here is cheaper
than a mapping layer in the client, and it keeps the contract legible on both
sides: a field in this file has exactly one counterpart in the TypeScript.

Money is `int` paise everywhere. There is no float in this file and there must
never be one — a rupee amount that has been through a float has already lost the
argument with an accountant.
"""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# The six values `quotes.discount_reason` will accept. This is a category, not
# a sentence — "why the rate moved", so margin can be read by cause later. It
# is spelled out here because the alternative is what happened in testing: the
# field typed as free text, a receptionist writing "regular guest", and the
# check constraint turning a reasonable entry into a 500. Refuse it at the edge
# with a message naming the six, or it fails four layers down.
# Keep in sync with 20260813_hotel_os_4_reservations.sql.
DiscountReason = Literal["volume", "negotiation", "coupon", "creator", "loyalty", "manual"]


# ── availability ─────────────────────────────────────────────────────────────

class RatePlanOptionOut(BaseModel):
    ratePlanId: str
    code: str
    name: str
    mealPlan: str
    audience: str
    refundable: bool
    inclusions: list[str] = []
    totalPaise: int
    perNightPaise: int
    minStayViolation: int | None = None
    closed: bool = False


class StayOptionOut(BaseModel):
    roomTypeId: str
    roomTypeCode: str
    roomTypeName: str
    unitsLeft: int
    maxOccupancy: int
    ratePlans: list[RatePlanOptionOut]


# ── quotes ───────────────────────────────────────────────────────────────────

class QuoteRequest(BaseModel):
    roomTypeId: str
    ratePlanId: str
    checkIn: date
    checkOut: date
    units: int = Field(default=1, ge=1, le=20)
    adults: int = Field(default=2, ge=0, le=40)
    children: int = Field(default=0, ge=0, le=20)
    # A *request*. Clamped server-side against the property's floor — see
    # services/pms/pricing.py. This is why an agent may ask for anything.
    discountPct: int = Field(default=0, ge=0, le=100)
    discountReason: DiscountReason | None = None
    conversationId: str | None = None


class NightlyOut(BaseModel):
    date: date
    pricePaise: int


class QuoteOut(BaseModel):
    id: str
    holdId: str | None
    propertyId: str
    roomTypeId: str
    ratePlanId: str
    checkIn: date
    checkOut: date
    units: int
    adults: int
    children: int
    nightly: list[NightlyOut]
    grossPaise: int
    discountPaise: int
    discountReason: DiscountReason | None = None
    taxPaise: int
    totalPaise: int
    tokenPaise: int
    balancePaise: int
    issuedBy: str
    expiresAt: datetime
    # Present when the caller asked for a bigger discount than the property
    # allows. The UI shows it to a human; an agent must not repeat the request.
    clampedFromPct: int | None = None


# ── booking ──────────────────────────────────────────────────────────────────

class BookingRequest(BaseModel):
    quoteId: str
    guestName: str = Field(min_length=1, max_length=120)
    guestPhone: str | None = None
    guestEmail: str | None = None
    source: str = "direct"
    tokenPaise: int = Field(default=0, ge=0)
    paymentMethod: str | None = None
    notes: str | None = None


class ReservationOut(BaseModel):
    id: str
    ref: str
    propertyId: str
    roomTypeId: str
    ratePlanId: str
    roomId: str | None = None
    guestId: str | None = None
    guestName: str
    guestPhone: str | None = None
    guestEmail: str | None = None
    checkIn: date
    checkOut: date
    nights: int
    units: int
    adults: int
    children: int
    grossPaise: int
    discountPaise: int
    taxPaise: int
    totalPaise: int
    paidPaise: int
    balancePaise: int
    # What is actually on the folio right now, summed from the ledger. Differs
    # from balancePaise only before the stay is fully billed — a booking whose
    # nights the audit has not reached yet is contracted for money it has not
    # been charged for. At check-out they are equal, because check-out posts
    # every remaining night before it lets anyone settle. Null where the caller
    # did not ask for it.
    folioBalancePaise: int | None = None
    source: str
    status: str
    paymentStatus: str
    notes: str | None = None
    createdAt: datetime

    @staticmethod
    def from_row(row: dict, folio_balance: int | None = None) -> "ReservationOut":
        return ReservationOut(
            folioBalancePaise=folio_balance,
            id=row["id"],
            ref=row["ref"],
            propertyId=row["property_id"],
            roomTypeId=row["room_type_id"],
            ratePlanId=row["rate_plan_id"],
            roomId=row.get("room_id"),
            guestId=row.get("guest_id"),
            guestName=row["guest_name"],
            guestPhone=row.get("guest_phone"),
            guestEmail=row.get("guest_email"),
            checkIn=row["check_in"],
            checkOut=row["check_out"],
            nights=row["nights"],
            units=row["units"],
            adults=row["adults"],
            children=row["children"],
            grossPaise=row["gross_paise"],
            discountPaise=row["discount_paise"],
            taxPaise=row["tax_paise"],
            totalPaise=row["total_paise"],
            paidPaise=row["paid_paise"],
            # Derived rather than stored: two numbers that must agree are one
            # number too many.
            balancePaise=row["total_paise"] - row["paid_paise"],
            source=row["source"],
            status=row["status"],
            paymentStatus=row["payment_status"],
            notes=row.get("notes"),
            createdAt=row["created_at"],
        )


# ── front desk ───────────────────────────────────────────────────────────────

class CheckInRequest(BaseModel):
    roomId: str


class FolioEntryOut(BaseModel):
    id: str
    reservationId: str
    kind: str
    description: str
    amountPaise: int
    method: str | None = None
    postedAt: datetime
    postedBy: str | None = None

    @staticmethod
    def from_row(row: dict) -> "FolioEntryOut":
        return FolioEntryOut(
            id=row["id"],
            reservationId=row["reservation_id"],
            kind=row["kind"],
            description=row["description"],
            amountPaise=row["amount_paise"],
            method=row.get("method"),
            postedAt=row["posted_at"],
            postedBy=row.get("posted_by_name"),
        )


class FolioOut(BaseModel):
    entries: list[FolioEntryOut]
    balancePaise: int


# ── calendar (ARI) ───────────────────────────────────────────────────────────

class InventoryNightOut(BaseModel):
    roomTypeId: str
    date: date
    unitsTotal: int
    unitsBooked: int
    unitsHeld: int
    unitsBlocked: int
    unitsLeft: int
    stopSell: bool


class RateNightOut(BaseModel):
    ratePlanId: str
    date: date
    pricePaise: int
    minStay: int | None = None
    closedToArrival: bool
    closedToDeparture: bool
    closed: bool


class AriEditBase(BaseModel):
    # Inclusive both ends. A manager selecting "the 3rd to the 5th" on the
    # calendar means three nights; the half-open convention belongs to stays,
    # where it stops a departure morning counting as a night.
    fromDate: date
    toDate: date


class InventoryEditRequest(AriEditBase):
    roomTypeId: str
    # Every field is optional and null means *leave it alone*. That is what lets
    # "stop selling this weekend" avoid also writing today's inherited unit
    # count into two rows that will then stop following the room type.
    unitsTotal: int | None = Field(None, ge=0)
    unitsBlocked: int | None = Field(None, ge=0)
    stopSell: bool | None = None
    oversellAllowance: int | None = Field(None, ge=0)


class RateEditRequest(AriEditBase):
    ratePlanId: str
    pricePaise: int | None = Field(None, ge=0)
    minStay: int | None = Field(None, ge=1)
    maxStay: int | None = Field(None, ge=1)
    closedToArrival: bool | None = None
    closedToDeparture: bool | None = None
    closed: bool | None = None


class AriEditOut(BaseModel):
    # How many nights were written. The calendar shows this back, because "saved"
    # with no number is exactly how a range edit that silently applied to one
    # night goes unnoticed.
    nights: int


class NightAuditRequest(BaseModel):
    # Omitted means the night that has just ended on the *property's* clock —
    # yesterday, locally. A past date lets a property close days it missed; a
    # future one posts nothing beyond each stay's own departure, so it cannot be
    # used to bill a guest in advance.
    businessDate: date | None = None


class NightAuditOut(BaseModel):
    businessDate: date
    reservations: int
    entries: int


class FolioPostRequest(BaseModel):
    kind: str
    description: str = Field(min_length=1, max_length=200)
    # Always positive. The sign is decided server-side by `kind`.
    amountPaise: int = Field(gt=0)
    method: str | None = None
    # Client-generated. What makes an offline replay a no-op instead of a second
    # charge on a guest's account.
    idempotencyKey: str = Field(min_length=8, max_length=100)


class RoomStatusRequest(BaseModel):
    status: str


class RoomOccupantOut(BaseModel):
    reservationId: str
    guestName: str
    checkOut: date


class RoomOut(BaseModel):
    id: str
    propertyId: str
    roomTypeId: str
    number: str
    floor: str
    housekeepingStatus: str
    isActive: bool
    outOfOrderReason: str | None = None
    outOfOrderUntil: date | None = None
    # Present only while a stay occupies the room. The room picker at check-in
    # needs this to grey out a room that is physically clean but still has
    # someone in it — "clean" and "free" are different questions.
    occupiedBy: RoomOccupantOut | None = None

    @staticmethod
    def from_row(row: dict, occupant: dict | None = None) -> "RoomOut":
        return RoomOut(
            id=row["id"],
            propertyId=row["property_id"],
            roomTypeId=row["room_type_id"],
            number=row["number"],
            # The PWA types `floor` as a plain string and groups by it; a null
            # would land as the literal "null" in a heading.
            floor=row.get("floor") or "",
            housekeepingStatus=row["housekeeping_status"],
            isActive=row["is_active"],
            outOfOrderReason=row.get("out_of_order_reason"),
            outOfOrderUntil=row.get("out_of_order_until"),
            occupiedBy=RoomOccupantOut(
                reservationId=occupant["id"],
                guestName=occupant["guest_name"],
                checkOut=occupant["check_out"],
            ) if occupant else None,
        )


class TaskCreateRequest(BaseModel):
    # Client-generated so the same complaint cannot become two tickets.
    id: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    category: str
    priority: str = "normal"
    roomNumber: str | None = None
    conversationId: str | None = None
    sourceChannel: str | None = None
    guestName: str | None = None
    createdBy: str = "staff"


class TaskStatusRequest(BaseModel):
    status: str


class TaskOut(BaseModel):
    id: str
    propertyId: str
    code: str
    title: str
    category: str
    priority: str
    status: str
    roomNumber: str | None = None
    reservationId: str | None = None
    assigneeId: str | None = None
    assigneeName: str | None = None
    createdBy: str
    conversationId: str | None = None
    sourceChannel: str | None = None
    guestName: str | None = None
    createdAt: datetime
    resolvedAt: datetime | None = None

    @staticmethod
    def from_row(row: dict) -> "TaskOut":
        return TaskOut(
            id=row["id"],
            propertyId=row["property_id"],
            code=row["code"],
            title=row["title"],
            category=row["category"],
            priority=row["priority"],
            status=row["status"],
            roomNumber=row.get("room_number"),
            reservationId=row.get("reservation_id"),
            assigneeId=row.get("assignee_member"),
            assigneeName=row.get("assignee_name"),
            createdBy=row["created_by"],
            conversationId=row.get("conversation_id"),
            sourceChannel=row.get("source_channel"),
            guestName=row.get("guest_name"),
            createdAt=row["created_at"],
            resolvedAt=row.get("resolved_at"),
        )


# ── workspace: who the caller is, and what they may open ─────────────────────

class MembershipOut(BaseModel):
    partnerId: str
    memberId: str | None
    role: str
    # Flattened from role + per-member grants, so the client never reimplements
    # the permission table. The server's copy stays authoritative: this list is
    # for deciding which buttons to draw, never for deciding what is allowed.
    permissions: list[str]
    # Empty means every property of the partner.
    propertyIds: list[str]


class MeOut(BaseModel):
    profileId: str
    email: str | None = None
    fullName: str | None = None
    memberships: list[MembershipOut]


class PropertyOut(BaseModel):
    id: str
    slug: str
    name: str
    propertyType: str
    city: str
    state: str
    timezone: str
    currency: str
    checkInTime: str
    checkOutTime: str
    totalRooms: int
    gstin: str | None = None
    status: str

    @staticmethod
    def from_row(row: dict) -> "PropertyOut":
        return PropertyOut(
            id=row["id"],
            slug=row["slug"],
            name=row["name"],
            propertyType=row["property_type"],
            city=row.get("city") or "",
            state=row.get("state") or "",
            timezone=row.get("timezone") or "Asia/Kolkata",
            currency=row.get("currency") or "INR",
            # Postgres hands back '14:00:00'; the PWA renders 'HH:MM'.
            checkInTime=str(row.get("check_in_time") or "14:00")[:5],
            checkOutTime=str(row.get("check_out_time") or "11:00")[:5],
            totalRooms=row.get("total_rooms") or 0,
            gstin=row.get("gstin"),
            status=row["status"],
        )


class RoomTypeOut(BaseModel):
    id: str
    propertyId: str
    code: str
    name: str
    totalUnits: int
    maxOccupancy: int
    standardOccupancy: int
    bedType: str
    sortOrder: int
    isActive: bool

    @staticmethod
    def from_row(row: dict) -> "RoomTypeOut":
        return RoomTypeOut(
            id=row["id"],
            propertyId=row["property_id"],
            code=row["code"],
            name=row["name"],
            totalUnits=row["total_units"],
            maxOccupancy=row["max_occupancy"],
            standardOccupancy=row["standard_occupancy"],
            bedType=row.get("bed_type") or "",
            sortOrder=row.get("sort_order") or 0,
            isActive=row["is_active"],
        )


class RatePlanOut(BaseModel):
    id: str
    propertyId: str
    roomTypeId: str
    code: str
    name: str
    mealPlan: str
    audience: str
    basePricePaise: int
    refundable: bool
    inclusions: list[str]
    isActive: bool

    @staticmethod
    def from_row(row: dict) -> "RatePlanOut":
        return RatePlanOut(
            id=row["id"],
            propertyId=row["property_id"],
            roomTypeId=row["room_type_id"],
            code=row["code"],
            name=row["name"],
            mealPlan=row["meal_plan"],
            audience=row["audience"],
            basePricePaise=row["base_price_paise"],
            refundable=row["refundable"],
            inclusions=row.get("inclusions") or [],
            isActive=row["is_active"],
        )


class SourceMixOut(BaseModel):
    source: str
    bookings: int
    revenuePaise: int


class TodayStatsOut(BaseModel):
    date: date
    occupancyPct: float
    occupancyDeltaPct: float
    adrPaise: int
    revparPaise: int
    roomsSold: int
    roomsAvailable: int
    arrivals: int
    arrivalsPending: int
    departures: int
    departuresPending: int
    inHouse: int
    revenueTodayPaise: int
    openTasks: int
    unreadConversations: int
    bySource: list[SourceMixOut]


# ── sync ─────────────────────────────────────────────────────────────────────

class SyncOut(BaseModel):
    # Echo this back as the next `since`. The server's clock, never the
    # client's: a front desk PC running ten minutes fast would otherwise skip
    # every change in the gap and never learn it had.
    serverTime: datetime
    # The local copy cannot be brought up to date incrementally — replace it.
    # Set when there was no cursor, when a page was truncated, or when the
    # cursor predates the oldest deletion still on record.
    fullResync: bool
    reservations: list[ReservationOut]
    rooms: list[RoomOut]
    tasks: list[TaskOut]
    folioEntries: list[FolioEntryOut]
    # Ids that stopped existing, by entity. The one thing a merge cannot express
    # and a client cannot infer: absence from a filtered list is not deletion.
    deleted: dict[str, list[str]]


# ── inbox ────────────────────────────────────────────────────────────────────

class ConversationOut(BaseModel):
    id: str
    propertyId: str
    channel: str
    guestName: str
    guestPhone: str | None = None
    guestHandle: str | None = None
    lastMessage: str
    lastMessageAt: datetime
    # Drives the 24-hour window on the client the same way it does on the
    # server. Nullable: a conversation nobody has written to has no window at
    # all, which is different from a window that has closed.
    lastInboundAt: datetime | None = None
    lastOutboundAt: datetime | None = None
    windowOpen: bool = False
    unread: int = 0
    handler: str = "ai"
    aiEnabled: bool = True
    assigneeName: str | None = None
    intent: str | None = None
    reservationId: str | None = None
    guestId: str | None = None

    @staticmethod
    def from_row(row: dict, *, window_open: bool = False,
                 assignee_name: str | None = None) -> "ConversationOut":
        return ConversationOut(
            id=row["id"],
            propertyId=row["property_id"],
            channel=row["channel"],
            guestName=row.get("guest_name") or row.get("guest_phone") or "Guest",
            guestPhone=row.get("guest_phone"),
            guestHandle=row.get("guest_handle"),
            lastMessage=row.get("last_message") or "",
            lastMessageAt=row.get("last_message_at") or row["created_at"],
            lastInboundAt=row.get("last_inbound_at"),
            lastOutboundAt=row.get("last_outbound_at"),
            windowOpen=window_open,
            unread=row.get("unread_count") or 0,
            handler=row.get("handler") or "ai",
            aiEnabled=bool(row.get("ai_enabled", True)),
            assigneeName=assignee_name,
            intent=row.get("intent"),
            reservationId=row.get("reservation_id"),
            guestId=row.get("guest_id"),
        )


class MessageOut(BaseModel):
    id: str
    conversationId: str
    direction: str
    author: str
    authorName: str | None = None
    body: str
    messageType: str = "text"
    sentAt: datetime
    deliveryStatus: str = "sent"
    failureReason: str | None = None
    templateName: str | None = None
    mediaUrls: list[str] = []
    # The action that sent it, when an agent did. What lets a receptionist read
    # a reply and ask what the AI was doing.
    agentActionId: str | None = None

    @staticmethod
    def from_row(row: dict) -> "MessageOut":
        return MessageOut(
            id=row["id"],
            conversationId=row["conversation_id"],
            direction=row["direction"],
            author=row["author"],
            authorName=row.get("author_name"),
            body=row.get("body") or "",
            messageType=row.get("message_type") or "text",
            sentAt=row.get("sent_at") or row["created_at"],
            deliveryStatus=row.get("delivery_status") or "sent",
            failureReason=row.get("failure_reason"),
            templateName=row.get("template_name"),
            mediaUrls=row.get("media_urls") or [],
            agentActionId=row.get("agent_action_id"),
        )


class SendMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class ConversationHandlerRequest(BaseModel):
    # 'human' takes it over; 'ai' hands it back. Deliberately explicit — a
    # conversation never drifts back to the agent because a human stopped
    # typing.
    handler: str = Field(pattern="^(ai|human)$")


# ── agents ───────────────────────────────────────────────────────────────────

class AgentOut(BaseModel):
    id: str
    propertyId: str
    agentType: str
    name: str
    autonomyLevel: int
    model: str
    provider: str
    isActive: bool
    status: str
    channels: list[str] = []
    tools: list[str] = []
    permissions: list[str] = []
    lastRunAt: datetime | None = None
    runsToday: int = 0
    # What this agent has actually consumed and done, over the window
    # `list_agents` reads. Every one of these is a count of rows, not an
    # estimate — a screen that shows an agent's credit spend has to be showing
    # the ledger's figure or it should show nothing at all.
    creditsCharged: int = 0
    actionsCount: int = 0
    conversationsCount: int = 0
    pendingApprovals: int = 0


class AgentActionOut(BaseModel):
    id: str
    agentId: str
    runId: str
    conversationId: str | None = None
    tool: str
    input: dict = {}
    output: str = ""
    refusalReason: str | None = None
    resultType: str | None = None
    resultId: str | None = None
    status: str
    createdAt: datetime
    latencyMs: int = 0
    creditsCharged: int = 0
    # Enough for a manager to decide without opening the conversation.
    guestName: str | None = None
    approvedBy: str | None = None
    approvedAt: datetime | None = None


class AgentAutonomyRequest(BaseModel):
    autonomyLevel: int = Field(ge=0, le=4)


class AgentDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=300)


# ── the staff console ────────────────────────────────────────────────────────

class ConsoleAskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # A *capability*, not a model. The router turns it into a tier — see
    # services/ai/model_router. Constrained to the front desk's own tasks plus
    # the two reasoning ones it can legitimately need, so a caller cannot route
    # every question to the expensive tier by typing its name.
    task: str = Field(
        default="frontdesk_operations",
        pattern="^(frontdesk_operations|frontdesk_summary|operational_analysis|multi_reservation)$",
    )


class ConsoleActionOut(BaseModel):
    """One tool call, as the console shows it back."""
    tool: str
    status: str
    refusalReason: str | None = None
    resultType: str | None = None
    resultId: str | None = None
    latencyMs: int = 0


class ConsoleAskOut(BaseModel):
    runId: str
    conversationId: str
    status: str
    reply: str | None = None
    handoff: bool = False
    # Returned rather than left to a second request: the desk needs to see that
    # a check-in went to the approval queue *in the same breath* as the reply
    # saying so, and a UI that has to poll for that will show the reply first.
    actions: list[ConsoleActionOut] = []
    pendingApproval: bool = False
    tokensIn: int = 0
    tokensOut: int = 0
    latencyMs: int = 0


# ── usage ────────────────────────────────────────────────────────────────────

class UsageEventOut(BaseModel):
    id: str
    usageType: str
    quantity: float
    provider: str | None = None
    model: str | None = None
    tokensIn: int | None = None
    tokensOut: int | None = None
    conversationId: str | None = None
    runId: str | None = None
    createdAt: datetime


class UsageMeterOut(BaseModel):
    """
    What a hotel is shown. Grouped by what they recognise — AI conversations,
    WhatsApp messages — never by tokens, which are our cost problem and not
    something anyone running a hotel should have to reason about.
    """
    code: str
    label: str
    unit: str
    used: float
    billable: bool
    # Which meter this belongs under. Driven by `usage_types.display_group`, so
    # a new billable type appears in the right place without a frontend change.
    group: str = "Usage"
    category: str = "platform"


class UsageGroupOut(BaseModel):
    """
    One meter, the way §8.10 says to show it:

        AI Usage
        1,240 / 2,000
        ────────────────░░░░
        Resets 1 September

    Usage against an allowance, not a credit balance. Credits are an internal
    monetization abstraction and a hotelier in Manali should never feel they are
    managing a token balance to answer a guest.
    """
    group: str
    category: str
    used: float
    allowance: float | None = None
    items: list[UsageMeterOut] = []


class UsageEventOut(BaseModel):
    """
    One line item. Carries what the hotel was charged and deliberately not what
    it cost us — that column is not granted to the browser role, and this model
    has no field for it so nothing can add one by accident.
    """
    id: str
    usageType: str
    displayName: str
    group: str = "Usage"
    agentName: str | None = None
    conversationId: str | None = None
    units: float = 0
    creditsCharged: int = 0
    provider: str | None = None
    model: str | None = None
    status: str = "charged"
    createdAt: datetime


class BillingSummaryOut(BaseModel):
    plan: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    # ok · low · critical · empty. Drives the banner and nothing else; the
    # numbers behind it are only surfaced in the purchase flow and the export.
    tier: str = "unknown"
    balance: int = 0
    available: int = 0
    groups: list[UsageGroupOut] = []
