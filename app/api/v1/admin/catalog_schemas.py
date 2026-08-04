"""Request/response models for admin listing edits and plan coupons."""
from typing import Literal

from pydantic import BaseModel, Field

from app.api.v1.marketplace.partner_schemas import GapItem

CouponKind = Literal["free_months", "percent", "flat", "deal"]
BillingPeriod = Literal["monthly", "yearly"]


class AdminListing(BaseModel):
    id: str
    kind: str
    slug: str
    title: str
    status: str
    partner_id: str | None = None
    brand: str
    city: str | None = None
    destination: str | None = None
    address: str | None = None
    map_url: str | None = None
    summary: str | None = None
    description: str | None = None
    price_from: int | None = None
    price_unit: str | None = None
    photos: list[str] = []
    content: dict = {}
    views: int = 0
    leads: int = 0
    rejection_reason: str | None = None
    updated_at: str
    public_url: str


class ListingPatch(BaseModel):
    """
    Every field optional and `exclude_unset` on write, so an admin correcting
    one thing cannot blank the rest by omission.
    """

    title: str | None = Field(default=None, min_length=2, max_length=160)
    summary: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=6000)
    city: str | None = Field(default=None, max_length=80)
    destination: str | None = Field(default=None, max_length=80)
    address: str | None = Field(default=None, max_length=300)
    map_url: str | None = Field(default=None, max_length=500)
    price_from: int | None = Field(default=None, ge=100, le=10_000_000)
    price_unit: str | None = Field(default=None, max_length=30)
    photos: list[str] | None = Field(default=None, max_length=20)
    content: dict | None = None
    #: Why the edit was made. Shows up in the audit trail beside the field list.
    reason: str | None = Field(default=None, max_length=500)


class AdminCoupon(BaseModel):
    id: str
    code: str
    kind: CouponKind
    value: int
    duration_months: int | None = None
    #: `deal` only — the whole commercial offer the code carries.
    plan_amount_paise: int | None = None
    plan_period: BillingPeriod | None = None
    commission_pct: float | None = None
    #: NULL = the rate holds while the deal does; a number makes it introductory.
    commission_months: int | None = None
    razorpay_offer_id: str | None = None
    description: str | None = None
    campaign: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    usage_limit: int | None = None
    #: The counter `redeem_plan_coupon` bumps.
    used_count: int = 0
    #: Actual rows in platform_coupon_redemptions. Should match used_count.
    redemptions: int = 0
    active: bool = True
    created_at: str


class CouponCreate(BaseModel):
    code: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9]+$")
    kind: CouponKind
    #: Months for free_months · % for percent · ₹ for flat · ₹ price for deal.
    value: int = Field(ge=1, le=100_000)
    #: How many billing cycles a percent/flat discount covers.
    duration_months: int | None = Field(default=None, ge=1, le=60)

    # ── deal codes ──────────────────────────────────────────────────────────
    #: The subscription price this code sets, in paise. 0 is a real answer:
    #: a commission-only deal with no mandate at all.
    plan_amount_paise: int | None = Field(default=None, ge=0, le=10_000_000)
    plan_period: BillingPeriod | None = None
    commission_pct: float | None = Field(default=None, ge=0, le=50)
    #: Blank means the rate lasts as long as the deal. A number makes it an
    #: intro period, after which the partner reverts to standard commission.
    commission_months: int | None = Field(default=None, ge=1, le=60)

    #: Required for percent/flat — Razorpay applies those, not us.
    razorpay_offer_id: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=200)
    campaign: str | None = Field(default=None, max_length=80)
    starts_at: str | None = None
    ends_at: str | None = None
    usage_limit: int | None = Field(default=None, ge=1, le=1_000_000)
    active: bool = True


class CouponUpdate(BaseModel):
    """
    Code, kind and value are absent on purpose — those were printed on a
    campaign. Changing what a live code is worth is a new coupon, not an edit.
    """

    active: bool | None = None
    description: str | None = Field(default=None, max_length=200)
    campaign: str | None = Field(default=None, max_length=80)
    ends_at: str | None = None
    usage_limit: int | None = Field(default=None, ge=1, le=1_000_000)


# ── a partner's commercial terms ────────────────────────────────────────────


