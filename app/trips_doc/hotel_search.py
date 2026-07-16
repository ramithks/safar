"""
Explicit hotel re-search for one stop of a TripDoc — the "Search hotels"
button (optionally near a landmark: "near Mall Road").

Geocode the landmark with Google Places, run the multi-source HotelService
search fresh, rank by distance to the landmark, and REPLACE that stop's
hotel options in the doc. This is an expensive, explicitly-invoked operation
— selections never come here.

Ported from the copilot's app/trip_state/hotel_search.py minus the chat
reply machinery; dies with nothing when app/trip_state is deleted.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from app.services.hotels import HotelService
from app.services.hotels_filters import sanitize_hotel_filters
from app.services.places import search_places

from .serialize import compute_budget, trim_hotel

logger = logging.getLogger(__name__)

_MAX_RESULTS = 12
_RADII_KM = (2.0, 5.0, 10.0)   # widen until enough hotels survive
_MIN_NEARBY = 4


def _hotel_coords(hotel: dict) -> tuple[float, float] | None:
    """Providers disagree on shape: lat/latitude at top level or nested under
    location/geo — and `location` is an address STRING on some sources."""
    nested: dict = {}
    for key in ("location", "geo", "coordinates", "coords"):
        v = hotel.get(key)
        if isinstance(v, dict):
            nested = v
            break

    def pick(*keys: str) -> float | None:
        for src in (hotel, nested):
            for k in keys:
                try:
                    f = float(src.get(k))  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                if f != 0:
                    return f
        return None

    lat = pick("latitude", "lat")
    lng = pick("longitude", "lng", "lon")
    return (lat, lng) if lat is not None and lng is not None else None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


async def _resolve_landmark(query: str, city: str) -> dict | None:
    results = await search_places(f"{query}, {city}")
    for p in results:
        loc = (p.get("geometry") or {}).get("location") or {}
        if loc.get("lat") and loc.get("lng"):
            return {"name": p.get("name") or query, "lat": loc["lat"], "lng": loc["lng"]}
    return None


async def search_stop_hotels(doc: dict, stop_id: str, query: str = "",
                             raw_filters: dict | None = None) -> tuple[dict, str]:
    """Fresh hotel search for one stop; replaces its options in the doc.
    Raises ValueError on a bad stop target (endpoint → 400)."""
    stops = doc.get("itinerary") or []
    stop = next((s for s in stops if s.get("stop_id") == stop_id), None)
    if stop is None:
        key = (stop_id or "").strip().lower()
        stop = next((s for s in stops if (s.get("city") or "").strip().lower() == key), None)
    if stop is None:
        raise ValueError(f"No stop '{stop_id}' on this trip")

    city = stop.get("city")
    spec = doc.get("spec") or {}
    try:
        checkin_dt = datetime.strptime((stop.get("arrival_date") or "")[:10], "%Y-%m-%d")
    except ValueError:
        checkin_dt = datetime.now() + timedelta(days=7)
    nights = max(int(stop.get("nights") or 1), 1)
    checkin = checkin_dt.strftime("%Y-%m-%d")
    checkout = (checkin_dt + timedelta(days=nights)).strftime("%Y-%m-%d")

    landmark = await _resolve_landmark(query, city) if query else None
    filters = sanitize_hotel_filters(raw_filters)
    radius_km = filters.pop("max_distance_km", None) if landmark else None

    result = await HotelService().search(
        city, checkin, checkout,
        adults=max(int(spec.get("travelers") or 1), 1),
        budget=spec.get("budget_tier") or "mid",
        filters=filters or None,
    )
    hotels = [dict(h) for h in (result.get("hotels") or [])]
    if not hotels:
        raise ValueError(f"No bookable hotels came back for {city} right now — try again in a minute")

    where = city
    if landmark:
        where = landmark["name"]
        located: list[dict] = []
        unlocated: list[dict] = []
        for h in hotels:
            c = _hotel_coords(h)
            if c is None:
                unlocated.append(h)
                continue
            h["distance_km"] = round(
                _haversine_km(c[0], c[1], landmark["lat"], landmark["lng"]), 1)
            h["search_landmark"] = landmark["name"]
            located.append(h)
        located.sort(key=lambda h: h["distance_km"])
        if radius_km:
            kept = [h for h in located if h["distance_km"] <= radius_km]
            located = kept or located
        else:
            for radius in _RADII_KM:
                kept = [h for h in located if h["distance_km"] <= radius]
                if len(kept) >= _MIN_NEARBY:
                    located = kept
                    break
        hotels = (located + unlocated)[:_MAX_RESULTS]
    else:
        hotels = hotels[:_MAX_RESULTS]

    trimmed = [trim_hotel(h) for h in hotels]
    cheapest = min(trimmed, key=lambda h: h.get("price_per_night") or 1e12) if trimmed else None
    stop["hotels"] = {
        "options": trimmed,
        "selected_id": None,          # a fresh list voids the old pick
        "recommended_id": cheapest.get("id") if cheapest else None,
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "search_query": query or None,
    }
    doc["budget"] = compute_budget(doc)
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()
    return doc, f"Found {len(trimmed)} stays near {where}"
