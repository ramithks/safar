"""
Shapes for the hotel operating system.

Kept out of `pms/schemas.py`, which is 2,579 lines and holds the shapes of the
things a hotel *does* — reservations, folios, availability, payments. These are
the shapes of what a hotel *is*: its rooms in full, its rules, its team, its
marketing. A file that held both would be one nobody could find anything in.

Two conventions carried over from that file rather than reinvented, because
every client already speaks them:

**camelCase out, snake_case in the database.** Every `from_row` does the
translation in one place, so no endpoint spells a column name into a response.

**Rupees on the way in, paise everywhere else.** A hotelier types 3500 and the
server multiplies. A client that converts is a client that can disagree with an
invoice by a factor of a hundred — which is why `priceRupees` exists on requests
and `pricePaise` on responses, and never the other way round.
"""
from datetime import date, time

from pydantic import BaseModel, Field, field_validator

# ── shared vocabularies ──────────────────────────────────────────────────────
#
# Mirrors of the database's own check constraints. Duplicated deliberately: a
# 422 that names the four words a field accepts is a usable error, and a 500
# from a constraint violation at the end of a ten-step wizard is not.

AVAILABILITY = ("available", "on_request", "none")
SERVICE_AVAILABILITY = ("available", "on_request", "not_available")
PRICE_UNITS = ("per_person", "per_room", "per_vehicle", "per_trip", "per_night", "per_item", "fixed")
TEAM_ROLES = (
    "owner", "manager", "front_desk", "reservations",
    "sales", "accounts", "marketing", "operations", "other",
)
NOTIFICATION_EVENTS = (
    "booking_created", "payment_received", "payment_failed", "group_inquiry",
    "negotiation_approval", "special_request", "transfer_request", "cancellation",
    "refund", "high_value_booking", "vip_guest", "unanswered_conversation",
    "agent_escalation",
)
CHANNELS = ("whatsapp", "email", "dashboard")


def _rupees_to_paise(value: float | int | None) -> int | None:
    """
    One conversion, and it rounds.

    `int(3500.1 * 100)` is 350009 on some inputs, because binary floating point
    does not hold a tenth. Every rupee amount in this file goes through here.
    """
    if value is None:
        return None
    return round(float(value) * 100)


# ── room types ───────────────────────────────────────────────────────────────


class PhotoMeta(BaseModel):
    url: str
    caption: str | None = None
    alt: str | None = None


class RoomTypeDetailOut(BaseModel):
    """
    A room type in full, including whether it may be sold.

    `sellable` and `readyToSell` are two different questions and both are here.
    See services/pms/readiness.py — the short version is that every room in
    production is the first and none is yet the second, and conflating them
    would have taken the live inventory off sale.
    """

    id: str
    code: str
    name: str
    description: str | None
    totalUnits: int
    maxOccupancy: int
    standardOccupancy: int
    maxAdults: int | None
    maxChildren: int | None
    childrenAllowed: str | None
    bedType: str | None
    bedCount: int | None
    extraBed: str | None
    extraBedPaise: int | None
    crib: str | None
    cribPaise: int | None
    sizeSqft: int | None
    floor: str | None
    view: str | None
    smoking: str | None
    accessible: str | None
    connecting: str | None
    bathroom: str | None
    amenities: list[str]
    photos: list[PhotoMeta]
    isActive: bool
    sortOrder: int
    sellable: bool = True
    readyToSell: bool = False
    missing: list[dict] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict, readiness: dict | None = None) -> "RoomTypeDetailOut":
        meta = row.get("photo_meta") or {}
        return cls(
            id=str(row["id"]),
            code=row.get("code") or "",
            name=row.get("name") or "",
            description=row.get("description"),
            totalUnits=int(row.get("total_units") or 0),
            maxOccupancy=int(row.get("max_occupancy") or 0),
            standardOccupancy=int(row.get("standard_occupancy") or 0),
            maxAdults=row.get("max_adults"),
            maxChildren=row.get("max_children"),
            childrenAllowed=row.get("children_allowed"),
            bedType=row.get("bed_type"),
            bedCount=row.get("bed_count"),
            extraBed=row.get("extra_bed"),
            extraBedPaise=row.get("extra_bed_paise"),
            crib=row.get("crib"),
            cribPaise=row.get("crib_paise"),
            sizeSqft=row.get("size_sqft"),
            floor=row.get("floor"),
            view=row.get("view"),
            smoking=row.get("smoking"),
            accessible=row.get("accessible"),
            connecting=row.get("connecting"),
            bathroom=row.get("bathroom"),
            amenities=row.get("amenities") or [],
            # The array is the source of truth for which photos exist and in
            # what order; the metadata is looked up beside it. A photo with no
            # entry is a photo with no caption, not a missing photo.
            photos=[
                PhotoMeta(url=url, **{k: v for k, v in (meta.get(url) or {}).items()
                                      if k in {"caption", "alt"}})
                for url in (row.get("photos") or [])
            ],
            isActive=bool(row.get("is_active", True)),
            sortOrder=int(row.get("sort_order") or 0),
            sellable=bool((readiness or {}).get("sellable", True)),
            readyToSell=bool((readiness or {}).get("readyToSell", False)),
            missing=(readiness or {}).get("missing") or [],
        )


