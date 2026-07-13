"""
Nearby-gateway finder — for a leg X → Y with no (or a worse) direct route, find
transport-hub cities near X that DO reach Y, so the trip can be routed X → D → Y.

Used by the trip builder both proactively (offer a cheaper/faster multi-leg
route even when a direct one exists) and as the classic no-route fallback (no
direct X→Y, so surface 2-3 gateways D with a real D→Y route).

Ranking needs coordinates. Geocoding dozens of candidate cities per call would be
far too slow, so hub cities carry static city-centre coordinates here (good
enough for distance ranking); only the origin city is geocoded live when it isn't
already a hub. Availability of the D→Y hop is confirmed with a real
transport.search_all, on the nearest few candidates only.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

from app.services import geo
from app.services.transport import TransportService

logger = logging.getLogger(__name__)

# Major transport hubs → (lat, lng, ISO-3166 country). Biased to airport cities
# Indian travellers actually route through. City-centre coords; ranking only
# needs relative distance, so precision beyond ~city level doesn't matter.
_HUB_COORDS: dict[str, tuple[float, float, str]] = {
    # ── India ──
    "delhi": (28.6139, 77.2090, "IN"),
    "mumbai": (19.0760, 72.8777, "IN"),
    "bengaluru": (12.9716, 77.5946, "IN"),
    "chennai": (13.0827, 80.2707, "IN"),
    "kolkata": (22.5726, 88.3639, "IN"),
    "hyderabad": (17.3850, 78.4867, "IN"),
    "ahmedabad": (23.0225, 72.5714, "IN"),
    "pune": (18.5204, 73.8567, "IN"),
    "goa": (15.2993, 74.1240, "IN"),
    "jaipur": (26.9124, 75.7873, "IN"),
    "lucknow": (26.8467, 80.9462, "IN"),
    "chandigarh": (30.7333, 76.7794, "IN"),
    "kochi": (9.9312, 76.2673, "IN"),
    "thiruvananthapuram": (8.5241, 76.9366, "IN"),
    "kozhikode": (11.2588, 75.7804, "IN"),
    "coimbatore": (11.0168, 76.9558, "IN"),
    "madurai": (9.9252, 78.1198, "IN"),
    "tiruchirapalli": (10.7905, 78.7047, "IN"),
    "visakhapatnam": (17.6868, 83.2185, "IN"),
    "bhubaneswar": (20.2961, 85.8245, "IN"),
    "guwahati": (26.1445, 91.7362, "IN"),
    "patna": (25.5941, 85.1376, "IN"),
    "ranchi": (23.3441, 85.3096, "IN"),
    "raipur": (21.2514, 81.6296, "IN"),
    "nagpur": (21.1458, 79.0882, "IN"),
    "indore": (22.7196, 75.8577, "IN"),
    "bhopal": (23.2599, 77.4126, "IN"),
    "surat": (21.1702, 72.8311, "IN"),
    "vadodara": (22.3072, 73.1812, "IN"),
    "rajkot": (22.3039, 70.8022, "IN"),
    "udaipur": (24.5854, 73.7125, "IN"),
    "jodhpur": (26.2389, 73.0243, "IN"),
    "amritsar": (31.6340, 74.8723, "IN"),
    "srinagar": (34.0837, 74.7973, "IN"),
    "jammu": (32.7266, 74.8570, "IN"),
    "leh": (34.1526, 77.5771, "IN"),
    "dehradun": (30.3165, 78.0322, "IN"),
    "varanasi": (25.3176, 82.9739, "IN"),
    "prayagraj": (25.4358, 81.8463, "IN"),
    "agra": (27.1767, 78.0081, "IN"),
    "gwalior": (26.2183, 78.1828, "IN"),
    "mangaluru": (12.9141, 74.8560, "IN"),
    "mysuru": (12.2958, 76.6394, "IN"),
    "hubli": (15.3647, 75.1240, "IN"),
    "belagavi": (15.8497, 74.4977, "IN"),
    "aurangabad": (19.8762, 75.3433, "IN"),
    "nashik": (19.9975, 73.7898, "IN"),
    "siliguri": (26.7271, 88.3953, "IN"),
    "bagdogra": (26.6812, 88.3286, "IN"),
    "imphal": (24.8170, 93.9368, "IN"),
    "agartala": (23.8315, 91.2868, "IN"),
    "dibrugarh": (27.4728, 94.9120, "IN"),
    "jorhat": (26.7509, 94.2037, "IN"),
    "shillong": (25.5788, 91.8933, "IN"),
    "port blair": (11.6234, 92.7265, "IN"),
    # ── Common international hubs for Indian travellers ──
    "dubai": (25.2048, 55.2708, "AE"),
    "abu dhabi": (24.4539, 54.3773, "AE"),
    "doha": (25.2854, 51.5310, "QA"),
    "singapore": (1.3521, 103.8198, "SG"),
    "bangkok": (13.7563, 100.5018, "TH"),
    "phuket": (7.8804, 98.3923, "TH"),
    "kuala lumpur": (3.1390, 101.6869, "MY"),
    "bali": (-8.4095, 115.1889, "ID"),
    "jakarta": (-6.2088, 106.8456, "ID"),
    "kathmandu": (27.7172, 85.3240, "NP"),
    "colombo": (6.9271, 79.8612, "LK"),
    "male": (4.1755, 73.5093, "MV"),
    "london": (51.5074, -0.1278, "GB"),
    "paris": (48.8566, 2.3522, "FR"),
    "amsterdam": (52.3676, 4.9041, "NL"),
    "frankfurt": (50.1109, 8.6821, "DE"),
    "istanbul": (41.0082, 28.9784, "TR"),
    "new york": (40.7128, -74.0060, "US"),
    "los angeles": (34.0522, -118.2437, "US"),
    "toronto": (43.6532, -79.3832, "CA"),
    "sydney": (-33.8688, 151.2093, "AU"),
    "tokyo": (35.6762, 139.6503, "JP"),
    "hong kong": (22.3193, 114.1694, "HK"),
}

# City → coords geocoded live (origins that aren't hubs). Process-lifetime cache.
_geocode_cache: dict[str, Optional[tuple[float, float]]] = {}

_svc = TransportService()


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km."""
    (lat1, lon1), (lat2, lon2) = a, b
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


