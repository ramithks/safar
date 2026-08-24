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

from app.api.v1.pms.hotel_os_schemas import PhotoMeta
from app.services.pms import money

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
    def from_row(row: dict, folio: dict | None = None) -> "ReservationOut":
        """
        One reservation, with the same balance every other surface shows.

        `balancePaise` was `total_paise - paid_paise`, computed right here — the
        fifth independent answer to a question that should have had one. It is
        wrong in both directions: negative when a guest overpays, and blind to
        the extras an in-house guest has put on the folio, so the dashboard and
        the desk disagreed about what to charge at check-out.

        `folio` is a row from the `folio_states` RPC, or None where the caller
        did not read one. None means "the folio has not been opened", which is
        the correct reading for every booking before arrival.
        """
        purse = money.state(row, money.rows_from_state(folio))
        return ReservationOut(
            folioBalancePaise=(folio or {}).get("balance_paise"),
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
            balancePaise=purse.balance_due_paise,
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
    #
    # Constrained to a uuid because the column is one. Without the pattern a
    # malformed id reached Postgres and came back as a 500 marked
    # `retryable: true` -- and the client that sends these is an offline queue,
    # which would then retry a request that can never succeed, forever, on a
    # receptionist's phone. A 422 stops the queue and says why.
    id: str = Field(
        min_length=36, max_length=36,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    )
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
    # What the room is, for the screens and for the AI employee. Empty is a
    # real and visible answer — the Rooms screen shows what is still missing.
    description: str = ""
    sizeSqft: int | None = None
    amenities: list[str] = []
    photos: list[str] = []
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
            description=row.get("description") or "",
            sizeSqft=row.get("size_sqft"),
            amenities=row.get("amenities") or [],
            photos=row.get("photos") or [],
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
    # The same fact as `refusalReason`, in the hotel's words, and only while
    # the action is actually waiting. `refusalReason` stays exactly as the
    # runtime wrote it — it is the audit trail — and is not what the owner is
    # shown.
    whyWaiting: str | None = None
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
    # The same fact as `refusalReason`, in the hotel's words, and only while
    # the action is actually waiting. `refusalReason` stays exactly as the
    # runtime wrote it — it is the audit trail — and is not what the owner is
    # shown.
    whyWaiting: str | None = None
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


class PlanStateOut(BaseModel):
    """
    The hotel's plan, in one shape.

    Every string here is already written for a hotel owner. No status enum
    reaches a screen — `past_due` is a word for this file and the database, and
    what an owner reads is "Your last payment didn't go through."
    """

    status: str
    headline: str
    detail: str = ""
    planName: str
    pricePaise: int
    billingPeriod: str = "monthly"
    includedCredits: int = 0
    # A one-off bonus on the first paid cycle, configured on the plan row.
    # Zero means there is no offer running, and the screen says nothing.
    signupBonusCredits: int = 0
    signupBonusLabel: str = "Launch bonus"
    currentPeriodEnd: datetime | None = None
    cancelAtPeriodEnd: bool = False
    couponCode: str | None = None
    graceUntil: datetime | None = None
    # Is the AI still answering guests? Not the same as "active" — a failed
    # payment keeps working through its grace period.
    serving: bool = True
    canStart: bool = False


class ChannelUsageOut(BaseModel):
    channel: str
    conversations: int = 0
    credits: int = 0
    # What one conversation costs here today, from the rate card.
    creditsEach: int = 0


class CreditPoolOut(BaseModel):
    kind: str
    # Already in the hotel's words — "Monthly credits", "Launch bonus".
    label: str
    remaining: int


class CreditsSummaryOut(BaseModel):
    """
    Where a hotel's credits came from and what they went on.

    Two halves answering two questions: `remainingByKind` is what the hotel was
    given and still holds, `byChannel` is what it spent and on which guests.
    A single "credits used" number is true and tells nobody anything.
    """

    balance: int = 0
    creditsUsed: int = 0
    conversationsTotal: int = 0
    # Staff assists, campaign copy, reports. Real spend, not guest conversations.
    otherCredits: int = 0
    byChannel: list[ChannelUsageOut] = []
    remainingByKind: list[CreditPoolOut] = []
    days: int = 30


class CouponPreviewOut(BaseModel):
    """What a code is worth, priced by the server against the real plan."""

    code: str
    label: str
    kind: str
    firstPaymentPaise: int
    discountPaise: int
    renewalPaise: int
    freeDays: int = 0
    description: str = ""


class CouponRequest(BaseModel):
    code: str = Field(max_length=32)


class SubscribeRequest(BaseModel):
    # Optional, and validated server-side against `plan_coupons`. There is no
    # price, no plan id and no amount in this request on purpose: a client that
    # can name its own price is a client that will.
    couponCode: str | None = Field(default=None, max_length=32)


class CheckoutOut(BaseModel):
    """Everything Razorpay Checkout needs, and nothing it could be lied to with."""

    razorpaySubscriptionId: str
    razorpayKeyId: str
    planName: str
    amountPaise: int
    firstPaymentPaise: int
    currency: str = "INR"
    couponCode: str | None = None
    couponLabel: str | None = None


class BillingSummaryOut(BaseModel):
    plan: str | None = None
    # What the plan costs, from the plan catalogue.
    #
    # The client used to hard-code 0 here with a comment saying a price belongs
    # to the catalogue and a client that computes one can disagree with the
    # invoice. Both true — and the answer was to *send* it, not to render "₹0
    # per property, billed monthly" on a hotel's own billing screen.
    pricePaise: int = 0
    billingPeriod: str = "monthly"
    # camelCase like every other field in this file. These two were snake_case
    # and the PWA has been reading `periodStart`/`periodEnd` — so the usage
    # screen's "billing period ends" has been rendering an empty date since the
    # endpoint was written. Renamed rather than patched at the client, because
    # the convention stated at the top of this file is the one worth keeping.
    periodStart: date | None = None
    periodEnd: date | None = None
    # ok · low · critical · empty. Drives the banner and nothing else; the
    # numbers behind it are only surfaced in the purchase flow and the export.
    tier: str = "unknown"
    balance: int = 0
    available: int = 0
    groups: list[UsageGroupOut] = []
    # A free period this hotel is inside, if any — a migrated partner's
    # transition year, for instance. Present so the usage screen can say why
    # nothing is being charged instead of showing a plan and leaving the
    # hotelier to wonder when the bill starts.
    freeUntil: date | None = None
    freeReason: str | None = None
    freeDaysLeft: int | None = None


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
    # Everything below arrived with the hotel-OS work and is defaulted, so a
    # client written against the earlier shape keeps working unchanged.
    #
    # `priceUnit` is the one that mattered most: without it, ₹1,500 for an
    # airport pickup could be per person or per vehicle, which is the difference
    # between ₹1,500 and ₹9,000 for a family of six — and an AI quoting it had
    # no way to know it was guessing.
    priceUnit: str = "fixed"
    availability: str = "available"
    opensAt: str | None = None
    closesAt: str | None = None
    maxQuantity: int | None = None
    availableFrom: str | None = None
    availableTo: str | None = None
    photoUrl: str | None = None
    terms: str | None = None
    advanceRequired: bool = False

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
            priceUnit=r.get("price_unit") or "fixed",
            availability=r.get("availability") or "available",
            opensAt=str(r["opens_at"])[:5] if r.get("opens_at") else None,
            closesAt=str(r["closes_at"])[:5] if r.get("closes_at") else None,
            maxQuantity=r.get("max_quantity"),
            availableFrom=str(r["available_from"])[:10] if r.get("available_from") else None,
            availableTo=str(r["available_to"])[:10] if r.get("available_to") else None,
            photoUrl=r.get("photo_url"),
            terms=r.get("terms"),
            advanceRequired=bool(r.get("advance_required")),
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
    priceUnit: Literal[
        "per_person", "per_room", "per_vehicle", "per_trip",
        "per_night", "per_item", "fixed",
    ] = "fixed"
    availability: Literal["available", "on_request", "not_available"] = "available"
    opensAt: str | None = Field(default=None, max_length=8)
    closesAt: str | None = Field(default=None, max_length=8)
    maxQuantity: int | None = Field(default=None, ge=1, le=999)
    availableFrom: str | None = Field(default=None, max_length=10)
    availableTo: str | None = Field(default=None, max_length=10)
    photoUrl: str | None = Field(default=None, max_length=1000)
    terms: str | None = Field(default=None, max_length=2000)
    advanceRequired: bool = False


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

class PublicStayCore(BaseModel):
    """
    What both public views of a booking agree a stranger may see.

    **Masked deliberately.** The booking reference is the whole credential, so
    what it unlocks has to be worth less than the reference is hard to guess:
    enough for a guest to recognise their own booking, not enough to be worth
    guessing at. No email, no full phone, no guest id, no room number, no folio
    detail beyond the balance they will be asked for.

    A base class rather than one view inheriting from the other, because the
    two differ by *subtraction* as well as addition: the check-in form needs the
    registration a guest filed and the booking page must not print it back.
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


class PublicStayOut(PublicStayCore):
    """The check-in view: adds back the registration this guest already filed."""
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


class ConversationSessionOut(BaseModel):
    """
    Where one thread has got to, without reading it.

    **This is the panel beside the transcript, and it is not a summary of the
    transcript.** Every field is composed from rows: the dates and the ids from
    `conversations.guest_profile`, which is written only from tool inputs and
    outputs, and the money from `quotes` / `payment_orders` / `reservations` via
    the same reader `get_booking_status` uses. Nothing here is derived from what
    the model wrote, because a receptionist taking over a thread needs the facts
    the guest is holding us to, not our own prose about them.

    The transcript stays. It is how you find out *why*; this is how you find out
    *where*, and forty messages is an expensive way to learn a check-in date.
    """
    conversationId: str
    channel: str = ""
    # 'ai' or 'human' — who is answering right now.
    handler: str = ""
    guestName: str = ""
    guestHandle: str = ""
    # en / hi / hinglish, as detected and stored. Empty when nobody has said
    # enough for it to be worth guessing.
    language: str = ""
    # browsing → quoted → awaiting_payment → booked, or empty for a thread that
    # has never reached one.
    stage: str = ""
    checkIn: date | None = None
    checkOut: date | None = None
    nights: int | None = None
    units: int | None = None
    adults: int | None = None
    children: int | None = None
    roomTypeName: str = ""
    ratePlanName: str = ""
    # The composed money state — NONE, QUOTE_ACTIVE, PAYMENT_PENDING,
    # PAYMENT_RECEIVED, PAYMENT_FAILED, CONFIRMED, CHECKED_IN, CHECKED_OUT,
    # CANCELLED. The same vocabulary the agent is given, so staff and AI cannot
    # describe the same booking differently.
    bookingState: str = "NONE"
    ref: str = ""
    totalPaise: int = 0
    paidPaise: int = 0
    balancePaise: int = 0
    quoteExpiresAt: datetime | None = None
    bookingUrl: str = ""


class BookingLinkOut(BaseModel):
    """The booking's public link after a rotation. The old one is dead."""
    ref: str
    url: str


class ReceiptLineOut(BaseModel):
    """
    One line of a folio, as a guest may read it.

    `postedByName` is deliberately absent. Which receptionist keyed a charge is
    the hotel's business; the guest's business is what was charged and when.
    """
    postedAt: datetime
    kind: str
    description: str
    method: str = ""
    # Signed as the folio stores it: charges positive, payments negative. Kept
    # signed rather than split into two lists because a receipt that does not
    # add up to the balance is a receipt a guest will argue with.
    amountPaise: int


class BookingPageOut(PublicStayCore):
    """
    A booking as its confirmation link shows it — reference, money, receipt.

    Not `PublicBookingOut`, which already exists further down this file for the
    booking engine's confirm response. Python rebinds a duplicate class name
    without a word, and the route kept validating against the wrong shape until
    a live call complained about a field neither of us had written.

    The parent shape is reached with a six-character reference and stays
    correspondingly thin. This one is reached with a 128-bit token, so it can
    afford to say more: the price breakdown behind the total, every folio line,
    and the cancellation terms the guest agreed to.

    It is still a projection, not a row. No guest id, no room number, no email,
    no internal notes, no staff names, and no `public_token` echoed back — a
    page that reprints its own credential is one screenshot away from sharing
    it.
    """
    grossPaise: int = 0
    discountPaise: int = 0
    taxPaise: int = 0
    bookedAt: datetime | None = None
    # One sentence, not a rules object.
    #
    # `reservations.cancellation_policy` is a jsonb column that every row in
    # production has left at `{}` — nothing has ever written it. The terms a
    # guest is actually held to are the ones the hotelier wrote under
    # `properties.policies.cancellation`, which is what the hotel's own page
    # shows. This field prefers the reservation's snapshot when one exists,
    # because terms at the time of booking beat terms as they stand today, and
    # falls back to the property's. Empty means the hotel has not written any,
    # and the page says exactly that rather than inventing a default.
    cancellationTerms: str = ""
    receipt: list[ReceiptLineOut] = []
    # Absolute, because these two pages live on different hosts and the guest's
    # browser should not have to guess which.
    checkInUrl: str = ""
    # So the page can link back to the hotel it is a booking at.
    propertySlug: str = ""
    # Whether the guest has filed their check-in details, and when — never what
    # they wrote. A confirmation link may be forwarded to whoever is paying, and
    # the passport details of whoever is travelling are not part of the receipt.
    registrationSubmittedAt: datetime | None = None


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

    # What a *page* about this room needs. All optional, because this model is
    # also the availability response, where a slug and a photo are noise.
    slug: str | None = None
    photos: list[str] = []
    # Alt text and captions for those photos, keyed by the URL as it appears
    # above. A separate field rather than a richer `photos`, because this model
    # is also the availability response — where a photo is already noise and an
    # object per photo would be noise with structure.
    #
    # **Every URL in `photos` has an entry, and every entry has an `alt`.** The
    # hotelier writes one or the room's own name does; see `website.photo_alt`.
    # A page that has to invent its own alt text is a page where half the
    # images end up with the hotel's name repeated, which is what a screen
    # reader then reads out eleven times.
    photoMeta: list[PhotoMeta] = []
    sizeSqft: int | None = None
    standardOccupancy: int | None = None
    # The cheapest public rate plan's base price. A "from" figure for a
    # brochure page and an Offer's lowPrice — never a quote. Live money comes
    # from /availability with dates, and only ever from a server-priced quote.
    fromPricePaise: int | None = None
    mealPlan: str | None = None

    # Everything the hotel-OS work made answerable, so the page and the AI say
    # the same thing about the same room.
    #
    # §45 asks for exactly this and it is worth naming the failure it avoids: a
    # page saying "children welcome" while the agent says it will check is two
    # answers to one question, and the guest picks whichever suits them. There
    # is one row underneath both.
    maxAdults: int | None = None
    maxChildren: int | None = None
    childrenAllowed: str | None = None
    bedCount: int | None = None
    extraBed: str | None = None
    extraBedPaise: int | None = None
    view: str | None = None
    smoking: str | None = None
    accessible: str | None = None
    bathroom: str | None = None
    # Cancellation terms, resolved from the same ladder the agent quotes.
    cancellation: dict | None = None


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

    # The structured facts §45 asks the page to be built from — the same rows
    # the agent reads, so the two cannot disagree.
    meals: list[dict] = []
    experiences: list[dict] = []
    # Early arrival and late departure, as bands a page can render as a table
    # and an answer engine can read as a policy.
    arrivalRules: list[dict] = []

    # ── the facts a page about this hotel needs ─────────────────────────────
    #
    # Added when the hotel website moved to server rendering. A booking widget
    # needs a slug and a price; a *page* needs a postal address, a map pin and
    # a sentence about the place — that is the difference between something a
    # guest can transact on and something a search or answer engine can
    # describe. None of it is new data: every field has been on `properties`
    # since it was created.
    address: str | None = None
    state: str | None = None
    country: str | None = None
    pincode: str | None = None
    lat: float | None = None
    lon: float | None = None
    propertyType: str | None = None
    totalRooms: int | None = None
    # Seeded or test data. The page renders and takes bookings exactly as
    # before; what this does is let the template force `noindex` however
    # complete the hotel looks. A demo hotel that passes the quality gate is
    # the dangerous case rather than the safe one — Casa Mandala passed it, and
    # was therefore the one fictional hotel Google was actually being asked to
    # index. See §36.
    isDemo: bool = False
    # When the *page* last changed — the website's publish time, not the
    # property row's. A rate edit is not a content change, and a lastmod that
    # moves for reasons a reader cannot see is one Google learns to ignore.
    updatedAt: str | None = None
    # The hotel's own custom domain, when it has one verified. The canonical
    # lives there rather than on travbuddy.in — a hotel that connected its own
    # address should not have us outranking it for its own name.
    domain: str | None = None


class PublicHotelIndexOut(BaseModel):
    """
    One published hotel, in the shape a sitemap and a quality gate need.

    Deliberately not the full site: this list is read to decide *which* URLs to
    submit, and the decision needs counts rather than content. Every field here
    is a signal in `stayosHotelGate` on the website side, and the gate and the
    sitemap must see the same numbers or they will disagree about the same
    hotel.
    """
    slug: str
    name: str
    city: str | None = None
    updatedAt: str | None = None
    domain: str | None = None
    roomTypes: int = 0
    roomsWithPhotos: int = 0
    photos: int = 0
    hasDescription: bool = False
    hasPolicies: bool = False
    hasLocation: bool = False
    amenities: int = 0


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


class DnsRecordOut(BaseModel):
    """One row of the table the owner copies into their registrar."""

    type: str
    name: str
    value: str
    # Whether the public internet can see it yet. Both records are always
    # returned so a half-finished setup shows *which* half.
    found: bool


class DomainOut(BaseModel):
    # none · pending · verified · error
    status: str = "none"
    domain: str | None = None
    verifiedAt: datetime | None = None
    checkedAt: datetime | None = None
    # Already a sentence for a hotel owner, never a resolver error.
    error: str | None = None
    records: list[DnsRecordOut] = []


class DomainRequest(BaseModel):
    domain: str = Field(max_length=253)


class PublicSiteHostOut(BaseModel):
    """Which hotel a hostname belongs to. Public, and deliberately thin."""

    slug: str
    name: str


class GalleryPhotoOut(BaseModel):
    """One photograph on the hotel's site, as the builder reads it back."""
    url: str
    category: str = "other"
    caption: str | None = None
    alt: str | None = None


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
    # Objects, always — `website.project` normalises the bare strings that older
    # rows hold on the way out, so the builder never has to know both shapes
    # exist. Typed `list[str]` here, this model answered 500 to a save that had
    # already succeeded: the write landed, the response failed validation, and
    # the screen showed an error over a change that was live.
    gallery: list[GalleryPhotoOut] = []
    amenities: list[str] = []
    contactPhone: str | None = None
    contactEmail: str | None = None
    sections: list[dict] = []
    publishedAt: datetime | None = None
    hasUnpublishedChanges: bool = False
    # The hotel's own address, if it has claimed one. `domain` above stays for
    # the one thing it was ever used for — printing where the site is — and
    # this carries the state behind it.
    customDomain: DomainOut = Field(default_factory=DomainOut)
    # What the site has sold in the last 30 days. Real rows, both of them —
    # there is no visitor counter on `/stay/[slug]`, so there is no view count
    # here and no conversion rate built on one.
    directBookings30d: int = 0
    directRevenue30dPaise: int = 0


class GalleryPhotoIn(BaseModel):
    """
    One photograph on the hotel's site, as the builder sends it.

    `alt` and `caption` are both optional and mean different things: a caption
    is prose shown under the photo, alt text is what a screen reader says and
    what a search engine reads. Neither is required, because nobody fills in
    twenty-four alt boxes — the server derives one from the category and the
    hotel's own name. See `services/pms/website.photo_alt`.
    """
    url: str = Field(max_length=500)
    category: str | None = Field(default=None, max_length=32)
    caption: str | None = Field(default=None, max_length=160)
    alt: str | None = Field(default=None, max_length=160)


class WebsiteUpdateRequest(BaseModel):
    template: str | None = None
    headline: str | None = Field(default=None, max_length=200)
    tagline: str | None = Field(default=None, max_length=300)
    about: str | None = Field(default=None, max_length=2000)
    accentColor: str | None = Field(default=None, max_length=32)
    heroImageUrl: str | None = Field(default=None, max_length=500)
    # A photograph is a URL and the words that go with it — a category, a
    # caption, alt text. The bare string is still accepted because that is what
    # every row written before the builder had a gallery holds, and because a
    # client that only has URLs should not have to wrap them.
    gallery: list[GalleryPhotoIn | str] | None = None
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


# ── onboarding ───────────────────────────────────────────────────────────────
# What a hotel owner fills in on their first morning. The vocabulary is theirs:
# a "room type" is what the API calls it and what the screen calls "a kind of
# room you sell", and nothing here mentions a partner, a tenant or a principal.
#
# Money arrives in **rupees** on these three, and only these three. Everywhere
# else in this file it is paise, because everywhere else the number came from
# the server. Here it came from a person typing into a box marked ₹, and asking
# a hotelier to enter 350000 for a ₹3,500 room is how ₹0 rooms get created.

class HotelCreateRequest(BaseModel):
    hotelName: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=2, max_length=80)
    ownerName: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=20)


