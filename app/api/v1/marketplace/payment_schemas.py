"""Request/response models for the money path."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    item_type: Literal["group_trip", "experience"]
    item_id: str = Field(min_length=1, max_length=64)
    travellers: int = Field(default=1, ge=1, le=50)
    travel_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Must match a tier title on the listing — validated against the row, never
    # trusted as a price.
    tier_title: str | None = Field(default=None, max_length=80)
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=6, max_length=20)
    email: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=500)


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