class RoomTypeDetailRequest(BaseModel):
    """
    The full room editor's payload. Every field optional; absent means unchanged.

    That is the same PATCH contract `RoomTypeUpdateRequest` already established,
    and it is what lets a ten-step wizard save one step at a time without step
    seven wiping step two. An *empty* value still clears — `description: ""`
    means the hotelier deleted it — and only the absence of a key means silence.
    """

    name: str | None = Field(None, min_length=2, max_length=120)
    description: str | None = Field(None, max_length=2000)
    totalUnits: int | None = Field(None, ge=1, le=999)
    maxOccupancy: int | None = Field(None, ge=1, le=30)
    standardOccupancy: int | None = Field(None, ge=1, le=30)
    maxAdults: int | None = Field(None, ge=1, le=30)
    maxChildren: int | None = Field(None, ge=0, le=30)
    childrenAllowed: str | None = None
    bedType: str | None = Field(None, max_length=60)
    bedCount: int | None = Field(None, ge=1, le=20)
    extraBed: str | None = None
    extraBedRupees: float | None = Field(None, ge=0, le=1_000_000)
    crib: str | None = None
    cribRupees: float | None = Field(None, ge=0, le=1_000_000)
    sizeSqft: int | None = Field(None, ge=1, le=100_000)
    floor: str | None = Field(None, max_length=40)
    view: str | None = Field(None, max_length=60)
    smoking: str | None = None
    accessible: str | None = None
    connecting: str | None = None
    bathroom: str | None = None
    amenities: list[str] | None = None
    photos: list[str] | None = None
    photoMeta: dict[str, dict] | None = None
    isActive: bool | None = None

    @field_validator("childrenAllowed")
    @classmethod
    def _children(cls, v):
        if v is not None and v not in ("allowed", "not_allowed", "above_age"):
            raise ValueError("Say allowed, not_allowed or above_age.")
        return v

    @field_validator("extraBed", "crib")
    @classmethod
    def _availability(cls, v):
        if v is not None and v not in AVAILABILITY:
            raise ValueError(f"Say one of: {', '.join(AVAILABILITY)}.")
        return v

    @field_validator("smoking")
    @classmethod
    def _smoking(cls, v):
        if v is not None and v not in ("smoking", "non_smoking"):
            raise ValueError("Say smoking or non_smoking.")
        return v

    @field_validator("accessible", "connecting")
    @classmethod
    def _yes_no(cls, v):
        if v is not None and v not in ("yes", "no"):
            raise ValueError("Say yes or no.")
        return v

    @field_validator("bathroom")
    @classmethod
    def _bathroom(cls, v):
        if v is not None and v not in ("private", "shared"):
            raise ValueError("Say private or shared.")
        return v

    def to_columns(self) -> dict:
        """
        The payload as database columns, with absent keys genuinely absent.

        `exclude_unset` rather than `exclude_none` is the whole trick: a client
        sending `"description": null` means *clear it*, and a client not sending
        the key at all means *leave it*. `exclude_none` cannot tell those apart
        and would silently make the first one impossible.
        """
        sent = self.model_dump(exclude_unset=True)
        mapping = {
            "name": "name", "description": "description", "totalUnits": "total_units",
            "maxOccupancy": "max_occupancy", "standardOccupancy": "standard_occupancy",
            "maxAdults": "max_adults", "maxChildren": "max_children",
            "childrenAllowed": "children_allowed", "bedType": "bed_type",
            "bedCount": "bed_count", "extraBed": "extra_bed", "crib": "crib",
            "sizeSqft": "size_sqft", "floor": "floor", "view": "view",
            "smoking": "smoking", "accessible": "accessible", "connecting": "connecting",
            "bathroom": "bathroom", "amenities": "amenities", "photos": "photos",
            "photoMeta": "photo_meta", "isActive": "is_active",
        }
        out = {column: sent[key] for key, column in mapping.items() if key in sent}
        if "extraBedRupees" in sent:
            out["extra_bed_paise"] = _rupees_to_paise(sent["extraBedRupees"])
        if "cribRupees" in sent:
            out["crib_paise"] = _rupees_to_paise(sent["cribRupees"])
        return out


# ── stay time rules ──────────────────────────────────────────────────────────


class StayTimeRuleOut(BaseModel):
    id: str
    kind: str
    untilTime: str
    chargeKind: str
    amount: int
    subjectToAvailability: bool
    note: str | None
    isActive: bool

    @classmethod
    def from_row(cls, row: dict) -> "StayTimeRuleOut":
        return cls(
            id=str(row["id"]),
            kind=row["kind"],
            untilTime=str(row["until_time"])[:5],
            chargeKind=row["charge_kind"],
            amount=int(row.get("amount") or 0),
            subjectToAvailability=bool(row.get("subject_to_availability", True)),
            note=row.get("note"),
            isActive=bool(row.get("is_active", True)),
        )


class StayTimeRuleRequest(BaseModel):
    id: str | None = None
    kind: str
    untilTime: time
    chargeKind: str
    # Rupees for a fixed charge, a whole percentage for pct_of_night. Two
    # meanings on one field is normally a smell; here it mirrors the column and
    # keeping them apart would mean two nullable fields and a rule about which
    # one to read.
    amount: float = Field(0, ge=0)
    subjectToAvailability: bool = True
    note: str | None = Field(None, max_length=300)
    isActive: bool = True

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        if v not in ("early_in", "late_out"):
            raise ValueError("Say early_in or late_out.")
        return v

    @field_validator("chargeKind")
    @classmethod
    def _charge(cls, v):
        if v not in ("free", "fixed", "pct_of_night", "unavailable"):
            raise ValueError("Say free, fixed, pct_of_night or unavailable.")
        return v

    def to_columns(self) -> dict:
        amount = (
            round(self.amount) if self.chargeKind == "pct_of_night"
            else _rupees_to_paise(self.amount) or 0
        )
        return {
            "kind": self.kind,
            "until_time": self.untilTime.strftime("%H:%M:%S"),
            "charge_kind": self.chargeKind,
            "amount": max(0, amount),
            "subject_to_availability": self.subjectToAvailability,
            "note": self.note,
            "is_active": self.isActive,
        }


# ── meals ────────────────────────────────────────────────────────────────────


class MealOptionOut(BaseModel):
    meal: str
    availability: str
    pricePaise: int | None
    priceUnit: str
    startsAt: str | None
    endsAt: str | None
    diet: str | None
    serviceStyle: str | None
    cuisine: str | None
    note: str | None

    @classmethod
    def from_row(cls, row: dict) -> "MealOptionOut":
        return cls(
            meal=row["meal"],
            availability=row.get("availability") or "available",
            pricePaise=row.get("price_paise"),
            priceUnit=row.get("price_unit") or "per_person",
            startsAt=str(row["starts_at"])[:5] if row.get("starts_at") else None,
            endsAt=str(row["ends_at"])[:5] if row.get("ends_at") else None,
            diet=row.get("diet"),
            serviceStyle=row.get("service_style"),
            cuisine=row.get("cuisine"),
            note=row.get("note"),
        )


