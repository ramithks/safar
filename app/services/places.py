"""
Google Places API helpers — used by plan_trip and plan_day_out.

Optimisations:
- Single persistent httpx.AsyncClient shared across all calls (connection reuse).
- enrich_venue skips the extra get_place_details round-trip; the textsearch
  result already contains geometry, photo_reference, address and rating.
"""
import httpx
from app.config import settings

# ── Shared HTTP client ────────────────────────────────────────────────────────
_http_client = httpx.AsyncClient(
    timeout=10.0,
    limits=httpx.Limits(max_connections=30, max_keepalive_connections=15),
)


def _maps_key() -> str:
    if not settings.google_maps_api_key:
        raise RuntimeError("GOOGLE_MAPS_API_KEY is not set")
    return settings.google_maps_api_key


async def search_places(query: str, type_filter: str | None = None) -> list[dict]:
    """Text search Google Places, returns up to 5 results."""
    key = _maps_key()
    final_query = f"{query} {type_filter}" if type_filter else query
    try:
        r = await _http_client.get(
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
        print(f"[places] search_places error for '{query}': {e}")
        return []


async def get_place_details(place_id: str) -> dict | None:
    """Fetch place details (address, photos, hours, geometry, rating)."""
    key = _maps_key()
    fields = "name,formatted_address,photos,opening_hours,geometry,rating,user_ratings_total,types"
    try:
        r = await _http_client.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id": place_id, "fields": fields, "key": key},
        )
        r.raise_for_status()
        return r.json().get("result")
    except Exception as e:
        print(f"[places] get_place_details error for {place_id}: {e}")
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
    """
    key = _maps_key()
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
        photo_url_str = (
            f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=600&photo_reference={photo_ref}&key={key}"
            if photo_ref
            else None
        )

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
        print(f"[places] enrich_venue error for '{venue.get('name')}': {e}")
        return venue


def photo_url(photo_reference: str, maxwidth: int = 800) -> str:
    """Build a Google Places photo URL."""
    key = _maps_key()
    return f"https://maps.googleapis.com/maps/api/place/photo?maxwidth={maxwidth}&photo_reference={photo_reference}&key={key}"


def extract_hero_image(itinerary: dict, maps_key: str | None = None) -> str | None:
    """Extract the best hero image from an itinerary_data JSONB object."""
    if not itinerary:
        return None
    if itinerary.get("hero_image_url"):
        return itinerary["hero_image_url"]
    if itinerary.get("destination_photo_ref") and maps_key:
        return f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=800&photoreference={itinerary['destination_photo_ref']}&key={maps_key}"
    for day in itinerary.get("days", []):
        for act in day.get("activities", []):
            if act.get("image_url"):
                return act["image_url"]
            photos = act.get("photos", [])
            if photos:
                p = photos[0]
                if isinstance(p, str) and p.startswith("http"):
                    return p
                if isinstance(p, str) and maps_key:
                    return f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=800&photoreference={p}&key={maps_key}"
    return None
