"""Request/response models for the internal admin API."""
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
