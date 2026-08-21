"""Shapes for the operator's billing screens. See `billing.py`."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Ok(BaseModel):
    ok: bool = True
    message: str = ""


class AdminHotelOut(BaseModel):
    propertyId: str
    name: str
    slug: str | None = None
    city: str | None = None
    propertyStatus: str
    partnerId: str
    billingAccountId: str | None = None
    # none · pending · trialing · active · past_due · paused · cancelled · expired
    planStatus: str = "none"
    # Whether a real Razorpay mandate exists. `planStatus` alone cannot say:
    # the seeded accounts carry an 'active' row and have never paid anybody.
    hasMandate: bool = False
    couponCode: str | None = None
    currentPeriodEnd: datetime | None = None
    startedAt: datetime | None = None
    # When money last actually arrived, from the ledger. `planStatus` says what
    # Razorpay thinks; this says when we were last paid, and the two are the
    # first pair an operator compares when something looks wrong.
    lastPaidAt: datetime | None = None
    graceUntil: datetime | None = None
    cancelAtPeriodEnd: bool = False
    credits: int = 0
    websiteConversations: int = 0
    instagramConversations: int = 0
    whatsappConversations: int = 0
    creditsUsedThisMonth: int = 0
    createdAt: datetime | None = None


class LedgerEntryOut(BaseModel):
    id: str
    entryType: str
    credits: int
    balanceAfter: int
    usageType: str | None = None
    conversationId: str | None = None
    referenceType: str | None = None
    referenceId: str | None = None
    actorType: str | None = None
    actor: str | None = None
    reason: str | None = None
    createdAt: datetime


class CreditAdjustRequest(BaseModel):
    billingAccountId: str
    # Signed: negative removes. One field rather than an amount plus a
    # direction, because two fields is a way to send +500 meaning -500.
    credits: int
    kind: Literal["bonus", "adjustment", "promotional", "purchased",
                  "free", "trial", "subscription"] = "adjustment"
    # Required in both directions. It is what appears in the hotel's ledger and
    # in the audit row, and a blank is not something anybody can read back.
    reason: str = Field(..., min_length=3, max_length=400)
    # The idempotency key. A retried adjustment that lands twice is money.
    reference: str = Field(..., min_length=4, max_length=120)


class AdminCouponOut(BaseModel):
    id: str
    code: str
    kind: str
    campaign: str | None = None
    description: str | None = None
    active: bool
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    max_redemptions: int | None = None
    max_per_partner: int = 1
    redeemed_count: int = 0
    percent_off: int | None = None
    percent_cycles: int = 1
    free_days: int | None = None
    razorpay_offer_id: str | None = None
    created_at: datetime | None = None
    # Derived, so the list does not make an operator work them out.
    live: bool = False
    exhausted: bool = False


class CouponUpsertRequest(BaseModel):
    # Absent creates; present edits.
    id: str | None = None
    code: str = Field(..., max_length=32)
    kind: Literal["percent", "free_days"]
    # Deliberately unbounded here and checked in the route. A pydantic bound
    # answers an operator who types 95 with
    # `[{"type":"less_than_equal","loc":["body","percentOff"]…}]`, which is a
    # sentence about our request model, on the screen of somebody running a
    # marketing campaign.
    percentOff: int | None = None
    percentCycles: int | None = Field(default=1, ge=1, le=12)
    freeDays: int | None = Field(default=None, ge=1, le=365)
    # Required for `percent`, and the route says why in words an operator can act on.
    razorpayOfferId: str | None = Field(default=None, max_length=120)
    campaign: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=400)
    active: bool = True
    startsAt: datetime | None = None
    expiresAt: datetime | None = None
    maxRedemptions: int | None = Field(default=None, ge=1, le=1_000_000)
    maxPerPartner: int = Field(default=1, ge=1, le=100)
    eligiblePartners: list[str] | None = None


class RedemptionOut(BaseModel):
    id: str
    propertyId: str | None = None
    hotelName: str | None = None
    valuePaise: int = 0
    razorpaySubscriptionId: str | None = None
    redeemedAt: datetime


class AuditEntryOut(BaseModel):
    """One thing a person chose to do to a hotel's money."""

    id: str
    actor: str
    action: str
    reason: str | None = None
    credits: int | None = None
    createdAt: datetime