class HotelCreateOut(BaseModel):
    partnerId: str
    propertyId: str
    slug: str
    name: str
    status: str
    created: bool


class RoomTypeCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    totalUnits: int = Field(ge=1, le=999)
    maxOccupancy: int = Field(default=2, ge=1, le=30)
    code: str | None = Field(default=None, max_length=12)

    # What the room actually is.
    #
    # `room_types` has carried these columns since it was created and nothing
    # ever wrote to them: every room in production has a null description, no
    # amenities and no photos. So an AI employee asked "what's in the Deluxe?"
    # or "photos hain?" had nothing to answer with, and said so — on a real
    # guest conversation, twice, while the guest was trying to buy.
    #
    # Optional, deliberately. A hotel that wants to sell a room tonight should
    # not be blocked from creating one; what the product owes them is asking,
    # and showing them the gap afterwards.
    description: str | None = Field(default=None, max_length=2000)
    bedType: str | None = Field(default=None, max_length=60)
    sizeSqft: int | None = Field(default=None, ge=30, le=20_000)
    amenities: list[str] | None = Field(default=None)
    # URLs the hotel already has — their website, their Instagram. StayOS has
    # no image storage, and pretending otherwise with a half-built uploader
    # would be worse than taking a link that already works.
    photos: list[str] | None = Field(default=None)
    # Optional in the same call, because "a room with no price" is the state
    # this whole module exists to prevent and a second request is a second
    # chance to stop halfway.
    priceRupees: int | None = Field(default=None, ge=1, le=1_000_000)
    mealPlan: Literal["EP", "CP", "MAP", "AP"] = "CP"


