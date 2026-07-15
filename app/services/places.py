"""
Google Places API helpers — used by plan_trip and plan_day_out.

Optimisations:
- Single persistent httpx.AsyncClient shared across all calls (connection reuse).
- enrich_venue skips the extra get_place_details round-trip; the textsearch
  result already contains geometry, photo_reference, address and rating.
"""
import httpx
import logging
from app.config import settings
from app.core.async_http import loop_client

logger = logging.getLogger(__name__)

# ── Shared HTTP client ────────────────────────────────────────────────────────
# Bound to the running loop (see app.core.async_http) — this module runs inside
# the Celery pipeline, where each task uses a fresh, short-lived event loop.
_clients_by_loop: dict[int, httpx.AsyncClient] = {}


def _http_client() -> httpx.AsyncClient:
    return loop_client(
        _clients_by_loop,
        lambda: httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
        ),
    )


def _mask(s: str) -> str:
    """Mask the last 28 chars of a key — keeps prefix visible for debugging."""
    if not s or len(s) < 8:
        return "***"
    return s[:6] + "*****"


def _maps_key() -> str:
    if not settings.google_maps_api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is not set")
    return settings.google_maps_api_key


async def search_places(query: str, type_filter: str | None = None) -> list[dict]:
    """Text search Google Places, returns up to 5 results."""
    key = _maps_key()
    final_query = f"{query} {type_filter}" if type_filter else query
    try:
        r = await _http_client().get(
            "https://maps.googleapis.com/maps/api/place/textsearch/json",
            params={"query": final_query, "key": key},
        )
        r.raise_for_status()
        data = r.json()
        return [
            {
                "place_id": p.get("place_id"),
                "name": p.get("name"),
                "types": p.get("types", []),
                "rating": p.get("rating"),
                "user_ratings_total": p.get("user_ratings_total"),
                "price_level": p.get("price_level"),
                "geometry": p.get("geometry"),
                "photo_reference": (p.get("photos") or [{}])[0].get("photo_reference"),
                "formatted_address": p.get("formatted_address"),
            }
            for p in data.get("results", [])
        ][:5]
    except Exception as e:
        logger.warning("[places] search_places error for %r: %s", query, type(e).__name__)
        return []


async def get_place_details(place_id: str) -> dict | None:
    """Fetch place details (address, photos, hours, geometry, rating)."""
    key = _maps_key()
    fields = "name,formatted_address,photos,opening_hours,geometry,rating,user_ratings_total,types,website,formatted_phone_number"
    try:
        r = await _http_client().get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": place_id, "fields": fields, "key": key},
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        logger.warning("[places] get_place_details error for %s: %s", place_id, type(e).__name__)
        return None


async def discover_places(destination: str) -> dict:
    """Parallel multi-category place discovery for a destination."""
    import asyncio
    categories = [
        ("attractions", f"top attractions in {destination}"),
        ("food", f"best local food cafes restaurants in {destination}"),
        ("nightlife", f"nightlife bars clubs in {destination}"),
        ("hidden_gems", f"hidden gems unique places in {destination}"),
    ]
    results = await asyncio.gather(*[search_places(q) for _, q in categories])
    return {key: res for (key, _), res in zip(categories, results)}


async def enrich_venue(venue: dict, location: str) -> dict:
    """
    Enrich a venue dict with real place_id, lat/lng, photo_url, address, opening_hours.
    Uses a single textsearch call (no extra get_place_details round-trip).
    Photo URL is a proxy path — the Google API key is never exposed to the frontend.
    """
    try:
        places = await search_places(f"{venue['name']} {location}")
        place = places[0] if places else None
        if not place:
            return venue

        place_id = place["place_id"]
        geo = (place.get("geometry") or {}).get("location", {})
        lat = geo.get("lat")
        lng = geo.get("lng")
        address = place.get("formatted_address")
        rating = place.get("rating") or venue.get("rating")
        photo_ref = place.get("photo_reference")

        maps_url = (
            f"https://www.google.com/maps/place/?q=place_id:{place_id}"
            if place_id
            else (f"https://maps.google.com/?q={lat},{lng}" if lat and lng else None)
        )
        photo_url_str = f"/api/v1/images/{photo_ref}" if photo_ref else None

        return {
            **venue,
            "place_id": place_id,
            "photo_reference": photo_ref,
            "photo_url": photo_url_str,
            "lat": lat,
            "lng": lng,
            "address": address,
            "rating": rating,
            "maps_url": maps_url,
        }
    except Exception as e:
        logger.warning("[places] enrich_venue error for %r: %s", venue.get("name"), type(e).__name__)
        return venue


def _google_photo_url(photo_reference: str, maxwidth: int = 800) -> str:
    """Build a Google Places photo URL (server-side only — never send to frontend)."""
    key = _maps_key()
    return f"https://maps.googleapis.com/maps/api/place/photo?maxwidth={maxwidth}&photo_reference={photo_reference}&key={key}"


def photo_url(photo_reference: str, maxwidth: int = 800) -> str:
    """Deprecated alias — use photo_proxy_path() for frontend, _google_photo_url() server-side."""
    return _google_photo_url(photo_reference, maxwidth)


def photo_proxy_path(photo_reference: str) -> str:
    """Return a proxy path for a photo reference — safe to send to the frontend."""
    return f"/api/v1/images/{photo_reference}"


def extract_hero_image(itinerary: dict, maps_key: str | None = None) -> str | None:
    """Extract the best hero image from an itinerary_data JSONB object."""
    if not itinerary:
        return None
    if itinerary.get("hero_image_url"):
        return itinerary["hero_image_url"]
    ref = itinerary.get("cover_photo_ref") or itinerary.get("destination_photo_ref")
    if ref:
        return f"/api/v1/images/{ref}"
    for day in itinerary.get("days", []):
        for act in day.get("activities", []):
            if act.get("image_url"):
                return act["image_url"]
            photos = act.get("photos", [])
            if photos:
                p = photos[0]
                if isinstance(p, str) and p.startswith("http"):
                    return p
                if isinstance(p, str):
                    return f"/api/v1/images/{p}"
    return None
