"""
Gemini response schemas + Pydantic models for the marketplace AI importer.

Two schemas, because extraction and copywriting are two different jobs:

  EXTRACTION_SCHEMA — what the document actually says. Temperature ~0.1, every
  field nullable, "use null if absent" hammered in the prompt. This is the
  trust-critical call: a hallucinated price here becomes a real price a
  traveller pays.

  GENERATION_SCHEMA — marketing copy written *from* the extracted object.
  Temperature ~0.7. Badged as AI-written in the UI so partners edit it.

Splitting them means a creative flourish can never contaminate a number.
Gemini's responseSchema is an OpenAPI subset: type/nullable/enum/items/
properties/required/description only — no unions, no oneOf.
"""
from typing import Literal

from pydantic import BaseModel, Field

# ── shared fragments ─────────────────────────────────────────────────────────

_STR = {"type": "string", "nullable": True}
_INT = {"type": "integer", "nullable": True}
_NUM = {"type": "number", "nullable": True}


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


_STR_ARR = _arr({"type": "string"})

_DAY = {
    "type": "object",
    "properties": {
        "n": {"type": "string", "description": "Day label as printed, e.g. '1' or 'Day 2'"},
        "title": {"type": "string", "description": "Short headline for the day"},
        "morning": _STR,
        "afternoon": _STR,
        "evening": _STR,
        "night": _STR,
    },
    "required": ["n", "title"],
}

_ROOM = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "price": _INT,
        "occupancy": _INT,
        "beds": _STR,
    },
    "required": ["name"],
}

_TIER = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "e.g. 'Triple sharing', 'Deluxe room'"},
        "amount": {"type": "integer", "description": "Price in whole rupees"},
    },
    "required": ["title", "amount"],
}

_FAQ = {
    "type": "object",
    "properties": {"q": {"type": "string"}, "a": {"type": "string"}},
    "required": ["q", "a"],
}

# ── 1. extraction ────────────────────────────────────────────────────────────

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["hotel", "experience", "group_trip", "unknown"],
            "description": (
                "hotel = a property to stay in. experience = a single activity, "
                "usually one day or less. group_trip = a multi-day packaged tour "
                "with a fixed departure."
            ),
        },
        "title": _STR,
        "description": _STR,
        "destination": _STR,
        "city": _STR,
        "address": _STR,
        "duration_days": _INT,
        "duration_nights": _INT,
        "duration_hours": _NUM,
        "departure_city": _STR,
        "departure_date_text": {
            **_STR,
            "description": "Departure date exactly as printed; do not reformat.",
        },
        "price_from": {
            **_INT,
            "description": "Lowest advertised price in whole rupees. Null if none is printed.",
        },
        "price_unit": {
            "type": "string",
            "enum": ["per_night", "per_person", "per_group", "unknown"],
        },
        "pricing_tiers": _arr(_TIER),
        "inclusions": _STR_ARR,
        "exclusions": _STR_ARR,
        "highlights": _STR_ARR,
        "days": _arr(_DAY),
        "amenities": _STR_ARR,
        "rooms": _arr(_ROOM),
        "meeting_point": _STR,
        "difficulty": {"type": "string", "enum": ["easy", "moderate", "hard", "unknown"]},
        "age_min": _INT,
        "group_size_min": _INT,
        "group_size_max": _INT,
        "what_to_bring": _STR_ARR,
        "cancellation_policy": _STR,
        "faqs": _arr(_FAQ),
        "contact_phone": _STR,
        "confidence": {
            "type": "number",
            "description": "0-1. How much of this document was actually readable as a travel product.",
        },
    },
    # Everything the document is most likely to contain is REQUIRED, even
    # though the values stay nullable. With only kind+confidence required,
    # Gemini reliably skipped inclusions, exclusions and the day list on real
    # brochures that plainly contained them — structured output under-fills
    # optional fields. Requiring them forces an explicit answer; "nothing
    # here" is then an empty array rather than an omission.
    "required": [
        "kind",
        "confidence",
        "title",
        "destination",
        "price_from",
        "pricing_tiers",
        "inclusions",
        "exclusions",
        "highlights",
        "days",
    ],
}

# ── 2. generation ────────────────────────────────────────────────────────────

GENERATION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Punchy, specific, max 70 chars"},
        "summary": {"type": "string", "description": "One sentence, max 160 chars"},
        "description": {"type": "string", "description": "2-3 short paragraphs"},
        "highlights": _arr(
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "desc": {"type": "string"},
                },
                "required": ["title"],
            }
        ),
        "tags": _STR_ARR,
        "things_to_know": _STR_ARR,
        "best_time": _STR,
        "suitable_for": _STR_ARR,
        "seo_title": {"type": "string", "description": "Max 60 chars, no brand suffix"},
        "seo_description": {"type": "string", "description": "Max 155 chars"},
        "faqs": _arr(_FAQ),
    },
    "required": ["title", "summary", "description"],
}


# ── request / response models ────────────────────────────────────────────────

class ExtractTextRequest(BaseModel):
    """Method 2: the partner pastes their itinerary straight in."""

    text: str = Field(min_length=20, max_length=60_000)
    kind_hint: Literal["hotel", "experience", "group_trip"] | None = None
    generate_copy: bool = True


class DayPlanIn(BaseModel):
    """One day of the generated itinerary, so experiences can be pinned to the
    day they actually belong to."""

    day: int = Field(ge=1, le=60)
    title: str = ""
    places: list[str] = Field(default_factory=list, max_length=20)


class MatchRequest(BaseModel):
    destination: str = Field(min_length=2, max_length=100)
    days: int = Field(default=3, ge=1, le=60)
    travellers: int = Field(default=2, ge=1, le=50)
    origin: str | None = Field(default=None, max_length=100)
    budget: int | None = Field(default=None, ge=0, le=100_000_000)
    # Indian trip budgets are quoted per person by default ("Manali under 15k").
    budget_is_total: bool = False
    interests: list[str] = Field(default_factory=list, max_length=20)
    travel_style: str | None = Field(default=None, max_length=60)
    depart_date: str | None = Field(default=None, description="YYYY-MM-DD")
    day_plan: list[DayPlanIn] = Field(default_factory=list, max_length=60)


class InventoryItem(BaseModel):
    """Deliberately kind-agnostic. The planner renders this shape without
    knowing whether it's a hotel, an experience or a future restaurant."""

    id: str
    kind: str
    slug: str
    title: str
    summary: str | None = None
    image: str | None = None
    price: int | None = None
    price_unit: str
    url: str
    booking_type: str
    cta: str
    partner_name: str | None = None
    partner_slug: str | None = None
    verified: bool = False
    confidence: float
    why: list[str] = []
    day: int | None = None


class InventorySection(BaseModel):
    kind: str
    title: str
    items: list[InventoryItem]


class MatchResponse(BaseModel):
    sections: list[InventorySection] = []
    day_matches: list[InventoryItem] = []
    package_replacement: InventoryItem | None = None
    meta: dict = {}


class ExtractResponse(BaseModel):
    ok: bool = True
    kind: str
    confidence: float
    # The listing draft, already shaped for the website's listing editor.
    draft: dict
    # Human one-liners for the "I found: …" moment in the UI.
    found: list[str]
    # True when copy was AI-written (so the editor can badge those fields).
    generated: bool = False
    warnings: list[str] = []
