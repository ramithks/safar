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

from pydantic import BaseModel, Field


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
    discountReason: str | None = None
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
    discountReason: str | None = None
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
    source: str
    status: str
    paymentStatus: str
    notes: str | None = None
    createdAt: datetime

    @staticmethod
    def from_row(row: dict) -> "ReservationOut":
        return ReservationOut(
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
