"""Request/response models for the money path."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    item_type: Literal["group_trip", "experience", "hotel"]
    item_id: str = Field(min_length=1, max_length=64)
    travellers: int = Field(default=1, ge=1, le=50)
    travel_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Must match a tier title on the listing — validated against the row, never
    # trusted as a price.
    tier_title: str | None = Field(default=None, max_length=80)

    # Stay-specific. Ignored for trips/experiences. The price still comes from
    # the DB row and the season table — these only choose *which* nights and how
    # many rooms, never the amount.
    check_in: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    check_out: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    rooms: int = Field(default=1, ge=1, le=20)
    adults: int = Field(default=1, ge=1, le=40)
    children: int = Field(default=0, ge=0, le=40)

    # A code, never a discount amount. The discount is recomputed server-side
    # from the coupon row, so a tampered value changes nothing.
    coupon_code: str | None = Field(default=None, max_length=40)

    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=6, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=500)


class QuoteRequest(BaseModel):
    """A price preview — the booking summary before payment. Charges nothing."""

    item_type: Literal["group_trip", "experience", "hotel"]
    item_id: str = Field(min_length=1, max_length=64)
    travellers: int = Field(default=1, ge=1, le=50)
    tier_title: str | None = Field(default=None, max_length=80)
    check_in: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    check_out: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    rooms: int = Field(default=1, ge=1, le=20)
    coupon_code: str | None = Field(default=None, max_length=40)


class QuoteResponse(BaseModel):
    ok: bool
    gross_paise: int = 0
    discount_paise: int = 0
    amount_paise: int = 0
    commission_paise: int = 0
    nights: int | None = None
    currency: str = "INR"
    coupon_applied: bool = False
    coupon_message: str | None = None
    error: str | None = None


class CheckoutResponse(BaseModel):
    ok: bool
    ref: str | None = None
    order_id: str | None = None
    amount: int | None = None
    currency: str = "INR"
    key_id: str | None = None
    name: str | None = None
    prefill: dict[str, str] | None = None


class VerifyRequest(BaseModel):
    razorpay_order_id: str = Field(min_length=4, max_length=120)
    razorpay_payment_id: str = Field(min_length=4, max_length=120)
    razorpay_signature: str = Field(min_length=4, max_length=200)


class VerifyResponse(BaseModel):
    ok: bool
    ref: str | None = None
    already: bool = False
    error: str | None = None
    code: str | None = None


class SubscribeResponse(BaseModel):
    ok: bool
    order_id: str | None = None
    amount: int | None = None
    currency: str = "INR"
    key_id: str | None = None
    name: str | None = None
    prefill: dict[str, str] | None = None


class PublicBooking(BaseModel):
    """
    The confirmation page's view of a booking.

    Deliberately narrow: no payment ids, no commission, no payout. The `ref` is
    the only credential a traveller has, so this is what a `ref` may reveal.
    """

    ref: str
    item_type: str | None = None
    item_title: str | None = None
    item_slug: str | None = None
    travel_date: str | None = None
    travellers: int | None = None
    # Stay-specific; null for trips/experiences. Safe to expose — the payer
    # chose these.
    check_in: str | None = None
    check_out: str | None = None
    nights: int | None = None
    # The discount line the payer already saw at checkout. Payment ids,
    # commission and payout remain excluded.
    gross_paise: int | None = None
    discount_paise: int | None = None
    amount_paise: int | None = None
    status: str | None = None
    lead_name: str | None = None
    created_at: str | None = None
    partner_id: str | None = None


class UploadResponse(BaseModel):
    ok: bool
    url: str | None = None
    path: str | None = None


class AiJobRequest(BaseModel):
    source_type: Literal["pdf", "image", "text"]
    source_ref: str | None = Field(default=None, max_length=500)
    input_chars: int = Field(default=0, ge=0)
    status: Literal["done", "failed"] = "done"
    output: Any | None = None
    error: str | None = Field(default=None, max_length=1000)
