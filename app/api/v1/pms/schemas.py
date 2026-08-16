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
    # Both closed, and both mirroring the check constraints on `folio_entries`.
    # They were plain strings, which meant a value outside the enumeration
    # reached Postgres and came back as a 500 — a receptionist told "an
    # unexpected error occurred" for a typo in a payment method. A Literal turns
    # the same mistake into a 422 that names the field and lists what it accepts.
    kind: Literal["room", "addon", "tax", "payment", "refund", "adjustment"]
    description: str = Field(min_length=1, max_length=200)
    # Always positive. The sign is decided server-side by `kind`.
    amountPaise: int = Field(gt=0)
    method: Literal["cash", "card", "upi", "bank", "gateway"] | None = None
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
    #
    # **Null is the normal case.** A receptionist should not have to pick a
    # capability from a dropdown to get the right model, so an absent task hands
    # the decision to `services/ai/classify` — one call on the cheapest tier
    # there is, with no tools, choosing from the front desk's own list. An
    # explicit task still wins: a caller that knows should not be second-guessed.
    task: str | None = Field(
        default=None,
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


# ── marketing campaigns ──────────────────────────────────────────────────────

class CampaignOut(BaseModel):
    """
    One campaign, with the numbers a screen shows.

    The counts are every terminal state a recipient can reach, plus the
    breakdown of *why* people were skipped. That last field is the one that
    matters: a campaign reporting "83 sent" out of an audience of 127 has to be
    able to name the other 44, or the first question anybody asks has no answer.
    """
    id: str
    name: str
    campaignType: str
    channel: str
    status: str
    offer: dict = {}
    body: str | None = None
    templatePurpose: str | None = None
    version: int = 1
    createdBy: str
    approvedBy: str | None = None
    approvedAt: datetime | None = None
    scheduledAt: datetime | None = None
    startedAt: datetime | None = None
    completedAt: datetime | None = None
    cancelledAt: datetime | None = None
    outcomeReason: str | None = None

    audienceSize: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    pending: int = 0
    # reason -> count, from campaign_recipients.reason.
    skippedBy: dict[str, int] = {}
    # Pulled out of skippedBy because it is the number a hotelier watches, and a
    # figure buried in a dict is a figure nobody watches.
    optedOut: int = 0

    # From the ledger, not from a count times a rate. None when nothing has been
    # charged yet, which the screen renders as "—" rather than as zero.
    creditsCharged: float | None = None
    providerCostMicros: int | None = None
    createdAt: datetime


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


# ── owner analytics ──────────────────────────────────────────────────────────
#
# These mirror `services/pms/analytics` field for field rather than reshaping
# it. The whole point of that module is that there is one definition of
# occupancy in the system; a response model that renamed or recomputed anything
# would be the second one.

class PeriodOut(BaseModel):
    """A stretch of the property's own calendar. Both ends inclusive."""
    key: str
    label: str
    # `from` is a Python keyword, so the field is declared under an alias and
    # populated by it. The wire name is what the PWA reads.
    from_: str = Field(alias="from")
    to: str
    days: int
    timezone: str

    model_config = {"populate_by_name": True}


class MetricOut(BaseModel):
    """
    One number and everything needed to read it.

    `change` is in the metric's own unit — percentage *points* for a percentage,
    paise for money. `changePct` is the relative move and is null wherever
    reporting it would be a different claim from the one the numbers support.
    Both are sent, so no client has to decide which applies.
    """
    key: str
    label: str
    # `int | float`, in that order, so paise stay integers on the wire. Pydantic's
    # smart union keeps the exact type; a bare `float` would render every rupee
    # amount with a decimal point it does not have.
    value: int | float | None = None
    unit: str
    currency: str | None = None
    previous: int | float | None = None
    change: float | None = None
    changePct: float | None = None
    basis: str
    sample: int = 0
    # False when the metric rests on fewer observations than this property's
    # threshold. A client showing a value with this false must say so; an agent
    # must refuse to recommend from it.
    sufficient: bool = True


class SignalOut(BaseModel):
    """A deterministic observation. No model produced this."""
    kind: str
    severity: str
    headline: str
    metric: str
    observed: int | float | None = None
    comparison: int | float | None = None
    threshold: float
    unit: str
    detail: str = ""
    subject: dict | None = None


class AnalyticsReportOut(BaseModel):
    propertyId: str
    period: PeriodOut
    comparisonPeriod: PeriodOut | None = None
    currency: str
    metrics: list[MetricOut] = []
    # roomTypes · ratePlans · sources · services · daily. Left as a dict because
    # each cut has its own shape and the client renders them independently;
    # five near-identical models would be five places to keep in step.
    breakdowns: dict = {}
    signals: list[SignalOut] = []
    generatedAt: datetime


class DailyPointOut(BaseModel):
    date: str
    roomsSold: int
    roomsAvailable: int
    occupancyPct: float
    adrPaise: int
    revparPaise: int
    revenuePaise: int


class RecommendationOut(BaseModel):
    """
    One recommendation, with the argument that produced it.

    `evidence` carries the server's own figures at the moment the advice was
    given — not the model's copy of them. There is no `apply` on this object
    because there is nothing to apply it with.
    """
    id: str
    propertyId: str
    type: str
    status: str
    title: str
    rationale: str
    confidence: str
    expectedImpact: str | None = None
    risk: str | None = None
    suggestedAction: str | None = None
    evidence: list[dict] = []
    period: dict = {}
    subject: dict = {}
    createdBy: str | None = None
    createdAt: datetime | None = None
    reviewedAt: datetime | None = None
    agentActionId: str | None = None


class RecommendationStatusRequest(BaseModel):
    status: Literal["open", "reviewed", "dismissed", "actioned"]


class OwnerAskRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    # The owner's own capabilities and nothing else. A caller cannot name
    # `complex_negotiation` to route an owner question somewhere it does not
    # belong — and cannot name an agent at all: which agent answers is decided
    # by the endpoint, not by the body.
    task: str | None = Field(
        default=None,
        pattern="^(revenue_analysis|operational_analysis)$",
    )
    # Which period the structured half of the answer covers. The model may look
    # at others; this is what the cards render.
    period: str = Field(default="today")


class OwnerAskOut(BaseModel):
    """
    A structured answer, not a paragraph to be parsed.

    `summary` is the model's prose. Everything below it is server-computed and
    survives the model being unavailable — which is the point: `aiAvailable`
    false with a full `metrics` array is an honest state, and a Reports page
    built on this keeps working when Vertex does not.
    """
    runId: str | None = None
    conversationId: str | None = None
    status: str
    summary: str | None = None
    aiAvailable: bool = True
    # Present when the model could not answer. A sentence for a person, not a
    # stack trace.
    aiUnavailableReason: str | None = None

    period: PeriodOut
    comparisonPeriod: PeriodOut | None = None
    currency: str = "INR"
    metrics: list[MetricOut] = []
    signals: list[SignalOut] = []
    recommendations: list[RecommendationOut] = []
    # Which tools ran, so an owner can see what the answer was built from.
    evidence: list[ConsoleActionOut] = []
    # high · medium · low · insufficient_data. Derived from the samples the
    # metrics rest on, never from the model's own opinion of itself.
    confidence: str = "medium"
    creditsCharged: int = 0
    tokensIn: int = 0
    tokensOut: int = 0
    latencyMs: int = 0


class OwnerOverviewOut(BaseModel):
    """What the /owner screen renders before anybody types anything."""
    propertyId: str
    propertyName: str | None = None
    businessDate: str
    timezone: str
    currency: str = "INR"
    today: AnalyticsReportOut
    period: AnalyticsReportOut
    recommendations: list[RecommendationOut] = []


# ── guests, services, members, margin ────────────────────────────────────────
#
# Four screens that shipped reading seed data because the routes behind them did
# not exist. The tables, their RLS and every write path have been real since M1;
# only the read was missing, which is the worst shape for a gap to have — the
# Guests page rendered invented people in the same typeface as real ones.

class GuestOut(BaseModel):
    id: str
    propertyId: str
    name: str
    phone: str = ""
    email: str | None = None
    city: str | None = None
    stays: int = 0
    lastStayAt: date | None = None
    totalSpendPaise: int = 0
    notes: str | None = None
    vip: bool = False
    # Whether this guest may be sent a campaign. Read-only here: consent is
    # changed by the guest saying STOP, never by a screen.
    marketingOptIn: bool = False

    @classmethod
    def from_row(cls, r: dict) -> "GuestOut":
        return cls(
            id=r["id"],
            propertyId=r["property_id"],
            name=r.get("name") or "Guest",
            phone=r.get("phone") or "",
            email=r.get("email"),
            city=r.get("city"),
            stays=int(r.get("stays_count") or 0),
            lastStayAt=r.get("last_stay_at"),
            totalSpendPaise=int(r.get("total_spend_paise") or 0),
            notes=r.get("notes"),
            vip=bool(r.get("vip")),
            marketingOptIn=bool(r.get("marketing_opt_in")),
        )


class ServiceOut(BaseModel):
    id: str
    propertyId: str
    name: str
    description: str = ""
    pricePaise: int
    category: str
    isActive: bool = True
    dailyCapacity: int | None = None
    leadTimeHours: int = 0

    @classmethod
    def from_row(cls, r: dict) -> "ServiceOut":
        return cls(
            id=r["id"],
            propertyId=r["property_id"],
            name=r["name"],
            description=r.get("description") or "",
            pricePaise=int(r.get("price_paise") or 0),
            category=r.get("category") or "other",
            isActive=bool(r.get("is_active", True)),
            dailyCapacity=r.get("daily_capacity"),
            leadTimeHours=int(r.get("lead_time_hours") or 0),
        )


class ServiceUpsertRequest(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    # Paise, and an integer. A price that has been through a float has already
    # lost the argument with an accountant.
    pricePaise: int = Field(ge=0)
    category: Literal["transfer", "food", "wellness", "activity", "room", "other"]
    isActive: bool = True
    dailyCapacity: int | None = Field(default=None, ge=0)
    leadTimeHours: int = Field(default=0, ge=0, le=720)


class MemberOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    propertyIds: list[str] = []
    status: str = "active"
    # The flattened permission set, so the screen can show what a role actually
    # allows. Not a security boundary — `require_member` re-derives it server
    # side on every route, exactly as `/me` does.
    permissions: list[str] = []


class MarginRowOut(BaseModel):
    """
    Internal only. Carries provider cost, which is the margin.

    `require_member("billing")` gates the route and the service-role client does
    the read, because `usage_events.provider_cost_inr_micros` is revoked from browser
    roles by design — see migration 12. A hotel must never receive this shape.
    """
    propertyId: str
    propertyName: str
    city: str = ""
    plan: str = ""
    usageCredits: int = 0
    providerCostInrMicros: int = 0
    revenuePaise: int = 0
    marginPaise: int = 0
    topUsageType: str = ""


# ── web check-in ─────────────────────────────────────────────────────────────
#
# The only shapes in this file a stranger can reach. Everything here is either
# masked on the way out or write-only on the way in.

class PublicStayOut(BaseModel):
    """
    A stay, as the person holding its reference may see it.

    **Masked deliberately.** The booking reference is the whole credential, so
    what it unlocks has to be worth less than the reference is hard to guess:
    enough for a guest to recognise their own booking, not enough to be worth
    guessing at. No email, no full phone, no guest id, no room number, no folio
    detail beyond the balance they will be asked for.
    """
    ref: str
    propertyName: str
    propertyCity: str
    checkInTime: str
    checkOutTime: str
    guestName: str
    # Last four digits only. Enough to recognise, useless if leaked.
    guestPhoneMasked: str
    checkIn: date
    checkOut: date
    nights: int
    units: int
    adults: int
    children: int
    roomTypeName: str
    ratePlanName: str
    inclusions: list[str] = []
    totalPaise: int
    paidPaise: int
    balancePaise: int
    status: str
    registration: "GuestRegistrationOut | None" = None


class GuestRegistrationOut(BaseModel):
    reservationId: str
    fullName: str
    email: str = ""
    phone: str = ""
    address: str = ""
    city: str = ""
    country: str = ""
    idType: str | None = None
    # Never the whole document. The rest is not stored — see migration 19.
    idLast4: str = ""
    travellingWith: int = 0
    estimatedArrival: str | None = None
    specialRequests: str | None = None
    submittedAt: datetime

    @classmethod
    def from_row(cls, r: dict) -> "GuestRegistrationOut":
        arrival = r.get("estimated_arrival")
        return cls(
            reservationId=r["reservation_id"],
            fullName=r.get("full_name") or "",
            email=r.get("email") or "",
            phone=r.get("phone") or "",
            address=r.get("address") or "",
            city=r.get("city") or "",
            country=r.get("country") or "",
            idType=r.get("id_type"),
            idLast4=r.get("id_last4") or "",
            travellingWith=int(r.get("travelling_with") or 0),
            estimatedArrival=str(arrival)[:5] if arrival else None,
            specialRequests=r.get("special_requests"),
            submittedAt=r["submitted_at"],
        )


class WebCheckInRequest(BaseModel):
    """
    What a guest fills in on their phone.

    `idNumber` is **write-only and mostly discarded**: the server keeps its last
    four characters and never stores the rest. It is accepted at all so the form
    can look like the form a hotelier expects, and so a guest who has typed
    their passport number is not asked to retype only part of it.
    """
    fullName: str = Field(min_length=2, max_length=120)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=32)
    address: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=120)
    country: str = Field(default="", max_length=120)
    idType: Literal["passport", "aadhaar", "driving_licence",
                    "voter_id", "pan", "other"] | None = None
    idNumber: str = Field(default="", max_length=64)
    travellingWith: int = Field(default=0, ge=0, le=30)
    # 'HH:MM'. Refused rather than coerced: a malformed arrival time silently
    # dropped is a guest the desk is not expecting.
    estimatedArrival: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    specialRequests: str | None = Field(default=None, max_length=500)