class RoomTypeUpdateRequest(BaseModel):
    """
    Editing a room that already exists.

    **There was no way to do this at all**, and the consequence was not
    theoretical: every one of the 21 room types in production was created
    before the form asked for a description, amenities or photos, so every one
    of them had none — and no route existed that could add them. The AI
    employee that now knows how to send room photos had nothing to send, and
    the hotel had no way to fix that short of deleting the room and losing its
    inventory with it.

    Every field is optional and `None` means "leave it alone", which is what
    makes this safe to call from a screen that edits one thing. Clearing a
    field is an explicit empty value — `[]` for a list, `""` for text — because
    a PATCH that could not distinguish "unchanged" from "cleared" would empty a
    hotel's description the first time somebody renamed a room.
    """

    name: str | None = Field(default=None, min_length=2, max_length=80)
    totalUnits: int | None = Field(default=None, ge=1, le=999)
    maxOccupancy: int | None = Field(default=None, ge=1, le=30)
    description: str | None = Field(default=None, max_length=2000)
    bedType: str | None = Field(default=None, max_length=60)
    sizeSqft: int | None = Field(default=None, ge=30, le=20_000)
    amenities: list[str] | None = Field(default=None)
    photos: list[str] | None = Field(default=None)
    isActive: bool | None = Field(default=None)


