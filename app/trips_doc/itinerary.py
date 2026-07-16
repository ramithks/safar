"""
Itinerary day editing — the instant path for "make this day mine".

Add / remove / reorder places inside one day of one stop: the client sends
the day's FULL new activities list and the backend replaces it — one explicit
JSON update, no diffing protocol, no AI. Adding a place is grounded: the
place-search endpoint returns Google-Places-verified candidates shaped like
the generator's own activities, so a hand-added spot renders identically to
a planned one (photo, rating, maps link).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .serialize import compute_budget  # noqa: F401  (budget untouched; kept for parity imports)

MAX_ACTIVITIES_PER_DAY = 15
_MAX_ACTIVITY_BYTES = 8_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def edit_day_activities(doc: dict, stop_id: str, day_index: int,
                        activities: list) -> tuple[dict, str]:
    """Replace one day's activities. Raises ValueError on bad targets/shape."""
    stops = doc.get("itinerary") or []
    stop = next((s for s in stops if s.get("stop_id") == stop_id), None)
    if stop is None:  # allow city-name targeting, same as hotel selection
        key = (stop_id or "").strip().lower()
        stop = next((s for s in stops if (s.get("city") or "").strip().lower() == key), None)
    if stop is None:
        raise ValueError(f"No stop '{stop_id}' on this trip")
    days = stop.get("days") or []
    if not (0 <= int(day_index) < len(days)):
        raise ValueError(f"{stop.get('city')} has no day {day_index + 1}")

    if not isinstance(activities, list) or len(activities) > MAX_ACTIVITIES_PER_DAY:
        raise ValueError(f"A day holds at most {MAX_ACTIVITIES_PER_DAY} places")
    cleaned: list[dict] = []
    for a in activities:
        if not isinstance(a, dict) or not (a.get("title") or a.get("name")):
            raise ValueError("Each place needs at least a title")
        if len(json.dumps(a, default=str)) > _MAX_ACTIVITY_BYTES:
            raise ValueError("A place's details are too large")
        cleaned.append(a)

    days[int(day_index)]["activities"] = cleaned
    doc["updated_at"] = _now()
    n = len(cleaned)
    return doc, f"Day {int(day_index) + 1} in {stop.get('city')} updated — {n} place{'s' if n != 1 else ''}"


# ── grounded place search (the "Add a place" source) ─────────────────────────

def _shape_place(p: dict) -> dict:
    """A Google Places text-search result → the activity shape DayCard renders."""
    from app.services.places import photo_proxy_path

    loc = (p.get("geometry") or {}).get("location") or {}
    ref = p.get("photo_reference")
    return {k: v for k, v in {
        "title": p.get("name"),
        "type": "attraction",
        "category": (p.get("types") or ["attraction"])[0],
        "description": p.get("formatted_address"),
        "address": p.get("formatted_address"),
        "place_id": p.get("place_id"),
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "rating": p.get("rating"),
        "user_ratings_total": p.get("user_ratings_total"),
        "photo_url": photo_proxy_path(ref) if ref else None,
        "maps_url": f"https://www.google.com/maps/place/?q=place_id:{p['place_id']}"
        if p.get("place_id") else None,
        "source": "user_added",
    }.items() if v is not None}


async def search_places_for_stop(doc: dict, stop_id: str, query: str) -> list[dict]:
    """Verified place candidates near a stop's city — never hallucinated."""
    from app.services.places import search_places

    stops = doc.get("itinerary") or []
    stop = next((s for s in stops if s.get("stop_id") == stop_id), None) \
        or next((s for s in stops
                 if (s.get("city") or "").strip().lower() == (stop_id or "").strip().lower()), None)
    city = (stop or {}).get("city") or (doc.get("spec") or {}).get("destination") or ""
    results = await search_places(f"{query}, {city}" if city else query)
    return [_shape_place(p) for p in (results or [])[:6] if p.get("name")]
