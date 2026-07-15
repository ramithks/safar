"""
Shared hotel filtering/sorting helpers — persona model, property-type
classification, the budget→sort mapping, and the canonical user-facing filter
layer (`sanitize_hotel_filters` / `apply_hotel_filters`). Used by the hotel
aggregator (`app/services/hotels.py`), the search endpoint, and the copilot so
persona/budget/filter behaviour stays in one place.

Canonical property types : hostel | homestay | guesthouse | hotel | resort |
                           apartment | villa | vacation_home | bnb
Canonical sort keys      : price_asc | price_desc | recommended | rating
Persona (travel_type)    : solo | couple | friends | family
Filters                  : see HOTEL_FILTERS_CATALOG / sanitize_hotel_filters

Native API codes for each scraper live in that scraper's own module (its
``_SORTS`` / property-type maps); this module only deals in the canonical
values. Facility/meal filters match against the normalized hotels' free-text
fields (amenities, highlights, meal_plan…) because every source returns
amenities as display strings, not codes.
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
# Order matters: more specific tokens first ("vacation home" before "home").
_NAME_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("villa",         ("villa", "villament")),
    ("resort",        ("resort", "retreat")),
    ("bnb",           ("bed and breakfast", "bed & breakfast", "b&b", "bnb")),
    ("guesthouse",    ("guest house", "guesthouse")),
    ("vacation_home", ("vacation home", "holiday home")),
    ("homestay",      ("homestay", "home stay", "cottage", "casa", "home")),
    ("apartment",     ("apartment", "aparthotel", "serviced", "studio", "suites", "stayz")),
]

# Full canonical property-type vocabulary (classifier output ∪ filter input).
PROPERTY_TYPES: frozenset[str] = frozenset({
    "hostel", "homestay", "guesthouse", "hotel", "resort",
    "apartment", "villa", "vacation_home", "bnb",
})


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
        if "villa" in cat:
            return "villa"
        if "bed and breakfast" in cat or "b&b" in cat:
            return "bnb"
        if "vacation" in cat or "holiday home" in cat:
            return "vacation_home"
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


# ── Canonical user-facing filter layer ───────────────────────────────────────
# Facility key → substrings matched (case-insensitive) against the hotel's
# free-text fields: amenities list, highlights, meal_plan, tags, description.
# Keys mirror the filter groups OTAs expose (facilities, room facilities, bed
# preference, travel group, accessibility) collapsed into one flat vocabulary.
FACILITY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "parking":          ("parking", "car park", "valet"),
    "pool":             ("pool", "swimming"),
    "private_pool":     ("private pool", "plunge pool"),
    "beachfront":       ("beachfront", "beach front", "private beach", "on the beach", "beach access"),
    "sea_view":         ("sea view", "ocean view", "beach view"),
    "mountain_view":    ("mountain view", "valley view", "himalaya"),
    "wifi":             ("wifi", "wi-fi", "wi fi", "internet"),
    "spa":              ("spa", "wellness", "massage", "sauna", "steam"),
    "gym":              ("gym", "fitness"),
    "air_conditioning": ("air condition", "aircondition", "climate control", "a/c"),
    "private_bathroom": ("private bathroom", "attached bathroom", "ensuite", "en-suite"),
    "balcony":          ("balcony", "terrace", "patio"),
    "kitchen":          ("kitchen", "kitchenette"),
    "restaurant":       ("restaurant", "dining"),
    "bar":              ("bar", "lounge", "pub"),
    "room_service":     ("room service",),
    "airport_shuttle":  ("airport shuttle", "airport transfer", "shuttle", "pick up", "pickup"),
    "pet_friendly":     ("pet friendly", "pet-friendly", "pets allowed", "pets are allowed"),
    "family_rooms":     ("family room",),
    "non_smoking":      ("non-smoking", "non smoking", "no smoking"),
    "laundry":          ("laundry", "washing machine", "dry cleaning"),
    "hot_tub":          ("hot tub", "jacuzzi"),
    "garden":           ("garden", "lawn"),
    "bonfire":          ("bonfire", "campfire", "fire pit"),
    "bicycle_rental":   ("bicycle", "bike rental", "cycling"),
    "ev_charging":      ("electric vehicle", "ev charging", "charging station"),
    # Bare "accessible" would hit "easily accessible location" in descriptions;
    # bare "tv" hits inside words ("ATV") — hence "wheelchair" only and " tv"
    # (the haystack is space-padded so a leading boundary always exists).
    "wheelchair_accessible": ("wheelchair", "grab rails", "roll-in", "disabled access"),
    "elevator":         ("elevator", "lift"),
    "heating":          ("heating", "heater", "fireplace"),
    "tv":               (" tv", "television", "flat-screen", "flat screen"),
    "twin_beds":        ("twin bed",),
    "double_bed":       ("double bed", "king bed", "queen bed", "king size", "queen size"),
}

# Meal filter key → substrings matched against meal_plan (breakfast_included
# additionally honours the structured is_breakfast_included flag).
MEAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "breakfast_included": ("breakfast",),
    "half_board":         ("breakfast & dinner", "breakfast and dinner", "half board", "half-board"),
    "full_board":         ("all meals", "full board", "full-board"),
    "all_inclusive":      ("all-inclusive", "all inclusive"),
}

_ALLOWED_SORTS = frozenset({"price_asc", "price_desc", "recommended", "rating"})

# Boolean filters that map straight onto normalized hotel fields.
_BOOL_FILTERS = ("free_cancellation", "no_prepayment", "breakfast_included",
                 "couple_friendly", "pet_friendly")


def hotel_filters_catalog() -> dict:
    """The full canonical filter vocabulary — for the frontend/copilot to render."""
    return {
        "property_types": sorted(PROPERTY_TYPES),
        "facilities": sorted(FACILITY_SYNONYMS),
        "meals": list(MEAL_SYNONYMS),
        "star_ratings": [1, 2, 3, 4, 5],
        "min_review_score": {"scale": "0–10", "examples": {"pleasant": 6, "good": 7, "very_good": 8, "wonderful": 9}},
        "booleans": list(_BOOL_FILTERS),
        "ranges": ["price_min", "price_max", "max_distance_km"],
        "sorts": sorted(_ALLOWED_SORTS),
    }


def _canon_token(v) -> str:
    """'Sea View' / 'sea-view' → 'sea_view' so LLM/UI variants hit the enums."""
    return str(v).strip().lower().replace("-", "_").replace(" ", "_")


def sanitize_hotel_filters(raw: dict | None) -> dict:
    """
    Validate an untrusted filter dict (LLM intent output or API body) down to
    the canonical vocabulary. Unknown keys/values are dropped, tokens are
    normalized, numbers coerced and clamped. Returns {} when nothing survives.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}

    def _enum_list(key: str, allowed) -> None:
        vals = raw.get(key)
        if not isinstance(vals, (list, tuple)):
            return
        kept = []
        for v in vals:
            tok = _canon_token(v)
            if tok in allowed and tok not in kept:
                kept.append(tok)
        if kept:
            out[key] = kept

    _enum_list("property_types", PROPERTY_TYPES)
    _enum_list("facilities", FACILITY_SYNONYMS)
    _enum_list("meals", MEAL_SYNONYMS)

    stars = raw.get("star_ratings")
    if isinstance(stars, (list, tuple)):
        kept_stars = sorted({int(s) for s in stars
                             if isinstance(s, (int, float)) and 1 <= int(s) <= 5})
        if kept_stars:
            out["star_ratings"] = kept_stars

    def _num(key: str, lo: float | None = None, hi: float | None = None) -> None:
        v = raw.get(key)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if lo is not None:
            f = max(f, lo)
        if hi is not None:
            f = min(f, hi)
        out[key] = f

    _num("price_min", lo=0)
    _num("price_max", lo=0)
    _num("min_review_score", lo=0, hi=10)
    _num("max_distance_km", lo=0.1)
    if "price_min" in out and "price_max" in out and out["price_min"] > out["price_max"]:
        out["price_min"], out["price_max"] = out["price_max"], out["price_min"]

    for key in _BOOL_FILTERS:
        if raw.get(key) is True:      # checkbox semantics: only True filters
            out[key] = True

    if _canon_token(raw.get("sort") or "") in _ALLOWED_SORTS:
        out["sort"] = _canon_token(raw["sort"])

    return out


