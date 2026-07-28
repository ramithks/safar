"""Request/response models for admin listing edits and plan coupons."""
from typing import Literal

from pydantic import BaseModel, Field

from app.api.v1.marketplace.partner_schemas import GapItem

CouponKind = Literal["free_months", "percent", "flat"]


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
    #: Months for free_months · % for percent · ₹ for flat.
    value: int = Field(ge=1, le=100_000)
    #: How many billing cycles a percent/flat discount covers.
    duration_months: int | None = Field(default=None, ge=1, le=60)
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
