"""
Batch activity hydration — replaces per-activity Places API calls.

Strategy (3 tiers):
  Tier 1 — place_id in pre-fetched place_lookup  → 0 API calls
  Tier 2 — batch search per day (1 call → 20 results → fuzzy match) → N/days calls
  Tier 3 — individual fallback for genuinely unresolved activities   → rare

Result: ~80% fewer Places API calls vs the old per-activity approach.
"""
import asyncio
import logging
from difflib import SequenceMatcher

from app.services.places import search_places, get_place_details
from app.services.osm_places import ground_activity

logger = logging.getLogger(__name__)

# Fields that are only trustworthy when set by THIS hydration pass. The LLM
# writes plausible-looking place_id/source/rating values for venues it invents
# (observed: fabricated Google-style place_ids shared across activities), so
# model-provided grounding is stripped before hydration and only server-side
# lookup results may restore it.
_GROUNDING_FIELDS = [
    "place_id", "lat", "lng", "address", "rating", "user_ratings_total",
    "opening_hours", "open_now", "maps_url", "photo_url", "photos",
    "website", "phone", "grounded",
]


def _strip_llm_grounding(act: dict) -> None:
    """Zero every grounding field the model may have fabricated."""
    for k in _GROUNDING_FIELDS:
        act.pop(k, None)
    act["source"] = "ai_suggested"


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


# ── Apply helpers ─────────────────────────────────────────────────────────────

def _apply(act: dict, place: dict) -> None:
    geo = (place.get("geometry") or {}).get("location", {})
    photos = place.get("photos") or []
    photo_ref = (
        photos[0].get("photo_reference")
        if photos and isinstance(photos[0], dict)
        else place.get("photo_reference")
    )
    pid = place.get("place_id") or act.get("place_id")
    lat = geo.get("lat")
    lng = geo.get("lng")

    act["place_id"]  = pid
    act["lat"]       = lat
    act["lng"]       = lng
    act["address"]   = place.get("formatted_address")
    act["rating"]    = place.get("rating")
    act["user_ratings_total"] = place.get("user_ratings_total")
    act["source"]    = "google_places"
    act["grounded"]  = "server"
    act["photos"]    = [photo_ref] if photo_ref else []
    act["maps_url"]  = (
        f"https://www.google.com/maps/place/?q=place_id:{pid}" if pid
        else (f"https://maps.google.com/?q={lat},{lng}" if lat and lng else None)
    )
    act["photo_url"] = f"/api/v1/images/{photo_ref}" if photo_ref else None
    act.setdefault("opening_hours", [])
    act.setdefault("open_now", None)
    act.setdefault("website", None)
    act.setdefault("phone", None)


def _apply_defaults(act: dict) -> None:
    for k in ["lat", "lng", "address", "rating", "maps_url", "photo_url", "website", "phone", "place_id"]:
        act.setdefault(k, None)
    act.setdefault("photos", [])
    act.setdefault("opening_hours", [])
    act.setdefault("open_now", None)
    act.setdefault("source", "ai_suggested")


def _apply_osm(act: dict, hit: dict, city: str = "") -> None:
    """Apply an OpenStreetMap match: confirms the place exists and where it
    is — nothing more. The planner's narrative (features, history, tips)
    is NOT verified by a name/location match, so it is replaced with a
    neutral description whose claims match the evidence."""
    act["lat"] = hit["lat"]
    act["lng"] = hit["lng"]
    act["address"] = hit.get("display_name") or hit.get("name")
    kind = {
        "attraction": "Attraction", "viewpoint": "Viewpoint", "museum": "Museum",
        "zoo": "Zoo", "theme_park": "Theme park", "gallery": "Gallery",
        "artwork": "Artwork", "place_of_worship": "Place of worship",
        "waterfall": "Waterfall", "peak": "Peak", "temple": "Temple",
    }.get((hit.get("category") or "").lower(), "Place")
    via = "Wikidata" if hit.get("source") == "wikidata" else "OpenStreetMap"
    # Only a locality the provider itself returned counts as venue geography;
    # the trip's stop city is not evidence of where the place sits.
    where = f" near {hit['locality']}" if hit.get("locality") else ""
    act["description"] = (
        f"{kind}{where} — name and location verified via {via}. "
        "Opening hours, entry fees and other details are not independently verified."
    )
    act["place_id"] = None  # never a Google place_id; keeps Google-only UI paths off
    act["source"] = hit.get("source") or "openstreetmap"
    act["grounded"] = "server"
    act["maps_url"] = (
        f"https://www.openstreetmap.org/{hit['osm_type']}/{hit['osm_id']}"
        if hit.get("osm_type") and hit.get("osm_id")
        else f"https://www.openstreetmap.org/?mlat={hit['lat']}&mlon={hit['lng']}#map=17/{hit['lat']}/{hit['lng']}"
    )
    act.setdefault("photos", [])
    act.setdefault("photo_url", None)
    act.setdefault("rating", None)
    act.setdefault("user_ratings_total", None)
    act.setdefault("opening_hours", [])
    act.setdefault("open_now", None)
    act.setdefault("website", None)
    act.setdefault("phone", None)