def _facility_text(hotel: dict) -> str:
    """One lowercase haystack of every free-text field facilities can hide in.
    Space-padded so boundary-anchored synonyms (" tv") match at the edges."""
    parts: list[str] = []
    amenities = hotel.get("amenities")
    if isinstance(amenities, (list, tuple)):
        parts += [str(a) for a in amenities]
    for key in ("highlights", "meal_plan", "tags", "description"):
        v = hotel.get(key)
        if v:
            parts.append(str(v))
    return f" {' | '.join(parts).lower()} "


def _review_score_10(hotel: dict) -> float | None:
    """Review score on a /10 scale across sources (EMT only has TA rating /5)."""
    score = hotel.get("review_score")
    if isinstance(score, (int, float)):
        return float(score)
    ta = hotel.get("ta_rating")
    if isinstance(ta, (int, float)):
        return float(ta) * 2
    return None


def _price_of(hotel: dict) -> float | None:
    p = hotel.get("price_per_night") or hotel.get("estimated_price_per_night")
    return float(p) if isinstance(p, (int, float)) and p > 0 else None


def apply_hotel_filters(hotels: list[dict], filters: dict | None) -> list[dict]:
    """
    HARD-apply sanitized canonical filters to a list of normalized hotels
    (call `sanitize_hotel_filters` first). Hotels missing the data a filter
    needs are excluded — an explicit user filter is a promise, unlike the
    persona/budget soft filters. Callers decide how to handle an empty result.
    The `sort` key is ignored here (ordering is the caller's job).
    """
    if not filters:
        return hotels
    out = hotels

    ptypes = filters.get("property_types")
    if ptypes:
        allowed = set(ptypes)
        out = [h for h in out if tag_property_type(h).get("property_type") in allowed]

    pmin, pmax = filters.get("price_min"), filters.get("price_max")
    if pmin is not None or pmax is not None:
        out = [
            h for h in out
            if (p := _price_of(h)) is not None
            and (pmin is None or p >= pmin)
            and (pmax is None or p <= pmax)
        ]

    min_score = filters.get("min_review_score")
    if min_score is not None:
        out = [h for h in out
               if (s := _review_score_10(h)) is not None and s >= min_score]

    stars = filters.get("star_ratings")
    if stars:
        allowed_stars = set(stars)
        def _star(h: dict) -> int | None:
            try:
                return int(float(h.get("star_rating")))
            except (TypeError, ValueError):
                return None
        out = [h for h in out if _star(h) in allowed_stars]

    facilities = filters.get("facilities")
    if facilities:
        kept = []
        for h in out:
            text = _facility_text(h)
            if all(any(s in text for s in FACILITY_SYNONYMS[key]) for key in facilities):
                kept.append(h)
        out = kept

    meals = filters.get("meals")
    if meals:
        def _meal_ok(h: dict, key: str) -> bool:
            if key == "breakfast_included" and (
                h.get("is_breakfast_included") or "breakfast" in _facility_text(h)
            ):
                return True
            plan = str(h.get("meal_plan") or "").lower()
            return any(s in plan for s in MEAL_SYNONYMS[key])
        out = [h for h in out if all(_meal_ok(h, k) for k in meals)]

    if filters.get("free_cancellation"):
        out = [h for h in out if h.get("free_cancellation")]
    if filters.get("no_prepayment"):
        out = [h for h in out if h.get("no_prepayment")]
    if filters.get("breakfast_included"):
        out = [h for h in out
               if h.get("is_breakfast_included") or "breakfast" in _facility_text(h)]
    if filters.get("couple_friendly"):
        out = [h for h in out if h.get("couple_friendly")]
    if filters.get("pet_friendly"):
        pet_syns = FACILITY_SYNONYMS["pet_friendly"]
        out = [h for h in out if any(s in _facility_text(h) for s in pet_syns)]

    max_km = filters.get("max_distance_km")
    if max_km is not None:
        def _dist(h: dict) -> float | None:
            d = h.get("distance_from_search_km", h.get("distance_km"))
            try:
                return float(d)
            except (TypeError, ValueError):
                return None
        out = [h for h in out if (d := _dist(h)) is not None and d <= max_km]

    return out


def sort_hotels(hotels: list[dict], sort: str | None) -> list[dict]:
    """Order a merged hotel list by a canonical sort key ('recommended' = as-is)."""
    if sort == "price_asc":
        return sorted(hotels, key=lambda h: _price_of(h) or 999_999)
    if sort == "price_desc":
        return sorted(hotels, key=lambda h: -(_price_of(h) or 0))
    if sort == "rating":
        return sorted(hotels, key=lambda h: -(
            float(h.get("review_score") or h.get("star_rating") or h.get("google_rating") or 0)
        ))
    return hotels
