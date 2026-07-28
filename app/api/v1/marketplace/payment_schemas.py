"""Request/response models for the money path."""
from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckoutRequest(BaseModel):
    item_type: Literal["group_trip", "experience", "hotel"]
    item_id: str = Field(min_length=1, max_length=64)
    travellers: int = Field(default=1, ge=1, le=50)
    travel_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # Which batch of a group trip. Required once a trip has open departures —
    # the seats come off THAT date's inventory, and the date on the booking is
    # read from the row, never from `travel_date`.
    departure_id: str | None = Field(default=None, max_length=64)
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
    departure_id: str | None = Field(default=None, max_length=64)
    tier_title: str | None = Field(default=None, max_length=80)
    check_in: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    check_out: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    rooms: int = Field(default=1, ge=1, le=20)
    coupon_code: str | None = Field(default=None, max_length=40)
    # Only ever used to check a phone-targeted coupon. Never stored by /quote.
    phone: str | None = Field(default=None, max_length=20)


class QuoteResponse(BaseModel):
    ok: bool
    gross_paise: int = 0
    discount_paise: int = 0
    #: gross - discount: what the booking is worth in full.
    total_paise: int = 0
    #: Charged now — the advance, or the whole total when the item takes none.
    amount_paise: int = 0
    #: Due to the partner directly. Never passes through TravBuddy.
    balance_paise: int = 0
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


class SubscribeRequest(BaseModel):
    """A plan coupon is the only thing the client gets to contribute here —
    and only as a code. What it is worth is read from our own table."""

    coupon_code: str | None = Field(default=None, max_length=32)


class PlanCouponRequest(BaseModel):
    code: str = Field(min_length=3, max_length=32)


class PlanCouponResponse(BaseModel):
    """
    What a validated code is worth, in the only terms the partner needs. The
    coupon row itself is never exposed — usage counts and campaign names are
    ours, not theirs.
    """

    ok: bool
    code: str | None = None
    kind: str | None = None
    #: One line for the UI: "First 3 months free", "50% off for 6 months".
    description: str | None = None
    #: Free plan months this grants outright; 0 for a Razorpay-offer discount.
    free_months: int = 0
    error: str | None = None


class SubscribeResponse(BaseModel):
    """
    Checkout opens against a `subscription_id`, not an `order_id` — that is what
    turns the ₹499 into a mandate the bank charges every month instead of a
    payment the partner has to remember to repeat.
    """

    ok: bool
    subscription_id: str | None = None
    amount: int | None = None
    currency: str = "INR"
    key_id: str | None = None
    name: str | None = None
    description: str | None = None
    prefill: dict[str, str] | None = None
    #: Echoed back when a coupon was accepted, so the UI can confirm what the
    #: partner is actually agreeing to before Checkout opens.
    coupon_code: str | None = None
    coupon_description: str | None = None
    #: When a free period defers the first charge, this is when billing starts.
    first_charge_at: str | None = None


class SubscriptionVerifyRequest(BaseModel):
    """
    The Checkout callback for a subscription.

    Note the field names: Razorpay hands back `razorpay_subscription_id` here,
    not `razorpay_order_id`, and the signature is over
    `payment_id|subscription_id` — the reverse of the order flow.
    """

    razorpay_subscription_id: str = Field(min_length=4, max_length=120)
    razorpay_payment_id: str = Field(min_length=4, max_length=120)
    razorpay_signature: str = Field(min_length=4, max_length=200)


class SubscriptionStatus(BaseModel):
    """What the plan page renders after a cancel or a status check."""

    ok: bool = True
    #: The Razorpay mandate state: none/active/pending/halted/cancelled/…
    status: str = "none"
    #: The gate the rest of the app reads: none/active/past_due/cancelled.
    plan_status: str = "none"
    #: When the paid-up period runs out. Cancelling never shortens this.
    expires_at: str | None = None
    error: str | None = None


class PublicBooking(BaseModel):
    """
    The confirmation page's view of a booking.

    Deliberately narrow: no payment ids, no commission, no payout. The `ref` is
    the only credential a traveller has, so this is what a `ref` may reveal —
    and therefore also what a *guessed* ref reveals, which is the lens to judge
    every field here by.

    `lead_name` is masked to a first name and an initial for that reason. The
    payer needs to recognise their own booking, which "Rishi A." does; nobody
    needs the full legal name of whoever a guessed ref belongs to. Phone and
    email were never here and must not be added — a ref is a weak credential and
    this response is sized to match it.
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
    total_paise: int | None = None
    amount_paise: int | None = None
    balance_paise: int | None = None
    status: str | None = None
    # Masked — see the class docstring.
    lead_name: str | None = None
    created_at: str | None = None


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