class ExtractedRoomType(BaseModel):
    """One room a model read out of a hotel's own material. Nothing is required
    except a name — "the brochure does not say" is the answer this whole flow
    most needs to be able to carry."""

    name: str
    totalUnits: int | None = None
    maxOccupancy: int | None = None
    bedType: str | None = None
    sizeSqft: int | None = None
    description: str | None = None
    amenities: list[str] = []
    priceRupees: int | None = None
    mealPlan: str | None = None


class ExtractedHotel(BaseModel):
    name: str | None = None
    # The street address is the field that unlocks the map pin: address →
    # geocode → lat/lon → "how far are you from the station?" and a hotel page
    # that can be placed on a map. It was missing from the first version of the
    # extractor, which is how a brochure carrying a full postal address still
    # left a hotel with no location.
    address: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    description: str | None = None
    amenities: list[str] = []
    checkInTime: str | None = None
    checkOutTime: str | None = None
    policies: dict = {}
    meals: dict = {}


class HotelExtractOut(BaseModel):
    """
    A draft, and what to tell the owner it found.

    **This is not saved.** The extractor writes nothing; a person reads `found`,
    edits what is wrong, and posts the rooms they approve back through the
    ordinary creation route. A model that misreads "₹4,200 for two nights" as a
    nightly rate has proposed a mistake rather than made one.
    """

    confidence: float
    hotel: ExtractedHotel
    roomTypes: list[ExtractedRoomType]
    found: list[str]
    droppedRooms: int = 0