# ── M5: public booking, website, credit purchase ─────────────────────────────

class PublicRoomOptionOut(BaseModel):
    """One room type a guest may book, with the rate plans they may see."""
    roomTypeId: str
    name: str
    code: str
    description: str | None = None
    maxOccupancy: int
    bedType: str | None = None
    amenities: list[str] = []
    unitsLeft: int
    ratePlans: list[RatePlanOptionOut] = []


class PublicAvailabilityOut(BaseModel):
    checkIn: date
    checkOut: date
    nights: int
    units: int
    currency: str = "INR"
    rooms: list[PublicRoomOptionOut] = []
    # Said plainly rather than left as an empty list. "Nothing is free" and "we
    # could not read availability" look identical to a guest otherwise.
    soldOut: bool = False


class PublicServiceOut(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str
    pricePaise: int
    leadTimeHours: int = 0
    dailyCapacity: int | None = None


class PublicSiteOut(BaseModel):
    """The published site, plus the facts a template needs about the hotel."""
    slug: str
    name: str
    city: str | None = None
    template: str
    currency: str = "INR"
    timezone: str
    checkInTime: str = ""
    checkOutTime: str = ""
    policies: dict = {}
    content: dict = {}
    services: list[PublicServiceOut] = []
    roomTypes: list[PublicRoomOptionOut] = []


class PublicQuoteRequest(BaseModel):
    checkIn: date
    checkOut: date
    ratePlanId: str
    units: int = Field(default=1, ge=1, le=5)
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)