class MealOptionRequest(BaseModel):
    meal: str
    availability: str = "available"
    priceRupees: float | None = Field(None, ge=0, le=100_000)
    priceUnit: str = "per_person"
    startsAt: time | None = None
    endsAt: time | None = None
    diet: str | None = None
    serviceStyle: str | None = None
    cuisine: str | None = Field(None, max_length=120)
    note: str | None = Field(None, max_length=500)

    @field_validator("meal")
    @classmethod
    def _meal(cls, v):
        if v not in ("breakfast", "lunch", "dinner"):
            raise ValueError("Say breakfast, lunch or dinner.")
        return v

    @field_validator("availability")
    @classmethod
    def _availability(cls, v):
        if v not in SERVICE_AVAILABILITY:
            raise ValueError(f"Say one of: {', '.join(SERVICE_AVAILABILITY)}.")
        return v

    def to_columns(self) -> dict:
        return {
            "meal": self.meal,
            "availability": self.availability,
            "price_paise": _rupees_to_paise(self.priceRupees),
            "price_unit": self.priceUnit,
            "starts_at": self.startsAt.strftime("%H:%M:%S") if self.startsAt else None,
            "ends_at": self.endsAt.strftime("%H:%M:%S") if self.endsAt else None,
            "diet": self.diet,
            "service_style": self.serviceStyle,
            "cuisine": self.cuisine,
            "note": self.note,
        }


# ── negotiation ──────────────────────────────────────────────────────────────


class NegotiationRuleOut(BaseModel):
    """
    A hotel's own negotiating rules, read back to the hotel.

    **This is the one projection that carries the floor**, and it is the reason
    `negotiation_rules` has a `rates`-scoped read policy rather than the usual
    `owns_partner`. A receptionist who knows the floor will eventually say it.
    Nothing derived from this shape ever reaches an agent tool or a guest.
    """

    id: str
    scope: str
    roomTypeId: str | None
    ratePlanId: str | None
    enabled: bool
    floorPct: int | None
    floorPaise: int | None
    targetPct: int | None
    targetPaise: int | None
    ceilingPaise: int | None
    autoPct: int | None
    approvalPct: int | None
    validFrom: date | None
    validTo: date | None
    daysOfWeek: list[int]
    blackoutDates: list[str]
    minNights: int
    minUnits: int
    note: str | None

    @classmethod
    def from_row(cls, row: dict) -> "NegotiationRuleOut":
        return cls(
            id=str(row["id"]),
            scope=row["scope"],
            roomTypeId=str(row["room_type_id"]) if row.get("room_type_id") else None,
            ratePlanId=str(row["rate_plan_id"]) if row.get("rate_plan_id") else None,
            enabled=bool(row.get("enabled", True)),
            floorPct=row.get("floor_pct"),
            floorPaise=row.get("floor_paise"),
            targetPct=row.get("target_pct"),
            targetPaise=row.get("target_paise"),
            ceilingPaise=row.get("ceiling_paise"),
            autoPct=row.get("auto_pct"),
            approvalPct=row.get("approval_pct"),
            validFrom=row.get("valid_from"),
            validTo=row.get("valid_to"),
            daysOfWeek=[int(d) for d in (row.get("days_of_week") or [])],
            blackoutDates=[str(d)[:10] for d in (row.get("blackout_dates") or [])],
            minNights=int(row.get("min_nights") or 1),
            minUnits=int(row.get("min_units") or 1),
            note=row.get("note"),
        )


class NegotiationRuleRequest(BaseModel):
    scope: str
    roomTypeId: str | None = None
    ratePlanId: str | None = None
    enabled: bool = True
    floorPct: int | None = Field(None, ge=0, le=100)
    floorRupees: float | None = Field(None, ge=0, le=10_000_000)
    targetPct: int | None = Field(None, ge=0, le=100)
    targetRupees: float | None = Field(None, ge=0, le=10_000_000)
    ceilingRupees: float | None = Field(None, ge=0, le=10_000_000)
    autoPct: int | None = Field(None, ge=0, le=100)
    approvalPct: int | None = Field(None, ge=0, le=100)
    validFrom: date | None = None
    validTo: date | None = None
    daysOfWeek: list[int] = Field(default_factory=list)
    blackoutDates: list[date] = Field(default_factory=list)
    minNights: int = Field(1, ge=1, le=90)
    minUnits: int = Field(1, ge=1, le=200)
    note: str | None = Field(None, max_length=300)

    @field_validator("scope")
    @classmethod
    def _scope(cls, v):
        if v not in ("property", "room_type", "rate_plan"):
            raise ValueError("Say property, room_type or rate_plan.")
        return v

    @field_validator("daysOfWeek")
    @classmethod
    def _days(cls, v):
        # ISO: 1 = Monday … 7 = Sunday. Stated in the error because the other
        # convention (0 = Sunday) is common enough that a caller using it would
        # otherwise silently shift every rule by a day.
        if any(d < 1 or d > 7 for d in v):
            raise ValueError("Days are 1 (Monday) to 7 (Sunday).")
        return sorted(set(v))

    def to_columns(self) -> dict:
        return {
            "scope": self.scope,
            "room_type_id": self.roomTypeId if self.scope == "room_type" else None,
            "rate_plan_id": self.ratePlanId if self.scope == "rate_plan" else None,
            "enabled": self.enabled,
            "floor_pct": self.floorPct,
            "floor_paise": _rupees_to_paise(self.floorRupees),
            "target_pct": self.targetPct,
            "target_paise": _rupees_to_paise(self.targetRupees),
            "ceiling_paise": _rupees_to_paise(self.ceilingRupees),
            "auto_pct": self.autoPct,
            "approval_pct": self.approvalPct,
            "valid_from": self.validFrom.isoformat() if self.validFrom else None,
            "valid_to": self.validTo.isoformat() if self.validTo else None,
            "days_of_week": self.daysOfWeek,
            "blackout_dates": [d.isoformat() for d in self.blackoutDates],
            "min_nights": self.minNights,
            "min_units": self.minUnits,
            "note": self.note,
        }


class NegotiationPreviewRequest(BaseModel):
    """
    "What would my AI say if a guest asked for this?"

    A hotelier cannot be asked to trust a floor they cannot test. This runs the
    real engine against the real calendar — not a simulation of it — so what the
    dashboard shows and what a guest is told on Instagram at 2am come from the
    same function.
    """

    ratePlanId: str
    checkIn: date
    checkOut: date
    units: int = Field(1, ge=1, le=200)
    requestedNightlyRupees: float | None = Field(None, ge=0, le=10_000_000)


class NegotiationPreviewOut(BaseModel):
    verdict: str
    reason: str
    publishedTotalPaise: int
    publishedNightlyPaise: int
    finalTotalPaise: int | None = None
    finalNightlyPaise: int | None = None
    counterTotalPaise: int | None = None
    counterNightlyPaise: int | None = None
    approvalAvailable: bool = False
    nights: int
    units: int
    # Only on this route, and only for the hotel's own screen: the sentence the
    # agent would build. Shown so a hotelier reads what a guest would read
    # rather than a verdict code.
    wouldSay: str