class HotelCorrectionOut(HotelExtractOut):
    """
    The same draft, corrected, plus what actually moved.

    `changes` is not decoration. A hotelier who says "isme breakfast included
    nahi hai" and is handed back a silently different draft has been asked to
    trust a model twice; a line saying *Deluxe Balcony: meal plan breakfast →
    room only* is a thing they can check in a second. `unclear` appears only
    when nothing moved — a question beside a visible change is noise.
    """
    changes: list[str] = []
    unclear: str | None = None


class HotelCorrectionRequest(BaseModel):
    """
    The draft on the hotelier's screen, and their sentence about it.

    The draft comes back from the client rather than from a server-side session
    because nothing about the extraction is stored — that is the safety
    argument, and keeping a draft on the server to make this route tidier would
    quietly undo it. Anything a client sends here is bounded and re-normalised
    on the way out, so a tampered draft can only produce a draft the same
    hotelier must still approve.
    """
    draft: HotelExtractOut
    correction: str = Field(min_length=2, max_length=2000)


class HotelExtractRequest(BaseModel):
    text: str = Field(min_length=20, max_length=60_000)


class PropertyUpdateRequest(BaseModel):
    """
    The hotel's own facts — where it is, when it opens, what its rules are.

    Every one of these was reachable only by editing Postgres by hand. That is
    why nine of ten live hotels have no address, no coordinates and empty
    policies, and therefore why their AI cannot answer "where are you?" and
    their website cannot be placed on a map.

    Same PATCH contract as the room route: an absent key is unchanged, an empty
    value clears. `policies` is the exception and it is worth stating — it is
    merged key by key rather than replaced, because the object also carries
    commercial settings (`token_pct`, `max_discount_pct`) that the pricing
    engine reads and no policy screen has any business overwriting.
    """

    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=2)
    pincode: str | None = Field(default=None, max_length=12)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    propertyType: Literal[
        "hotel", "resort", "homestay", "hostel", "boutique", "guesthouse", "villa"
    ] | None = None
    totalRooms: int | None = Field(default=None, ge=1, le=5000)
    checkInTime: str | None = Field(default=None, max_length=5)
    checkOutTime: str | None = Field(default=None, max_length=5)
    policies: dict[str, str] | None = None