class QuotedAddonOut(BaseModel):
    serviceId: str
    name: str
    quantity: int
    unitPricePaise: int
    totalPaise: int
    serviceDate: date | None = None


class PublicQuoteOut(BaseModel):
    """
    A priced stay, held.

    Every figure is the server's. There is no field here a client may send back
    changed and have honoured — the confirm step reads the quote row, not this
    response.
    """
    quoteId: str
    checkIn: date
    checkOut: date
    nights: int
    units: int
    roomTypeName: str
    ratePlanName: str
    inclusions: list[str] = []
    grossPaise: int
    discountPaise: int
    taxPaise: int
    totalPaise: int
    tokenPaise: int
    balancePaise: int
    # When the hold lets go. Shown, because a guest who wanders off and comes
    # back to a dead quote deserves to have been told.
    expiresAt: datetime
    currency: str = "INR"


class PublicAddonSelection(BaseModel):
    serviceId: str
    quantity: int = Field(default=1, ge=1, le=50)
    serviceDate: date | None = None
    # Deliberately no price field. If one is sent it is not read — see
    # `public_booking.price_addons`.


class PublicGuestDetails(BaseModel):
    fullName: str = Field(min_length=2, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    phone: str = Field(min_length=6, max_length=32)
    notes: str | None = Field(default=None, max_length=500)


class PublicOrderRequest(BaseModel):
    guest: PublicGuestDetails
    addons: list[PublicAddonSelection] = Field(default_factory=list, max_length=10)
    # Where the booking came from, when the link carried it. Recorded, never
    # trusted for anything priced.
    source: str | None = Field(default=None, max_length=40)
    # The ref this browser was given when it followed a creator's link, possibly
    # weeks ago. It names a row the server wrote, not a creator — resolution
    # happens in `creator_resolve_attribution`, which reads the chain and
    # ignores anything the client thinks it knows.
    visitorRef: str | None = Field(default=None, max_length=64)


class PublicOrderOut(BaseModel):
    """What Checkout needs, and nothing that would let a client decide a price."""
    orderId: str
    razorpayOrderId: str
    keyId: str
    amountPaise: int
    currency: str = "INR"
    quoteId: str
    addons: list[QuotedAddonOut] = []
    # The room's token plus the extras, broken out so the summary can show why
    # the number is what it is.
    roomTokenPaise: int
    addonsTotalPaise: int
    balanceDuePaise: int
    hotelName: str
    guestName: str
    guestEmail: str
    guestPhone: str


class PublicConfirmRequest(BaseModel):
    razorpayOrderId: str
    razorpayPaymentId: str
    razorpaySignature: str


class PublicBookingOut(BaseModel):
    """
    A confirmed booking, as a stranger may see it.

    The reference is the credential for web check-in, so it is here. No
    reservation id, no property id, no guest id — see `/public/stays/{ref}`,
    which makes the same argument at more length.
    """
    ref: str
    status: str
    hotelName: str
    checkIn: date
    checkOut: date
    nights: int
    units: int
    roomTypeName: str
    guestName: str
    totalPaise: int
    paidPaise: int
    balancePaise: int
    addons: list[QuotedAddonOut] = []
    checkInUrl: str
    currency: str = "INR"


class WebsiteConfigOut(BaseModel):
    propertyId: str
    template: str
    status: str
    travbuddyPath: str = ""
    domain: str | None = None
    headline: str = ""
    tagline: str = ""
    about: str = ""
    accentColor: str = "#0F766E"
    heroImageUrl: str | None = None
    gallery: list[str] = []
    amenities: list[str] = []
    contactPhone: str | None = None
    contactEmail: str | None = None
    sections: list[dict] = []
    publishedAt: datetime | None = None
    hasUnpublishedChanges: bool = False


class WebsiteUpdateRequest(BaseModel):
    template: str | None = None
    headline: str | None = Field(default=None, max_length=200)
    tagline: str | None = Field(default=None, max_length=300)
    about: str | None = Field(default=None, max_length=2000)
    accentColor: str | None = Field(default=None, max_length=32)
    heroImageUrl: str | None = Field(default=None, max_length=500)
    gallery: list[str] | None = None
    amenities: list[str] | None = None
    contactPhone: str | None = Field(default=None, max_length=32)
    contactEmail: str | None = Field(default=None, max_length=200)
    sections: list[dict] | None = None


class CreditOrderRequest(BaseModel):
    # A bundle, not a rupee amount. The client picks how many credits; the
    # server decides what they cost — the same reason no other request model
    # here carries a price.
    credits: int = Field(ge=100, le=200_000)


class CreditOrderOut(BaseModel):
    orderId: str
    razorpayOrderId: str
    keyId: str
    credits: int
    amountPaise: int
    currency: str = "INR"


class CreditPurchaseOut(BaseModel):
    status: str
    credits: int = 0
    creditsGranted: int = 0
    balanceAfter: int | None = None
    alreadyProcessed: bool = False


class PaymentOrderOut(BaseModel):
    id: str
    razorpayOrderId: str
    purpose: str
    amountPaise: int
    credits: int | None = None
    status: str
    reservationId: str | None = None
    createdAt: datetime
    paidAt: datetime | None = None
    failureReason: str | None = None


# ── M6: the B2B agent portal ─────────────────────────────────────────────────
#
# Three of the four shapes below exist because an agency is a principal that is
# not staff: it has a contract rather than a role, a credit line rather than a
# till, and a list of hotels it may sell rather than a property it works at.
#
# The fourth — `AgencyQuoteRequest` — exists for what it *omits*. There is no
# `discountPct` on it, and that is the decision: an agency's rate is already the
# negotiated one. A portal that could ask for a further discount would be a
# second negotiation channel with nobody in it, running against the same floor
# the Sales agent is clamped to but with no conversation anyone could review.


class AgencyContractOut(BaseModel):
    id: str
    code: str
    companyName: str
    contactName: str | None = None
    city: str | None = None
    phone: str | None = None
    email: str | None = None
    commissionPct: float
    creditLimitPaise: int
    # Derived, never stored: unsettled bookings plus live quotes. See
    # b2b_committed_paise() in migration 22.
    committedPaise: int
    availablePaise: int
    status: str
    partnerId: str
    propertyIds: list[str] = []


class AgencyMeOut(BaseModel):
    profileId: str
    fullName: str | None = None
    email: str | None = None
    contracts: list[AgencyContractOut]


class AgencyPropertyOut(BaseModel):
    id: str
    name: str
    city: str | None = None
    currency: str = "INR"
    checkInTime: str | None = None
    checkOutTime: str | None = None
    # Which contract covers this hotel. The portal reads the credit line from
    # here rather than assuming an agency has only one.
    agencyId: str


class AgencyQuoteRequest(BaseModel):
    ratePlanId: str
    checkIn: date
    checkOut: date
    units: int = Field(default=1, ge=1, le=20)
    adults: int = Field(default=2, ge=0, le=40)
    children: int = Field(default=0, ge=0, le=20)


class AgencyQuoteOut(QuoteOut):
    """A quote with the state of the line it was just committed against."""

    agencyId: str
    committedPaise: int
    creditLimitPaise: int
    availablePaise: int


class AgencyBookingRequest(BaseModel):
    quoteId: str
    guestName: str = Field(min_length=1, max_length=120)
    guestPhone: str | None = None
    guestEmail: str | None = None
    notes: str | None = Field(default=None, max_length=500)


# ── M6b: the creator programme ───────────────────────────────────────────────
#
# One card per *participation* — a creator in a campaign — because that is the
# unit a hotel acts on: they pause @maya, not "the August programme". The
# campaign's own name and terms ride along, and `status` is the effective one
# (see `_effective_status` in services/pms/creators.py).
#
# Every money field here is derived server-side and none of them is accepted on
# any request. `CampaignStatusRequest` and `PayoutApprovalRequest` carry no
# amount, no room-night count and no creator — there is nothing on the wire that
# could move a rupee.


class CreatorCampaignOut(BaseModel):
    id: str
    campaignId: str
    campaignName: str
    propertyId: str

    creatorId: str
    creatorName: str
    creatorHandle: str
    platform: Literal["instagram", "youtube", "blog", "other"]
    followers: int

    # The only identifier that ever leaves the building. No creator, campaign or
    # property uuid appears in a public link.
    code: str
    status: Literal["draft", "live", "paused", "ended"]
    startDate: date
    endDate: date

    guestDiscountPct: float
    commissionPct: float
    attributionWindowDays: int
    minNights: int

    clicks: int
    conversations: int
    bookings: int
    roomNights: int
    revenuePaise: int
    # What is waiting for somebody to press approve, and what already has been.
    payoutPaise: int
    approvedPaise: int
    payoutStatus: Literal["accruing", "pending_approval", "approved", "paid"]


class AttributionTouchOut(BaseModel):
    """
    One link in the chain, as an owner reads it.

    `reservationRef` and `valuePaise` are filled in on the booking touch, which
    is what turns a list of clicks into an answer to "why did we pay this".
    """

    id: str
    campaignId: str
    kind: Literal["click", "conversation", "quote", "booking"]
    source: Literal["website", "whatsapp", "instagram", "direct", "other"]
    at: datetime
    expiresAt: datetime
    city: str | None = None
    touchRef: str | None = None
    conversationId: str | None = None
    reservationId: str | None = None
    reservationRef: str | None = None
    nights: int | None = None
    valuePaise: int | None = None


class PayoutEventOut(BaseModel):
    fromStatus: str
    toStatus: str
    actorName: str
    amountPaise: int
    note: str | None = None
    at: datetime


class CreatorPayoutOut(BaseModel):
    id: str
    campaignId: str
    reservationId: str
    reservationRef: str
    guestName: str
    checkIn: date
    checkOut: date
    source: str
    reservationStatus: str

    status: Literal["accruing", "pending_approval", "approved", "rejected", "paid"]
    eligible: bool
    # Why not, in a sentence a hotel can read back to a creator.
    ineligibleReason: str | None = None

    roomNights: int
    netRevenuePaise: int
    # Snapshotted at attribution. September's renegotiation does not restate
    # August — see creator_payouts in migration 23.
    commissionPct: float
    termsVersion: int
    amountPaise: int

    approvedByName: str | None = None
    approvedAt: datetime | None = None
    events: list[PayoutEventOut] = []


class CampaignStatusRequest(BaseModel):
    # 'draft' is deliberately absent: a campaign cannot be un-launched, and
    # offering it would offer to make touches already recorded ambiguous.
    status: Literal["live", "paused", "ended"]


class PayoutApprovalRequest(BaseModel):
    note: str | None = Field(default=None, max_length=300)


class PayoutApprovalOut(BaseModel):
    approved: int
    amountPaise: int


class PublicAttributionRequest(BaseModel):
    code: str = Field(min_length=3, max_length=24)
    # Returned by a previous call and sent back by the browser. It is a handle
    # on a row the server wrote, not a claim about which creator is owed money.
    visitorRef: str | None = Field(default=None, max_length=64)


class PublicAttributionOut(BaseModel):
    """
    What a guest's browser is told after following a creator's link.

    `attributed=False` is a normal answer, not an error: a campaign the hotel
    paused last week still shows the hotel, it just no longer credits anyone.
    """

    attributed: bool
    visitorRef: str
    creatorName: str | None = None
    creatorHandle: str | None = None
    campaignName: str | None = None
    guestDiscountPct: float = 0
    # Goes into the prefilled text of the hotel's WhatsApp link, so the chain
    # survives the channel change without asking the guest for anything.
    touchRef: str | None = None


class CreatorCampaignCreateRequest(BaseModel):
    """
    Add a creator to a campaign, making both if they are new.

    There is no `code` field and there never will be: the public code is
    generated server-side. A hotel that could choose one could choose one
    already live at another property, and the unique index would turn a typo
    into a 500 on a screen with nowhere to go.

    `campaignId` is how a second creator joins an existing programme. Omitted,
    the campaign fields below are required and a new one is created as a draft.
    """

    creatorName: str = Field(min_length=1, max_length=80)
    creatorHandle: str = Field(min_length=2, max_length=60)
    platform: Literal["instagram", "youtube", "blog", "other"] = "instagram"
    followers: int = Field(default=0, ge=0, le=1_000_000_000)
    city: str | None = Field(default=None, max_length=80)

    campaignId: str | None = None
    campaignName: str | None = Field(default=None, max_length=120)
    startDate: date | None = None
    endDate: date | None = None
    commissionPct: float = Field(default=10, ge=0, le=50)
    guestDiscountPct: float = Field(default=0, ge=0, le=50)
    attributionWindowDays: int = Field(default=30, ge=1, le=180)
    minNights: int = Field(default=1, ge=1, le=30)
