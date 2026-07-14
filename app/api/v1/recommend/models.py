"""Request models for /recommend/*. Responses are plain dicts (never expose scores)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class HotelRecommendRequest(BaseModel):
    destination: str
    checkin: str | None = None
    checkout: str | None = None
    pax: int = 2
    budget: str | None = None                    # low|mid|high (also inferred from text)
    text: str | None = None                      # free-form conversation, drives intent
    intake: dict = Field(default_factory=dict)   # structured intake (who/vibe/budget/when)
    candidates: list[dict] | None = None         # pre-fetched hotels; else server scrapes
    session_id: str | None = None
    limit: int = 20
    explain: bool = True                          # run the LLM hero pick


class TransportRecommendRequest(BaseModel):
    origin: str
    destination: str
    date: str | None = None
    pax: int = 1
    modes: list[str] | None = None                # subset of flight|train|bus
    budget: str | None = None
    text: str | None = None
    intake: dict = Field(default_factory=dict)
    candidates: list[dict] | None = None
    hotel_per_night: int | None = None            # for the hidden-hotel-night term
    session_id: str | None = None
    limit: int = 20
    explain: bool = True


class JourneyRecommendRequest(BaseModel):
    origin: str
    destination: str
    date: str | None = None
    pax: int = 1
    text: str | None = None
    intake: dict = Field(default_factory=dict)
    transports: list[dict] | None = None
    stays: list[dict] | None = None
    session_id: str | None = None
    explain: bool = True


class ExplainRequest(BaseModel):
    item: dict
    kind: str = "hotel"                            # hotel|flight|train|bus
    peers: list[dict] | None = None
    text: str | None = None
    intake: dict = Field(default_factory=dict)


class FeedbackRequest(BaseModel):
    item_type: str
    item_id: str
    event: str
    trip_id: str | None = None
    session_id: str | None = None
    position: int | None = None
    dwell_ms: int | None = None
    item: dict | None = None                       # optional, feeds session bias