class PropertyLocationOut(BaseModel):
    """
    A map pin we think is this hotel — **proposed, not saved.**

    Same discipline as the brochure importer: a geocoder that writes straight to
    the row is a geocoder whose one confident mistake puts a hotel in the wrong
    town on its own website, in its own structured data, and in every distance
    the AI quotes. The owner sees the address we matched and agrees to it.
    """
    found: bool
    lat: float | None = None
    lon: float | None = None
    formattedAddress: str | None = None
    # Said plainly rather than left to a false `found`. "We could not look this
    # up" and "there is no such place" are different problems for a hotelier.
    unavailable: bool = False


class ReadinessGap(BaseModel):
    id: str
    label: str
    detail: str
    # 'agent' — the AI cannot answer a question without it.
    # 'website' — the page will not be submitted for indexing without it.
    affects: list[str]


class ReadinessOut(BaseModel):
    """
    What is missing, named — not scored.
    
    A percentage tells a hotelier they are at 62% and nothing about what to do
    next. A sentence — "your AI can't tell guests where you are" — is one tap
    from being fixed, which is the only number that matters.
    """
    agentReady: bool
    websiteIndexable: bool
    gaps: list[ReadinessGap] = []


class MediaUploadOut(BaseModel):
    url: str
    path: str


class RatePlanCreateRequest(BaseModel):
    roomTypeId: str
    priceRupees: int = Field(ge=1, le=1_000_000)
    name: str | None = Field(default=None, max_length=80)
    mealPlan: Literal["EP", "CP", "MAP", "AP"] = "CP"
    audience: Literal["public", "b2b", "corporate", "creator", "internal"] = "public"
    code: str | None = Field(default=None, max_length=16)