# ── group pricing ────────────────────────────────────────────────────────────


class GroupTierOut(BaseModel):
    id: str
    roomTypeId: str | None
    minUnits: int
    maxUnits: int | None
    discountPct: int | None
    fixedPricePaise: int | None
    negotiable: bool
    isActive: bool

    @classmethod
    def from_row(cls, row: dict) -> "GroupTierOut":
        return cls(
            id=str(row["id"]),
            roomTypeId=str(row["room_type_id"]) if row.get("room_type_id") else None,
            minUnits=int(row["min_units"]),
            maxUnits=row.get("max_units"),
            discountPct=row.get("discount_pct"),
            fixedPricePaise=row.get("fixed_price_paise"),
            negotiable=bool(row.get("negotiable")),
            isActive=bool(row.get("is_active", True)),
        )


class GroupTierRequest(BaseModel):
    id: str | None = None
    roomTypeId: str | None = None
    minUnits: int = Field(..., ge=1, le=500)
    maxUnits: int | None = Field(None, ge=1, le=500)
    discountPct: int | None = Field(None, ge=0, le=100)
    fixedPriceRupees: float | None = Field(None, ge=0, le=10_000_000)
    negotiable: bool = False
    isActive: bool = True

    def to_columns(self) -> dict:
        return {
            "room_type_id": self.roomTypeId,
            "min_units": self.minUnits,
            "max_units": self.maxUnits,
            "discount_pct": self.discountPct,
            "fixed_price_paise": _rupees_to_paise(self.fixedPriceRupees),
            "negotiable": self.negotiable,
            "is_active": self.isActive,
        }


class GroupPolicyOut(BaseModel):
    minUnits: int
    depositPct: int | None
    paymentDueDays: int | None
    cancellation: dict
    mealPackage: str | None
    blackoutDates: list[str]
    terms: str | None

    @classmethod
    def from_row(cls, row: dict | None) -> "GroupPolicyOut":
        row = row or {}
        return cls(
            minUnits=int(row.get("min_units") or 3),
            depositPct=row.get("deposit_pct"),
            paymentDueDays=row.get("payment_due_days"),
            cancellation=row.get("cancellation") or {},
            mealPackage=row.get("meal_package"),
            blackoutDates=[str(d)[:10] for d in (row.get("blackout_dates") or [])],
            terms=row.get("terms"),
        )


class GroupPolicyRequest(BaseModel):
    minUnits: int = Field(3, ge=1, le=500)
    depositPct: int | None = Field(None, ge=0, le=100)
    paymentDueDays: int | None = Field(None, ge=0, le=365)
    cancellation: dict | None = None
    mealPackage: str | None = Field(None, max_length=200)
    blackoutDates: list[date] = Field(default_factory=list)
    terms: str | None = Field(None, max_length=2000)

    def to_columns(self) -> dict:
        return {
            "min_units": self.minUnits,
            "deposit_pct": self.depositPct,
            "payment_due_days": self.paymentDueDays,
            "cancellation": self.cancellation or {},
            "meal_package": self.mealPackage,
            "blackout_dates": [d.isoformat() for d in self.blackoutDates],
            "terms": self.terms,
        }


# ── services and experiences ─────────────────────────────────────────────────


class ServiceDetailRequest(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=2, max_length=120)
    description: str | None = Field(None, max_length=1000)
    category: str = "other"
    priceRupees: float = Field(0, ge=0, le=10_000_000)
    priceUnit: str = "fixed"
    availability: str = "available"
    opensAt: time | None = None
    closesAt: time | None = None
    maxQuantity: int | None = Field(None, ge=1, le=999)
    availableFrom: date | None = None
    availableTo: date | None = None
    photoUrl: str | None = Field(None, max_length=1000)
    terms: str | None = Field(None, max_length=2000)
    advanceRequired: bool = False
    dailyCapacity: int | None = Field(None, ge=0, le=9999)
    leadTimeHours: int = Field(0, ge=0, le=8760)
    isActive: bool = True

    @field_validator("priceUnit")
    @classmethod
    def _unit(cls, v):
        if v not in PRICE_UNITS:
            raise ValueError(f"Say one of: {', '.join(PRICE_UNITS)}.")
        return v

    @field_validator("availability")
    @classmethod
    def _availability(cls, v):
        if v not in SERVICE_AVAILABILITY:
            raise ValueError(f"Say one of: {', '.join(SERVICE_AVAILABILITY)}.")
        return v

    def to_columns(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "price_paise": _rupees_to_paise(self.priceRupees) or 0,
            "price_unit": self.priceUnit,
            "availability": self.availability,
            "opens_at": self.opensAt.strftime("%H:%M:%S") if self.opensAt else None,
            "closes_at": self.closesAt.strftime("%H:%M:%S") if self.closesAt else None,
            "max_quantity": self.maxQuantity,
            "available_from": self.availableFrom.isoformat() if self.availableFrom else None,
            "available_to": self.availableTo.isoformat() if self.availableTo else None,
            "photo_url": self.photoUrl,
            "terms": self.terms,
            "advance_required": self.advanceRequired,
            "daily_capacity": self.dailyCapacity,
            "lead_time_hours": self.leadTimeHours,
            "is_active": self.isActive,
        }


class ServiceDetailOut(BaseModel):
    id: str
    name: str
    description: str | None
    category: str
    pricePaise: int
    priceUnit: str
    availability: str
    opensAt: str | None
    closesAt: str | None
    maxQuantity: int | None
    availableFrom: date | None
    availableTo: date | None
    photoUrl: str | None
    terms: str | None
    advanceRequired: bool
    dailyCapacity: int | None
    leadTimeHours: int
    isActive: bool

    @classmethod
    def from_row(cls, row: dict) -> "ServiceDetailOut":
        return cls(
            id=str(row["id"]),
            name=row.get("name") or "",
            description=row.get("description"),
            category=row.get("category") or "other",
            pricePaise=int(row.get("price_paise") or 0),
            priceUnit=row.get("price_unit") or "fixed",
            availability=row.get("availability") or "available",
            opensAt=str(row["opens_at"])[:5] if row.get("opens_at") else None,
            closesAt=str(row["closes_at"])[:5] if row.get("closes_at") else None,
            maxQuantity=row.get("max_quantity"),
            availableFrom=row.get("available_from"),
            availableTo=row.get("available_to"),
            photoUrl=row.get("photo_url"),
            terms=row.get("terms"),
            advanceRequired=bool(row.get("advance_required")),
            dailyCapacity=row.get("daily_capacity"),
            leadTimeHours=int(row.get("lead_time_hours") or 0),
            isActive=bool(row.get("is_active", True)),
        )


