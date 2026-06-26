"""
Shared hotel filtering/sorting helpers — persona model, property-type
classification, and the budget→sort mapping. Used by the hotel aggregator
(`app/services/hotels.py`) and the search endpoint so persona/budget behaviour
stays in one place.

Canonical property types : hostel | homestay | guesthouse | hotel | resort | apartment
Canonical sort keys      : price_asc | price_desc | recommended | rating
Persona (travel_type)    : solo | couple | friends | family

Native API codes for each scraper live in that scraper's own module (its
``_SORTS`` / property-type maps); this module only deals in the canonical values.
"""
from __future__ import annotations

# ── Budget → sort (hotels). Budget always wins over an explicit sort. ─────────
_BUDGET_SORT = {"low": "price_asc", "mid": "recommended", "high": "rating"}


def budget_to_hotel_sort(budget: str | None) -> str | None:
    """Map a budget tier → canonical hotel sort key, or None if not a tier."""
    return _BUDGET_SORT.get((budget or "").lower())


# ── Persona ──────────────────────────────────────────────────────────────────
def derive_travel_type(adults: int, explicit: str | None = None) -> str:
    """Persona from an explicit field, else inferred from the adult count."""
    if explicit and explicit.lower() in ("solo", "couple", "friends", "family"):
        return explicit.lower()
    if adults <= 1:
        return "solo"
    if adults == 2:
        return "couple"
    return "friends"


# (travel_type, budget) → preferred property types, most-preferred first.
# First entry is the *primary* type used for a native API filter; the whole list
# drives the client-side soft filter (prefer these, never empty) and ranking.
# Rules: solo+low → hostels; mid+couple → hotels; high + couple/friends →
# resorts/hotels (never hostels). Couples & families never get hostels.
_MATRIX: dict[tuple[str, str], list[str]] = {
    ("solo", "low"):     ["hostel", "homestay", "guesthouse", "hotel"],
    ("solo", "mid"):     ["hostel", "hotel"],
    ("solo", "high"):    ["hotel", "resort"],
    ("couple", "low"):   ["hotel", "homestay", "guesthouse"],
    ("couple", "mid"):   ["hotel", "resort"],
    ("couple", "high"):  ["resort", "hotel"],
    ("friends", "low"):  ["hostel", "homestay", "hotel"],
    ("friends", "mid"):  ["hotel", "hostel"],
    ("friends", "high"): ["resort", "hotel"],
    ("family", "low"):   ["hotel", "homestay", "guesthouse"],
    ("family", "mid"):   ["hotel", "resort"],
    ("family", "high"):  ["resort", "hotel"],
}


def preferred_types(travel_type: str, budget: str) -> list[str]:
    """Ordered preferred property types for a persona + budget tier."""
    tt = (travel_type or "couple").lower()
    bg = budget if budget in ("low", "mid", "high") else "mid"
    return _MATRIX.get((tt, bg)) or ["hotel"]


# ── Property-type classification (client-side backbone) ──────────────────────
# Strong, unambiguous hostel signals in the NAME. These override a structured
# source category: a property literally named "Coco Hostel" is a hostel even if
# EaseMyTrip files it under category "Hotel". Mislabelling here is a user-facing
# trust bug (solo/budget travellers expect hostels; couples expect not to see them).
_HOSTEL_TOKENS = (
    "hostel", "backpacker", "gostops", "go stops", "madpackers", "zostel",
    "the hosteller", "hosteller", "jugaad", "moustache", "bunk", "dorm",
)
# Secondary name heuristics, consulted only AFTER the source category.
_NAME_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("resort",    ("resort", "retreat")),
    ("homestay",  ("homestay", "home stay", "cottage", "casa", "villa", "guest house",
                   "guesthouse", "b&b", "bnb", "home")),
    ("apartment", ("apartment", "serviced", "studio", "suites", "stayz")),
]


def classify_property_type(hotel: dict) -> str:
    """
    Best-effort canonical property type for a normalized hotel. Priority:
      1. explicit hostel tokens in the name  (trusted over a wrong category)
      2. source structured category (EaseMyTrip `category`)
      3. secondary name heuristics (resort / homestay / apartment)
      4. default 'hotel'
    """
    name = (hotel.get("name") or "").lower()
    if any(tok in name for tok in _HOSTEL_TOKENS):
        return "hostel"

    cat = (hotel.get("category") or "").lower()
    if cat:
        if "hostel" in cat:
            return "hostel"
        if "resort" in cat:
            return "resort"
        if "homestay" in cat or "home stay" in cat:
            return "homestay"
        if "apart" in cat:
            return "apartment"
        if "inn" in cat or "guest" in cat:
            return "guesthouse"
        if "hotel" in cat:
            return "hotel"

    for ptype, kws in _NAME_RULES:
        if any(kw in name for kw in kws):
            return ptype

    return "hotel"


def tag_property_type(hotel: dict) -> dict:
    """Attach ``property_type`` to a normalized hotel (idempotent)."""
    if not hotel.get("property_type"):
        hotel["property_type"] = classify_property_type(hotel)
    return hotel


def filter_and_rank_by_persona(hotels: list[dict], travel_type: str, budget: str) -> list[dict]:
    """
    Soft-filter to the persona's preferred property types (never returns empty),
    then stable-rank preferred types first (by their order in the preference list).
    Hotels are tagged with ``property_type`` in place if not already.
    """
    prefs = preferred_types(travel_type, budget)
    rank = {t: i for i, t in enumerate(prefs)}
    for h in hotels:
        tag_property_type(h)

    preferred = [h for h in hotels if h.get("property_type") in rank]
    pool = preferred if preferred else hotels
    # Stable sort by preference order; non-preferred (in the fallback case) sink.
    pool.sort(key=lambda h: rank.get(h.get("property_type"), len(prefs)))
    return pool
