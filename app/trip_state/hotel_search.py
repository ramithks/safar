"""
Live hotel search for the copilot — "show me hotels near Mall Road".

The generation pipeline fetches hotels once, anchored on the destination
centre; the copilot previously could only re-pick from that list. This module
re-searches on demand: geocode the landmark/area with Google Places, run the
multi-source HotelService search fresh, rank the merged list by distance to
the landmark, and patch /stay/hotels so the workspace updates in place.
"""
import logging
import math
from datetime import datetime, timedelta

from app.services.hotels import HotelService
from app.services.places import search_places

from .patch import PatchOp

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


async def _resolve_landmark(query: str, destination: str) -> dict | None:
    results = await search_places(f"{query}, {destination}")
    for p in results:
        loc = (p.get("geometry") or {}).get("location") or {}
        if loc.get("lat") and loc.get("lng"):
            return {"name": p.get("name") or query, "lat": loc["lat"], "lng": loc["lng"]}
    return None


def _short_name(hotel: dict, limit: int = 42) -> str:
    """Scraper names can be keyword-stuffed ('!!! #Beststay…') — trim for chat."""
    name = (hotel.get("name") or "this hotel").split("!!!")[0].split("#")[0].strip(" -–|")
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _price_label(hotel: dict) -> str:
    price = hotel.get("price_per_night") or hotel.get("price")
    if isinstance(price, (int, float)) and price > 0:
        return f"₹{round(price):,}/night"
    return "price on booking"


async def handle_search_hotels(state: dict, query: str) -> tuple[list[PatchOp], str]:
    """Fresh hotel search near a landmark/area; replaces the /stay/hotels list."""
    req = state.get("request") or {}
    destination = req.get("destination")
    if not destination:
        return [], "I don't know this trip's destination yet, so I can't search hotels."

    try:
        checkin_dt = datetime.strptime((req.get("departure_date") or "")[:10], "%Y-%m-%d")
    except ValueError:
        checkin_dt = datetime.now() + timedelta(days=7)
    nights = max(int(req.get("stay_nights") or 1), 1)
    checkin = checkin_dt.strftime("%Y-%m-%d")
    checkout = (checkin_dt + timedelta(days=nights)).strftime("%Y-%m-%d")

    landmark = await _resolve_landmark(query, destination) if query else None

    result = await HotelService().search(
        destination, checkin, checkout,
        adults=max(int(req.get("travelers") or 1), 1),
        budget=req.get("budget_tier") or "mid",
    )
    hotels = [dict(h) for h in (result.get("hotels") or [])]
    if not hotels:
        return [], (
            f"I searched live but no bookable hotels came back for {destination} "
            "right now — give it another try in a minute."
        )

    where = destination
    if landmark:
        where = landmark["name"]
        located: list[dict] = []
        unlocated: list[dict] = []
        for h in hotels:
            c = _hotel_coords(h)
            if c is None:
                unlocated.append(h)
                continue
            h["distance_from_search_km"] = round(
                _haversine_km(c[0], c[1], landmark["lat"], landmark["lng"]), 1
            )
            h["search_landmark"] = landmark["name"]
            located.append(h)
        located.sort(key=lambda h: h["distance_from_search_km"])
        for radius in _RADII_KM:
            kept = [h for h in located if h["distance_from_search_km"] <= radius]
            if len(kept) >= _MIN_NEARBY:
                located = kept
                break
        hotels = (located + unlocated)[:_MAX_RESULTS]
    else:
        hotels = hotels[:_MAX_RESULTS]

    patches = [PatchOp(op="set", path="/stay/hotels", value=hotels)]

    top = hotels[:3]
    highlights = "; ".join(
        f"{_short_name(h)} ({_price_label(h)}"
        + (f", {h['distance_from_search_km']} km away)" if h.get("distance_from_search_km") is not None else ")")
        for h in top
    )
    reply = (
        f"Found {len(hotels)} stays near {where} — closest first: {highlights}. "
        "They're all in your Stays tab now; tell me which one to lock in."
    )
    if query and not landmark:
        reply = (
            f"I couldn't pin “{query}” on the map, so I refreshed hotels across "
            f"{destination} instead: {highlights}. Tell me which one to lock in."
        )
    return patches, reply