class ExperienceRequest(BaseModel):
    id: str | None = None
    title: str = Field(..., min_length=2, max_length=160)
    description: str | None = Field(None, max_length=3000)
    category: str = "other"
    durationMinutes: int | None = Field(None, ge=1, le=10_080)
    priceRupees: float = Field(0, ge=0, le=10_000_000)
    priceUnit: str = "per_person"
    meetingPoint: str | None = Field(None, max_length=300)
    startsAt: time | None = None
    minParticipants: int = Field(1, ge=1, le=500)
    maxParticipants: int | None = Field(None, ge=1, le=500)
    minAge: int | None = Field(None, ge=0, le=120)
    maxAge: int | None = Field(None, ge=0, le=120)
    requirements: str | None = Field(None, max_length=1000)
    availability: str = "available"
    availableFrom: date | None = None
    availableTo: date | None = None
    daysOfWeek: list[int] = Field(default_factory=list)
    cancellation: dict | None = None
    photos: list[str] = Field(default_factory=list)
    photoMeta: dict[str, dict] | None = None
    leadTimeHours: int = Field(0, ge=0, le=8760)
    dailyCapacity: int | None = Field(None, ge=0, le=9999)
    isActive: bool = True
    sortOrder: int = 0

    @field_validator("priceUnit")
    @classmethod
    def _unit(cls, v):
        if v not in ("per_person", "per_group", "per_vehicle", "fixed"):
            raise ValueError("Say per_person, per_group, per_vehicle or fixed.")
        return v

    @field_validator("availability")
    @classmethod
    def _availability(cls, v):
        if v not in ("available", "on_request", "seasonal", "not_available"):
            raise ValueError("Say available, on_request, seasonal or not_available.")
        return v

    @field_validator("daysOfWeek")
    @classmethod
    def _days(cls, v):
        if any(d < 1 or d > 7 for d in v):
            raise ValueError("Days are 1 (Monday) to 7 (Sunday).")
        return sorted(set(v))

    def to_columns(self) -> dict:
        return {
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "duration_minutes": self.durationMinutes,
            "price_paise": _rupees_to_paise(self.priceRupees) or 0,
            "price_unit": self.priceUnit,
            "meeting_point": self.meetingPoint,
            "starts_at": self.startsAt.strftime("%H:%M:%S") if self.startsAt else None,
            "min_participants": self.minParticipants,
            "max_participants": self.maxParticipants,
            "min_age": self.minAge,
            "max_age": self.maxAge,
            "requirements": self.requirements,
            "availability": self.availability,
            "available_from": self.availableFrom.isoformat() if self.availableFrom else None,
            "available_to": self.availableTo.isoformat() if self.availableTo else None,
            "days_of_week": self.daysOfWeek,
            "cancellation": self.cancellation or {},
            "photos": self.photos,
            "photo_meta": self.photoMeta or {},
            "lead_time_hours": self.leadTimeHours,
            "daily_capacity": self.dailyCapacity,
            "is_active": self.isActive,
            "sort_order": self.sortOrder,
        }


class ExperienceOut(BaseModel):
    id: str
    title: str
    description: str | None
    category: str
    durationMinutes: int | None
    pricePaise: int
    priceUnit: str
    meetingPoint: str | None
    startsAt: str | None
    minParticipants: int
    maxParticipants: int | None
    minAge: int | None
    maxAge: int | None
    requirements: str | None
    availability: str
    availableFrom: date | None
    availableTo: date | None
    daysOfWeek: list[int]
    cancellation: dict
    photos: list[PhotoMeta]
    leadTimeHours: int
    dailyCapacity: int | None
    isActive: bool
    sortOrder: int

    @classmethod
    def from_row(cls, row: dict) -> "ExperienceOut":
        meta = row.get("photo_meta") or {}
        return cls(
            id=str(row["id"]),
            title=row.get("title") or "",
            description=row.get("description"),
            category=row.get("category") or "other",
            durationMinutes=row.get("duration_minutes"),
            pricePaise=int(row.get("price_paise") or 0),
            priceUnit=row.get("price_unit") or "per_person",
            meetingPoint=row.get("meeting_point"),
            startsAt=str(row["starts_at"])[:5] if row.get("starts_at") else None,
            minParticipants=int(row.get("min_participants") or 1),
            maxParticipants=row.get("max_participants"),
            minAge=row.get("min_age"),
            maxAge=row.get("max_age"),
            requirements=row.get("requirements"),
            availability=row.get("availability") or "available",
            availableFrom=row.get("available_from"),
            availableTo=row.get("available_to"),
            daysOfWeek=[int(d) for d in (row.get("days_of_week") or [])],
            cancellation=row.get("cancellation") or {},
            photos=[
                PhotoMeta(url=url, **{k: v for k, v in (meta.get(url) or {}).items()
                                      if k in {"caption", "alt"}})
                for url in (row.get("photos") or [])
            ],
            leadTimeHours=int(row.get("lead_time_hours") or 0),
            dailyCapacity=row.get("daily_capacity"),
            isActive=bool(row.get("is_active", True)),
            sortOrder=int(row.get("sort_order") or 0),
        )


# ── team ─────────────────────────────────────────────────────────────────────


class TeamMemberOut(BaseModel):
    id: str
    name: str
    role: str
    department: str | None
    email: str | None
    whatsapp: str | None
    phone: str | None
    memberId: str | None
    isActive: bool
    sortOrder: int

    @classmethod
    def from_row(cls, row: dict) -> "TeamMemberOut":
        return cls(
            id=str(row["id"]),
            name=row.get("name") or "",
            role=row.get("role") or "other",
            department=row.get("department"),
            email=row.get("email"),
            whatsapp=row.get("whatsapp"),
            phone=row.get("phone"),
            memberId=str(row["member_id"]) if row.get("member_id") else None,
            isActive=bool(row.get("is_active", True)),
            sortOrder=int(row.get("sort_order") or 0),
        )