class RoomsCreateRequest(BaseModel):
    roomTypeId: str
    numbers: list[str] = Field(min_length=1, max_length=200)


class OnboardingStepOut(BaseModel):
    id: str
    label: str
    done: bool
    required: bool
    href: str


class OnboardingCountsOut(BaseModel):
    roomTypes: int
    ratePlans: int
    rooms: int
    agents: int


class OnboardingOut(BaseModel):
    propertyId: str
    name: str
    status: str
    slug: str
    steps: list[OnboardingStepOut]
    remaining: int
    canGoLive: bool
    isLive: bool
    counts: OnboardingCountsOut


# ── paying for a stay that already exists ────────────────────────────────────

class StayPaymentOut(BaseModel):
    """
    What the pay page shows before it opens Checkout.

    Deliberately thin. A payment link is a URL a guest may forward, paste into
    a group chat or open on a shared phone, so this names the hotel, the stay
    and the amount — enough to know what is being paid for — and nothing that
    would be a leak if the wrong person opened it. No phone number, no email,
    no room number, no folio.
    """

    orderId: str
    hotelName: str
    guestName: str
    ref: str
    amountPaise: int
    currency: str = "INR"
    status: str
    razorpayKeyId: str | None = None
    checkIn: str | None = None
    checkOut: str | None = None