class BillingMismatch(BaseModel):
    """
    The live mandate charges something other than the agreed deal.

    Not an error state — an expected consequence of Razorpay having no API to
    reprice a running subscription. It is reported so an operator can see it and
    act, rather than the dashboard showing agreed terms that nobody's bank is
    honouring.
    """

    billed_amount_paise: int
    billed_period: BillingPeriod
    expected_amount_paise: int
    expected_period: BillingPeriod
    subscription_id: str | None = None


class PartnerTerms(BaseModel):
    """
    The deal a partner is on. `source` says where it came from, which is the
    difference between "they redeemed EARLYHOTEL" and "someone agreed this on
    WhatsApp" when the numbers are queried three months from now.

    Every rate here is RESOLVED — what would be charged on a booking made right
    now, after any intro period or lapse. `lapsed` and `intro_ended` say why it
    differs from what was originally agreed.
    """

    partner_id: str
    subscription_fee_paise: int
    billing_period: BillingPeriod
    commission_pct: float
    commission_expires_at: str | None = None
    base_commission_pct: float
    entitlement: Literal["subscription", "permanent", "until"] = "subscription"
    entitlement_expires_at: str | None = None
    effective_at: str | None = None
    #: True when the deal itself has lapsed (plan unpaid, or a dated entitlement
    #: passed) and commission has fallen back to the base rate.
    lapsed: bool = False
    #: True when the introductory window ended but the deal is otherwise live.
    intro_ended: bool = False
    source: Literal["default", "coupon", "admin"]
    source_coupon_id: str | None = None
    source_coupon_code: str | None = None
    notes: str | None = None
    #: The one-line rendering, so the dashboard and the partner's plan page
    #: cannot describe the same deal two different ways.
    summary: str
    #: Present when Razorpay is collecting something other than these terms.
    billing_mismatch: BillingMismatch | None = None
    #: Tables whose denormalised copy could not be rewritten on the last save.
    #: Bookings are unaffected — they price from these terms — but the partner's
    #: dashboard will show a stale rate until it succeeds.
    stale_listings: list[str] = []


class PartnerTermsUpdate(BaseModel):
    """
    Set a partner's terms by hand — the negotiated-over-WhatsApp path.

    Every field is required rather than patchable on purpose: these numbers ARE
    the deal, and letting someone change the commission while leaving a stale
    period behind is how a partner ends up on terms nobody agreed to. The same
    reasoning is why saving always re-dates the deal: an intro window is
    measured from this save, never inherited from the deal it replaced.
    """

    #: 0 is legitimate: commission-only, no subscription, no mandate.
    subscription_fee_paise: int = Field(ge=0, le=10_000_000)
    billing_period: BillingPeriod
    commission_pct: float = Field(ge=0, le=50)
    #: Blank = holds for as long as the deal does. A number = intro period,
    #: counted from the moment this is saved.
    commission_months: int | None = Field(default=None, ge=1, le=60)
    #: What the rate reverts to when an intro period ends. Defaults to standard.
    base_commission_pct: float | None = Field(default=None, ge=0, le=50)
    #: What keeps the deal alive. Omitted means "subscription" for a paid deal
    #: and "permanent" for a ₹0 one — a free deal cannot be conditional on a
    #: subscription that will never exist.
    entitlement: Literal["subscription", "permanent", "until"] | None = None
    #: Required when entitlement is "until"; ignored otherwise.
    entitlement_expires_at: str | None = None
    notes: str | None = Field(default=None, max_length=500)
    #: Why. Lands in the audit trail beside the numbers.
    reason: str | None = Field(default=None, max_length=500)


class MandateRealignResult(BaseModel):
    """What happened when an operator cancelled a mandate to re-price it."""

    ok: bool
    message: str
    terms: PartnerTerms


# ── publishing someone else's listing ───────────────────────────────────────


class ListingPublishRequest(BaseModel):
    """`live=False` takes it back down to a draft."""

    live: bool = True
    reason: str | None = Field(default=None, max_length=500)
    #: Publish although the partner has no active ₹499 plan. Off by default:
    #: the nightly expiry sweep pulls unpaid stays back down, so publishing
    #: without one usually just produces a listing that vanishes overnight.
    ignore_plan: bool = False


class ListingPublishResult(BaseModel):
    """
    Always 200 — a refusal here is an answer the editor renders, not an error.
    `gaps` is the same checklist the partner sees in their own dashboard.
    """

    ok: bool
    status: str
    message: str
    gaps: list[GapItem] = []
    needs_plan: bool = False
    public_url: str | None = None