class TeamMemberRequest(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=2, max_length=120)
    role: str
    department: str | None = Field(None, max_length=60)
    email: str | None = Field(None, max_length=200)
    whatsapp: str | None = Field(None, max_length=20)
    phone: str | None = Field(None, max_length=20)
    memberId: str | None = None
    isActive: bool = True
    sortOrder: int = 0

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        if v not in TEAM_ROLES:
            raise ValueError(f"Say one of: {', '.join(TEAM_ROLES)}.")
        return v

    @field_validator("whatsapp", "phone")
    @classmethod
    def _digits(cls, v):
        """
        Digits only, the way every other phone column in this schema stores them.

        A number typed as "+91 98765 43210" and one typed as "919876543210" are
        the same phone and must not become two team members — which the unique
        index on (property_id, whatsapp) would otherwise happily allow.
        """
        if v is None:
            return None
        digits = "".join(c for c in v if c.isdigit())
        if not digits:
            return None
        if len(digits) < 10 or len(digits) > 15:
            raise ValueError("That does not look like a phone number.")
        # A bare Indian mobile gets its country code, because the WhatsApp API
        # will not accept it without one and failing at send time is far too
        # late to find out.
        return digits if len(digits) > 10 else f"91{digits}"

    def to_columns(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "department": self.department,
            "email": self.email,
            "whatsapp": self.whatsapp,
            "phone": self.phone,
            "member_id": self.memberId,
            "is_active": self.isActive,
            "sort_order": self.sortOrder,
        }


class NotificationRuleOut(BaseModel):
    id: str
    event: str
    teamMemberId: str
    teamMemberName: str | None = None
    channels: list[str]
    isActive: bool

    @classmethod
    def from_row(cls, row: dict, name: str | None = None) -> "NotificationRuleOut":
        return cls(
            id=str(row["id"]),
            event=row["event"],
            teamMemberId=str(row["team_member_id"]),
            teamMemberName=name,
            channels=row.get("channels") or [],
            isActive=bool(row.get("is_active", True)),
        )


class NotificationRuleRequest(BaseModel):
    event: str
    teamMemberId: str
    channels: list[str] = Field(default_factory=lambda: ["whatsapp"])
    isActive: bool = True

    @field_validator("event")
    @classmethod
    def _event(cls, v):
        if v not in NOTIFICATION_EVENTS:
            raise ValueError(f"Unknown event. One of: {', '.join(NOTIFICATION_EVENTS)}.")
        return v

    @field_validator("channels")
    @classmethod
    def _channels(cls, v):
        bad = [c for c in v if c not in CHANNELS]
        if bad:
            raise ValueError(f"Unknown channel(s): {', '.join(bad)}.")
        if not v:
            raise ValueError("A rule with no channel would fire and do nothing.")
        return sorted(set(v))


class EscalationOut(BaseModel):
    id: str
    kind: str
    status: str
    reason: str | None
    payload: dict
    teamMemberId: str | None
    teamMemberName: str | None
    conversationId: str | None
    messageSent: str | None
    resolution: str | None
    createdAt: str
    notifiedAt: str | None
    respondedAt: str | None
    resolvedAt: str | None

    @classmethod
    def from_row(cls, row: dict, name: str | None = None) -> "EscalationOut":
        return cls(
            id=str(row["id"]),
            kind=row["kind"],
            status=row.get("status") or "open",
            reason=row.get("reason"),
            payload=row.get("payload") or {},
            teamMemberId=str(row["team_member_id"]) if row.get("team_member_id") else None,
            teamMemberName=name,
            conversationId=str(row["conversation_id"]) if row.get("conversation_id") else None,
            messageSent=row.get("message_sent"),
            resolution=row.get("resolution"),
            createdAt=str(row.get("created_at")),
            notifiedAt=str(row["notified_at"]) if row.get("notified_at") else None,
            respondedAt=str(row["responded_at"]) if row.get("responded_at") else None,
            resolvedAt=str(row["resolved_at"]) if row.get("resolved_at") else None,
        )


class EscalationResolveRequest(BaseModel):
    status: str
    resolution: str | None = Field(None, max_length=1000)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v not in ("responded", "resolved", "dismissed"):
            raise ValueError("Say responded, resolved or dismissed.")
        return v


# ── readiness ────────────────────────────────────────────────────────────────


class ReadinessSectionOut(BaseModel):
    key: str
    label: str
    passed: int
    total: int
    pct: int
    complete: bool
    missing: list[dict]


class RoomReadinessOut(BaseModel):
    roomTypeId: str
    name: str | None
    sellable: bool
    readyToSell: bool
    passed: int
    total: int
    pct: int
    missing: list[dict]


class ReadinessReportOut(BaseModel):
    # READY · NEEDS_ATTENTION · INCOMPLETE. §11: a hotel is in one of three
    # states and a percentage is not one of them. Derived from the sections
    # rather than stored, so it cannot drift from them.
    state: str = "INCOMPLETE"
    stateReason: str = ""
    sections: list[ReadinessSectionOut]
    rooms: list[RoomReadinessOut]
    sellableRooms: int
    readyRooms: int
    roomCount: int
    remaining: int
    complete: bool


# ── rate plans ───────────────────────────────────────────────────────────────


class RatePlanDetailOut(BaseModel):
    """
    A rate plan in full, including the four things a contract has and the
    existing projection does not: when it runs, on which days, how much of the
    room pool it may sell, and whether it has a price of its own at all.
    """

    id: str
    roomTypeId: str
    code: str
    name: str
    mealPlan: str
    audience: str
    basePricePaise: int
    extraAdultPaise: int | None
    extraChildPaise: int | None
    refundable: bool
    cancellationCutoffHours: int | None
    cancellationPolicy: dict
    inclusions: list[str]
    minStay: int | None
    maxStay: int | None
    derivedFromPlanId: str | None
    deriveDiscountPct: int | None
    validFrom: date | None
    validTo: date | None
    daysOfWeek: list[int]
    allocationUnits: int | None
    isActive: bool
    sortOrder: int

    @classmethod
    def from_row(cls, row: dict) -> "RatePlanDetailOut":
        return cls(
            id=str(row["id"]),
            roomTypeId=str(row["room_type_id"]),
            code=row.get("code") or "",
            name=row.get("name") or "",
            mealPlan=row.get("meal_plan") or "EP",
            audience=row.get("audience") or "public",
            basePricePaise=int(row.get("base_price_paise") or 0),
            extraAdultPaise=row.get("extra_adult_paise"),
            extraChildPaise=row.get("extra_child_paise"),
            refundable=bool(row.get("refundable", True)),
            cancellationCutoffHours=row.get("cancellation_cutoff_hours"),
            cancellationPolicy=row.get("cancellation_policy") or {},
            inclusions=row.get("inclusions") or [],
            minStay=row.get("min_stay"),
            maxStay=row.get("max_stay"),
            derivedFromPlanId=(
                str(row["derived_from_plan_id"]) if row.get("derived_from_plan_id") else None
            ),
            deriveDiscountPct=row.get("derive_discount_pct"),
            validFrom=row.get("valid_from"),
            validTo=row.get("valid_to"),
            daysOfWeek=[int(d) for d in (row.get("days_of_week") or [])],
            allocationUnits=row.get("allocation_units"),
            isActive=bool(row.get("is_active", True)),
            sortOrder=int(row.get("sort_order") or 0),
        )