class StayPaymentConfirmRequest(BaseModel):
    razorpayOrderId: str
    razorpayPaymentId: str
    razorpaySignature: str


class PaymentLinkRequest(BaseModel):
    """
    Ask a guest for money they already owe.

    `amountRupees` is for a deposit or a part payment. Left out — which is the
    ordinary case — the server uses the whole outstanding balance, read from
    the reservation. There is no field for the total, because a desk that could
    name an amount is a desk that could name the wrong one.
    """

    amountRupees: int | None = Field(default=None, ge=1)


class PaymentLinkOut(BaseModel):
    reservationId: str
    ref: str
    orderId: str
    amountPaise: int
    balancePaise: int
    payUrl: str


# ── the AI employee on the hotel's website ───────────────────────────────────


class WebChatIn(BaseModel):
    """
    One message from a visitor on a hotel's public page.

    `session` is the opaque token the previous response handed back. Absent on
    the first message of a conversation and echoed unchanged after that; the
    client never constructs one and cannot read what is inside it.
    """

    message: str = Field(min_length=1, max_length=1000)
    session: str | None = Field(default=None, max_length=600)


class WebChatOut(BaseModel):
    reply: str
    session: str
    # True once a person at the hotel owns the thread. The widget stops
    # promising an instant answer, because there will not be one.
    handedOff: bool = False


class WebChatOpenerOut(BaseModel):
    """What the widget shows before anyone has typed."""

    greeting: str
    suggestions: list[str] = []