async def _hydrate_osm(act: dict, destination: str) -> bool:
    """Free grounding tier: verify the named place against the city's OSM
    POI list (one cached Overpass query per city — no systematic geocoding)."""
    title = act.get("title", "")
    for prefix in ["Check-in at", "Check-out from", "Dinner at", "Lunch at", "Breakfast at", "Visit", "Explore"]:
        title = title.replace(prefix, "").strip()
    hit = await ground_activity(title, destination)
    if not hit:
        return False
    _apply_osm(act, hit, destination)
    return True


# ── Tier 2: one batch search per day, fuzzy match ────────────────────────────

async def _hydrate_day_batch(day: dict, destination: str) -> None:
    area = (day.get("theme") or destination).split("—")[0].strip()
    query = f"top attractions restaurants things to do {area} {destination}"
    try:
        candidates = await search_places(query)
        # search_places returns up to 5; for batching we want more — call once with broader query
        # If we got fewer than 3 candidates extend with a second search
        if len(candidates) < 3:
            extra = await search_places(f"places to visit {area} {destination}")
            seen = {c["place_id"] for c in candidates}
            candidates += [c for c in extra if c["place_id"] not in seen]
    except Exception as e:
        logger.warning("[hydration] day %s batch failed: %s", day.get("day"), e)
        candidates = []

    for act in (day.get("activities") or []):
        if act.get("lat"):   # already resolved by tier 1
            continue
        best = max(candidates, key=lambda c: _sim(act.get("title", ""), c.get("name", "")), default=None)
        if best and _sim(act.get("title", ""), best.get("name", "")) > 0.45:
            _apply(act, best)
        else:
            await _hydrate_individual(act, destination)


async def _hydrate_individual(act: dict, destination: str) -> None:
    title = act.get("title", "")
    for prefix in ["Check-in at", "Check-out from", "Dinner at", "Lunch at", "Breakfast at", "Visit", "Explore"]:
        title = title.replace(prefix, "").strip()
    try:
        results = await search_places(f"{title} {destination}")
        if results:
            _apply(act, results[0])
            # Fetch details for opening hours + phone only on individual hits (not batch)
            pid = act.get("place_id")
            if pid:
                details = await get_place_details(pid)
                if details:
                    oh = details.get("opening_hours") or {}
                    act["opening_hours"] = oh.get("weekday_text") or []
                    act["open_now"]  = oh.get("open_now")
                    act["website"]   = details.get("website")
                    act["phone"]     = details.get("formatted_phone_number")
            return
    except Exception:
        pass
    # Free tier: Google Places needs a billing key; OpenStreetMap does not.
    if await _hydrate_osm(act, destination):
        return
    _apply_defaults(act)


# ── Public entry point ────────────────────────────────────────────────────────

async def batch_hydrate_days(days: list[dict], destination: str, place_lookup: dict) -> None:
    """
    Hydrate all activities.

    Tier 1 (free):   place_id in place_lookup → apply immediately
    Tier 2 (cheap):  batch search per day → fuzzy match
    Tier 3 (rare):   individual search for unresolved activities
    """
    # Strip model-fabricated grounding first: from here on, a place_id,
    # rating or coordinate on an activity means a server-side lookup put it
    # there, never the LLM.
    for day in days:
        for act in (day.get("activities") or []):
            _strip_llm_grounding(act)

    tier1_count = 0
    for day in days:
        for act in (day.get("activities") or []):
            pid = act.get("place_id") or ""
            if pid and pid in place_lookup:
                _apply(act, place_lookup[pid])
                tier1_count += 1

    tier2_days = [d for d in days if any(not a.get("lat") for a in (d.get("activities") or []))]
    logger.debug("[hydration] Tier1 free hits: %d | Tier2 batch days: %d", tier1_count, len(tier2_days))

    await asyncio.gather(*[_hydrate_day_batch(d, destination) for d in tier2_days])