async def geocode_city(city: str) -> Optional[tuple[float, float]]:
    """(lat, lng) for a city — hub table first, then a cached live geocode via
    Places. Returns None if it can't be resolved."""
    key = geo.normalize(city)
    if key in _HUB_COORDS:
        lat, lng, _ = _HUB_COORDS[key]
        return (lat, lng)
    if key in _geocode_cache:
        return _geocode_cache[key]
    coords: Optional[tuple[float, float]] = None
    try:
        from app.services.places import search_places

        results = await search_places(city)
        loc = ((results or [{}])[0].get("geometry") or {}).get("location") or {}
        if loc.get("lat") is not None and loc.get("lng") is not None:
            coords = (float(loc["lat"]), float(loc["lng"]))
    except Exception as exc:  # noqa: BLE001 — geocode is best-effort
        logger.warning("[geo_nearby] geocode failed for %s: %s", city, exc)
    _geocode_cache[key] = coords
    return coords


def _best_option(search: dict) -> Optional[dict]:
    """Cheapest available ride across modes from a search_all result, or None if
    the D→Y hop has nothing bookable."""
    candidates: list[tuple[float, str, dict]] = []
    for mode, key, fare_key in (
        ("flight", "flights", "price_total"),
        ("train", "trains", "cheapest_fare"),
        ("bus", "buses", "fare_discounted"),
    ):
        block = search.get(key) or {}
        items = block.get(key) or block.get("items") or []
        for it in items:
            fare = it.get(fare_key) or it.get("fare_original") or it.get("price_total") or 0
            candidates.append((fare or 1e12, mode, it))
    if not candidates:
        return None
    fare, mode, it = min(candidates, key=lambda c: c[0])
    return {"mode": mode, "fare_per_person": None if fare >= 1e12 else fare, "item": it}


async def find_gateways(
    from_city: str,
    dest_city: str,
    date: str,
    travelers: int = 1,
    nearest_n: int = 6,
    want: int = 3,
) -> list[dict]:
    """Gateway cities near `from_city` that have a real transport route to
    `dest_city`, nearest first. Each result:
        {city, distance_km, mode, fare_per_person, search}
    where `search` is the raw D→dest search_all result (kept so the builder can
    build the hop's options without re-searching). Returns [] if none reach dest.
    """
    origin_coords = await geocode_city(from_city)
    if not origin_coords:
        return []

    from_key = geo.normalize(from_city)
    dest_key = geo.normalize(dest_city)
    country = geo.country_of(from_city)          # None → treat as domestic (India-ish)

    # Candidate hubs: same country as origin (or all, when origin country unknown),
    # excluding the origin and destination themselves.
    ranked: list[tuple[float, str]] = []
    for hub, (lat, lng, cc) in _HUB_COORDS.items():
        if hub in (from_key, dest_key):
            continue
        if country and cc != country:
            continue
        ranked.append((_haversine(origin_coords, (lat, lng)), hub))
    ranked.sort(key=lambda x: x[0])
    shortlist = ranked[:nearest_n]
    if not shortlist:
        return []

    # Confirm the hard hop (D → dest) on the nearest few, in parallel.
    searches = await asyncio.gather(
        *[_svc.search_all(hub, dest_city, date, travelers=travelers) for _, hub in shortlist],
        return_exceptions=True,
    )

    out: list[dict] = []
    for (dist, hub), search in zip(shortlist, searches):
        if isinstance(search, Exception) or not isinstance(search, dict):
            continue
        best = _best_option(search)
        if not best:
            continue
        out.append({
            "city": hub.title(),
            "distance_km": round(dist),
            "mode": best["mode"],
            "fare_per_person": best["fare_per_person"],
            "search": search,
        })
        if len(out) >= want:
            break
    return out
