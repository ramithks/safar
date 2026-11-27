"""
Models for Safar group-trip authoring.

These mirror the `PackageContent` contract the public /trips/{slug} template
reads (see lib/marketplace/types.ts on the website). Keeping the shapes aligned
is what lets a Safar trip and a partner package render through one template
with no forking.
"""
from typing import Literal

from pydantic import BaseModel, Field


class TripDay(BaseModel):
    n: str = Field(default="", max_length=12)
    title: str = Field(min_length=1, max_length=120)
    img: str | None = Field(default=None, max_length=500)
    morning: str | None = Field(default=None, max_length=600)
    afternoon: str | None = Field(default=None, max_length=600)
    evening: str | None = Field(default=None, max_length=600)
    night: str | None = Field(default=None, max_length=600)


class TripTier(BaseModel):
    """`amount` is authoritative; `price`/`value` are derived server-side."""

    title: str = Field(min_length=1, max_length=40)
    amount: int = Field(ge=0, le=10_000_000)


class TripInclusion(BaseModel):
    emoji: str | None = Field(default=None, max_length=8)
    label: str = Field(min_length=1, max_length=160)


class TripFaq(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    a: str = Field(min_length=1, max_length=1200)


class TripDraft(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=6000)
    destination: str | None = Field(default=None, max_length=80)
    origin: str | None = Field(default=None, max_length=80)

    duration_days: int | None = Field(default=None, ge=1, le=60)
    departure_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    departure_label: str | None = Field(default=None, max_length=60)
    seats_total: int | None = Field(default=None, ge=1, le=500)
    price_from: int | None = Field(default=None, ge=0, le=10_000_000)

    hero_image: str | None = Field(default=None, max_length=500)
    gallery: list[str] = Field(default_factory=list, max_length=24)
    whatsapp_number: str | None = Field(default=None, max_length=20)
    seo_title: str | None = Field(default=None, max_length=70)

    # `instant` — pay on the page — is the default because that is what every
    # partner trip already does, and a Safar-run trip defaulting to a
    # WhatsApp enquiry made OUR trips look less trustworthy than a partner's on
    # an otherwise identical page. Set it to whatsapp/enquiry per trip when a
    # departure genuinely can't be sold online.
    booking_mode: Literal["whatsapp", "enquiry", "instant"] = "instant"

    days: list[TripDay] = Field(default_factory=list, max_length=30)
    pricing: list[TripTier] = Field(default_factory=list, max_length=8)
    inclusions: list[TripInclusion] = Field(default_factory=list, max_length=40)
    exclusions_note: str | None = Field(default=None, max_length=1000)
    fine_print: list[TripFaq] = Field(default_factory=list, max_length=20)

    # Anything the editor doesn't model yet (reviews, stay_blurb, hotel_photos)
    # rides through untouched so hand-authored enrichment survives an edit.
    content: dict = Field(default_factory=dict)


class PublishGap(BaseModel):
    field: str
    label: str


class TripSaved(BaseModel):
    id: str
    slug: str
    status: str
    gaps: list[PublishGap] = []


class TripValidation(BaseModel):
    ready: bool
    gaps: list[PublishGap] = []


class AdminTrip(BaseModel):
    id: str
    slug: str
    title: str
    description: str | None = None
    destination: str | None = None
    origin: str | None = None
    duration_days: int | None = None
    departure_date: str | None = None
    departure_label: str | None = None
    seats_total: int | None = None
    seats_left: int | None = None
    price_from: int | None = None
    hero_image: str | None = None
    whatsapp_number: str | None = None
    seo_title: str | None = None
    content: dict = {}
    status: str
    booking_mode: str
    partner_id: str | None = None
    is_safar: bool
    views: int = 0
    leads: int = 0
    updated_at: str
    public_url: str
    gaps: list[PublishGap] = []