class RatePlanDetailRequest(BaseModel):
    """
    Absent means unchanged, the same contract the room editor uses.

    **A derived plan and a priced plan are mutually exclusive and it is enforced
    here as well as by the database.** Setting `derivedFromPlanId` clears the
    base price to zero rather than leaving a stale number behind it — a plan
    that resolves from its parent has no price of its own, and a leftover figure
    in that column is one somebody will eventually read.
    """

    name: str | None = Field(None, min_length=1, max_length=80)
    mealPlan: str | None = None
    audience: str | None = None
    priceRupees: float | None = Field(None, ge=0, le=10_000_000)
    extraAdultRupees: float | None = Field(None, ge=0, le=1_000_000)
    extraChildRupees: float | None = Field(None, ge=0, le=1_000_000)
    refundable: bool | None = None
    cancellationCutoffHours: int | None = Field(None, ge=0, le=8760)
    cancellationPolicy: dict | None = None
    inclusions: list[str] | None = None
    minStay: int | None = Field(None, ge=1, le=90)
    maxStay: int | None = Field(None, ge=1, le=365)
    derivedFromPlanId: str | None = None
    deriveDiscountPct: int | None = Field(None, ge=0, le=100)
    validFrom: date | None = None
    validTo: date | None = None
    daysOfWeek: list[int] | None = None
    allocationUnits: int | None = Field(None, ge=0, le=999)
    isActive: bool | None = None

    @field_validator("mealPlan")
    @classmethod
    def _meal(cls, v):
        if v is not None and v not in ("EP", "CP", "MAP", "AP"):
            raise ValueError("Say EP, CP, MAP or AP.")
        return v

    @field_validator("audience")
    @classmethod
    def _audience(cls, v):
        if v is not None and v not in ("public", "b2b", "corporate", "creator", "internal"):
            raise ValueError("Unknown audience.")
        return v

    @field_validator("daysOfWeek")
    @classmethod
    def _days(cls, v):
        if v is not None and any(d < 1 or d > 7 for d in v):
            raise ValueError("Days are 1 (Monday) to 7 (Sunday).")
        return sorted(set(v)) if v is not None else v

    def to_columns(self) -> dict:
        sent = self.model_dump(exclude_unset=True)
        mapping = {
            "name": "name", "mealPlan": "meal_plan", "audience": "audience",
            "refundable": "refundable",
            "cancellationCutoffHours": "cancellation_cutoff_hours",
            "cancellationPolicy": "cancellation_policy", "inclusions": "inclusions",
            "minStay": "min_stay", "maxStay": "max_stay",
            "derivedFromPlanId": "derived_from_plan_id",
            "deriveDiscountPct": "derive_discount_pct",
            "daysOfWeek": "days_of_week", "allocationUnits": "allocation_units",
            "isActive": "is_active",
        }
        out = {column: sent[key] for key, column in mapping.items() if key in sent}
        for key, column in (("priceRupees", "base_price_paise"),
                            ("extraAdultRupees", "extra_adult_paise"),
                            ("extraChildRupees", "extra_child_paise")):
            if key in sent:
                out[column] = _rupees_to_paise(sent[key])
        for key, column in (("validFrom", "valid_from"), ("validTo", "valid_to")):
            if key in sent:
                out[column] = sent[key].isoformat() if sent[key] else None

        # A plan cannot both derive and carry a price. The database refuses the
        # first half of that; this refuses the second, so the column does not
        # keep a number nobody reads.
        if out.get("derived_from_plan_id"):
            out["base_price_paise"] = 0
        elif "derived_from_plan_id" in out and out["derived_from_plan_id"] is None:
            out["derive_discount_pct"] = None
        return out


# ── marketing and ads ────────────────────────────────────────────────────────


class MarketingProfileOut(BaseModel):
    usp: str | None
    bestFor: list[str]
    idealGuest: str | None
    popularWith: list[str]
    nearby: list
    bestMonths: list[int]
    weekendDemand: str | None
    weekdayDemand: str | None
    seasonality: str | None
    tone: str | None
    languages: list[str]

    @classmethod
    def from_row(cls, row: dict | None) -> "MarketingProfileOut":
        row = row or {}
        return cls(
            usp=row.get("usp"),
            bestFor=row.get("best_for") or [],
            idealGuest=row.get("ideal_guest"),
            popularWith=row.get("popular_with") or [],
            nearby=row.get("nearby") or [],
            bestMonths=[int(m) for m in (row.get("best_months") or [])],
            weekendDemand=row.get("weekend_demand"),
            weekdayDemand=row.get("weekday_demand"),
            seasonality=row.get("seasonality"),
            tone=row.get("tone"),
            languages=row.get("languages") or [],
        )


class MarketingProfileRequest(BaseModel):
    usp: str | None = Field(None, max_length=500)
    bestFor: list[str] | None = None
    idealGuest: str | None = Field(None, max_length=300)
    popularWith: list[str] | None = None
    nearby: list | None = None
    bestMonths: list[int] | None = None
    weekendDemand: str | None = None
    weekdayDemand: str | None = None
    seasonality: str | None = Field(None, max_length=500)
    tone: str | None = Field(None, max_length=100)
    languages: list[str] | None = None

    @field_validator("bestMonths")
    @classmethod
    def _months(cls, v):
        if v is not None and any(m < 1 or m > 12 for m in v):
            raise ValueError("Months are 1 (January) to 12 (December).")
        return sorted(set(v)) if v is not None else v

    @field_validator("weekendDemand", "weekdayDemand")
    @classmethod
    def _demand(cls, v):
        if v is not None and v not in ("low", "medium", "high"):
            raise ValueError("Say low, medium or high.")
        return v

    def to_columns(self) -> dict:
        sent = self.model_dump(exclude_unset=True)
        mapping = {
            "usp": "usp", "bestFor": "best_for", "idealGuest": "ideal_guest",
            "popularWith": "popular_with", "nearby": "nearby",
            "bestMonths": "best_months", "weekendDemand": "weekend_demand",
            "weekdayDemand": "weekday_demand", "seasonality": "seasonality",
            "tone": "tone", "languages": "languages",
        }
        return {column: sent[key] for key, column in mapping.items() if key in sent}


