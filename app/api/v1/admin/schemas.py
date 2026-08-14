"""Request/response models for the internal admin API."""
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

EntityKind = Literal["experience", "group_trip"]


class ReviewRequest(BaseModel):
    kind: EntityKind
    id: str
    action: Literal["approve", "reject"]
    # Required on reject and enforced in the endpoint: a rejection with no
    # explanation is the fastest way to lose supply.
    reason: str | None = Field(default=None, max_length=500)


class UnpublishRequest(BaseModel):
    kind: EntityKind
    id: str
    reason: str | None = Field(default=None, max_length=500)


class PartnerActionRequest(BaseModel):
    id: str
    # trust_publishing / require_review flip `auto_publish` — whether this
    # partner's submissions skip the review queue. Deliberately separate from
    # verify/unverify, which is the traveller-facing trust badge.
    action: Literal[
        "verify",
        "unverify",
        "suspend",
        "reactivate",
        "grant_plan",
        "trust_publishing",
        "require_review",
    ]
    months: int = Field(default=1, ge=1, le=12)
    reason: str | None = Field(default=None, max_length=500)


class PartnerLoginLinkRequest(BaseModel):
    """
    Ask for a one-time sign-in link into a partner's own dashboard.

    `reason` is required, not polite: this is the one admin action that lets a
    human act *as* someone else, and the audit row is the only thing that
    distinguishes "onboarding help they asked for" from an account takeover.
    """

    reason: str = Field(min_length=3, max_length=500)


class PartnerInviteRequest(BaseModel):
    """Open a dashboard for a business that has no TravBuddy account yet."""

    # Shape check only — pydantic's EmailStr would pull in email-validator, and
    # GoTrue is the real judge of whether an address is usable anyway.
    email: str = Field(min_length=5, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    reason: str = Field(min_length=3, max_length=500)


class PartnerLoginLink(BaseModel):
    # Null when the account exists (or was just created) but hasn't started a
    # partner profile — the link then lands on /partner/start instead.
    partner_id: str | None = None
    brand: str | None = None
    # The auth account the link signs into — shown in the dashboard so the
    # operator can see whose session they're about to open.
    email: str
    # Single-use, short-lived. Exchanged by the website at /auth/confirm.
    token_hash: str
    # The OTP type the website must pass to verifyOtp: "magiclink", or "invite"
    # for an account this call just created.
    type: str
    # Where to land once the session is live.
    next: str
    url: str
    # True when this call created the auth account.
    created_account: bool = False


class SettlePayoutRequest(BaseModel):
    partner_id: str
    booking_ids: list[str] = Field(min_length=1, max_length=500)
    utr: str = Field(min_length=3, max_length=64)
    note: str | None = Field(default=None, max_length=500)


class QueueItem(BaseModel):
    id: str
    kind: EntityKind
    slug: str
    title: str
    brand: str
    partner_id: str | None = None
    destination: str | None = None
    price: int | None = None
    photo: str | None = None
    submitted: str
    flags: list[str] = []
    public_url: str


class DueGroup(BaseModel):
    partner_id: str
    brand: str
    payout_method: str
    booking_ids: list[str]
    refs: list[str]
    gross_paise: int
    commission_paise: int
    net_paise: int


class AuditEntry(BaseModel):
    id: str
    actor: str
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    entity_label: str | None = None
    reason: str | None = None
    metadata: dict = {}
    created_at: str


class Ok(BaseModel):
    ok: bool = True
    message: str | None = None


# ── AI usage and margin (§9.2) ───────────────────────────────────────────────
#
# An internal operating instrument, not a reporting feature to add when finance
# asks. Two things surface here that nothing else will: some hotels are
# excellent accounts, and some lean on expensive AI far past what their plan
# covers. Without this view, the first signal of the second kind is a bad
# quarter.

class MarginRow(BaseModel):
    billingAccountId: str
    partnerId: str | None = None
    account: str
    propertyName: str | None = None
    creditsConsumed: int = 0
    # What we paid the providers, in paise. Micros are the storage unit; nobody
    # reads a dashboard in millionths of a rupee.
    costPaise: int = 0
    revenuePaise: int = 0
    marginPaise: int = 0
    marginPct: float | None = None
    events: int = 0
    topUsageType: str | None = None
    costByProvider: dict = {}


class MarginReport(BaseModel):
    periodStart: date
    periodEnd: date
    rows: list[MarginRow] = []
    totalCostPaise: int = 0
    totalRevenuePaise: int = 0
    totalMarginPaise: int = 0
    blendedMarginPct: float | None = None


class CreditGrantRequest(BaseModel):
    billingAccountId: str
    credits: int = Field(..., gt=0, le=1_000_000)
    kind: Literal["free", "trial", "subscription", "purchased",
                  "bonus", "adjustment", "promotional"] = "purchased"
    # The idempotency key, and required for that reason. A retried grant that
    # lands twice is money given away.
    reference: str = Field(..., min_length=4, max_length=120)
    expiresAt: datetime | None = None
    note: str | None = Field(None, max_length=400)