class AudienceOut(BaseModel):
    id: str
    name: str
    ageMin: int | None
    ageMax: int | None
    genders: list[str]
    locations: list
    interests: list[str]
    travelIntent: list[str]

    @classmethod
    def from_row(cls, row: dict) -> "AudienceOut":
        return cls(
            id=str(row["id"]),
            name=row.get("name") or "",
            ageMin=row.get("age_min"),
            ageMax=row.get("age_max"),
            genders=row.get("genders") or [],
            locations=row.get("locations") or [],
            interests=row.get("interests") or [],
            travelIntent=row.get("travel_intent") or [],
        )


class AudienceRequest(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=2, max_length=80)
    ageMin: int | None = Field(None, ge=13, le=65)
    ageMax: int | None = Field(None, ge=13, le=65)
    genders: list[str] = Field(default_factory=list)
    locations: list = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    travelIntent: list[str] = Field(default_factory=list)

    def to_columns(self) -> dict:
        return {
            "name": self.name,
            "age_min": self.ageMin,
            "age_max": self.ageMax,
            "genders": self.genders,
            "locations": self.locations,
            "interests": self.interests,
            "travel_intent": self.travelIntent,
        }


class AdCreativeOut(BaseModel):
    id: str
    format: str
    headline: str | None
    primaryText: str | None
    cta: str | None
    media: list
    instagramMediaId: str | None
    templateKey: str | None
    edited: bool
    isPrimary: bool

    @classmethod
    def from_row(cls, row: dict) -> "AdCreativeOut":
        return cls(
            id=str(row["id"]),
            format=row.get("format") or "single_image",
            headline=row.get("headline"),
            primaryText=row.get("primary_text"),
            cta=row.get("cta"),
            media=row.get("media") or [],
            instagramMediaId=row.get("instagram_media_id"),
            templateKey=row.get("template_key"),
            edited=bool(row.get("edited")),
            isPrimary=bool(row.get("is_primary", True)),
        )


class AdCampaignOut(BaseModel):
    id: str
    name: str
    channel: str
    objective: str
    destination: str
    audienceId: str | None
    roomTypeId: str | None
    experienceId: str | None
    offer: dict
    startsOn: date | None
    endsOn: date | None
    dailyBudgetPaise: int | None
    status: str
    externalId: str | None
    lastError: str | None
    createdAt: str
    creatives: list[AdCreativeOut] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: dict, creatives: list[dict] | None = None) -> "AdCampaignOut":
        return cls(
            id=str(row["id"]),
            name=row.get("name") or "",
            channel=row.get("channel") or "instagram",
            objective=row.get("objective") or "messages",
            destination=row.get("destination") or "whatsapp",
            audienceId=str(row["audience_id"]) if row.get("audience_id") else None,
            roomTypeId=str(row["room_type_id"]) if row.get("room_type_id") else None,
            experienceId=str(row["experience_id"]) if row.get("experience_id") else None,
            offer=row.get("offer") or {},
            startsOn=row.get("starts_on"),
            endsOn=row.get("ends_on"),
            dailyBudgetPaise=row.get("daily_budget_paise"),
            status=row.get("status") or "draft",
            externalId=row.get("external_id"),
            lastError=row.get("last_error"),
            createdAt=str(row.get("created_at")),
            creatives=[AdCreativeOut.from_row(c) for c in (creatives or [])],
        )


class AdCampaignRequest(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=2, max_length=120)
    channel: str = "instagram"
    objective: str = "messages"
    destination: str = "whatsapp"
    audienceId: str | None = None
    roomTypeId: str | None = None
    experienceId: str | None = None
    offerDiscountPct: int | None = Field(None, ge=0, le=100)
    offerValidUntil: date | None = None
    startsOn: date | None = None
    endsOn: date | None = None
    dailyBudgetRupees: float | None = Field(None, ge=0, le=10_000_000)

    @field_validator("channel")
    @classmethod
    def _channel(cls, v):
        if v not in ("instagram", "facebook", "whatsapp"):
            raise ValueError("Say instagram, facebook or whatsapp.")
        return v

    @field_validator("objective")
    @classmethod
    def _objective(cls, v):
        if v not in ("messages", "traffic", "awareness", "engagement", "leads"):
            raise ValueError("Unknown objective.")
        return v

    @field_validator("destination")
    @classmethod
    def _destination(cls, v):
        if v not in ("whatsapp", "instagram_dm", "website", "hotel_page"):
            raise ValueError("Unknown destination.")
        return v


class AdCreativeRequest(BaseModel):
    """
    What a hotelier edited, or picked.

    `edited` is not here: it is derived server-side from whether the copy
    differs from what the template produced. A client deciding that could
    report a draft as hand-written, which is the one thing that flag exists to
    tell the truth about.
    """

    id: str | None = None
    format: str = "single_image"
    headline: str | None = Field(None, max_length=80)
    primaryText: str | None = Field(None, max_length=400)
    cta: str | None = Field(None, max_length=60)
    photos: list[str] = Field(default_factory=list)
    photoMeta: dict[str, dict] | None = None
    videoUrl: str | None = Field(None, max_length=1000)
    instagramMediaId: str | None = Field(None, max_length=64)
    templateKey: str | None = Field(None, max_length=40)

    @field_validator("format")
    @classmethod
    def _format(cls, v):
        if v not in ("single_image", "carousel", "video", "existing_post"):
            raise ValueError("Say single_image, carousel, video or existing_post.")
        return v


class AdDraftOut(BaseModel):
    """The generated starting points, before anybody edits one."""

    drafts: list[dict]
    offer: dict
    # What this hotel is still missing before any ad can be published. Named so
    # the screen can say the specific thing rather than "not available".
    missing: list[str]


class AdPublishOut(BaseModel):
    status: str
    externalId: str | None = None
    detail: str | None = None
    missing: list[str] = Field(default_factory=list)


class ContextHealthOut(BaseModel):
    """
    Whether this hotel's agent can answer each kind of question. §12.

    Deliberately a different question from readiness: readiness asks what the
    hotelier has not filled in, this asks what the AI will get wrong tonight.
    Every row is a count against a threshold; nothing here asks a model.
    """

    propertyId: str
    checks: list[dict]
    answerable: int
    total: int
    coreBroken: list[str]
    health: str
