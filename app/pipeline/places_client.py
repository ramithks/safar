"""
Google Places API client with disk-based caching.

Since Gemini 2.5 Flash returns lat/lng (approximate) directly, we use:
  - Nearby Search (radius=1km) → top result is primary match, rest are suggestions
  - Text Search only when Gemini returned no coordinates
  - Place Details (full) only for the PRIMARY match — suggestions use free Nearby data
  - diskcache by place_id (7-day TTL) — 60%+ hit rate after first month

This means: 1 Nearby Search + 1 Place Details per frame = ~$0.037/location.
Suggestions come free from the same Nearby Search response.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import diskcache
import googlemaps

from app.config import settings
from app.pipeline.vision_client import VisionResult
from app.models.response import (
    PlaceDetail, PlaceAddress, Coordinates, PlacePhoto
)

logger = logging.getLogger(__name__)

# Essentials + rating fields only ($5/1k base, rating bumps to Pro at $17/1k)
PLACE_DETAIL_FIELDS = [
    "place_id",
    "name",
    "formatted_address",
    "address_components",
    "geometry",
    "types",
    "photos",
    "url",
    "rating",
    "user_ratings_total",
    "website",
    "international_phone_number",
]

# How many nearby results to return as suggestions (excluding primary)
MAX_SUGGESTIONS = 4

# Search radius in metres — wider than the old 200m because Gemini coords can drift
NEARBY_RADIUS = 1000

_cache: Optional[diskcache.Cache] = None
_gmaps: Optional[googlemaps.Client] = None


@dataclass
class EnrichmentResult:
    primary: Optional[PlaceDetail]
    suggestions: list[PlaceDetail] = field(default_factory=list)


def _get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        os.makedirs(settings.places_cache_dir, exist_ok=True)
        _cache = diskcache.Cache(
            settings.places_cache_dir,
            size_limit=500 * 1024 * 1024,
        )
    return _cache


def _get_client() -> Optional[googlemaps.Client]:
    global _gmaps
    if _gmaps is None:
        if not settings.google_maps_api_key:
            logger.info("GOOGLE_MAPS_API_KEY not set — Places enrichment disabled")
            return None
        try:
            _gmaps = googlemaps.Client(key=settings.google_maps_api_key)
        except ValueError as e:
            logger.warning(f"GOOGLE_MAPS_API_KEY is invalid, Places enrichment disabled: {e}")
            return None
    return _gmaps


def enrich_with_places(
    vision_results: list[VisionResult],
) -> list[tuple[VisionResult, EnrichmentResult]]:
    """
    For each confident vision result, fetch primary place + nearby suggestions.
    Returns list of (VisionResult, EnrichmentResult) pairs.
    """
    enriched = []
    primary_cache: dict[str, Optional[PlaceDetail]] = {}  # dedup within job

    for vr in vision_results:
        if vr.confidence < settings.min_vision_confidence:
            enriched.append((vr, EnrichmentResult(primary=None)))
            continue

        result = _lookup(vr, primary_cache)
        enriched.append((vr, result))

    found = sum(1 for _, r in enriched if r.primary is not None)
    logger.info(f"Places enrichment: {found}/{len(vision_results)} primary places found")
    return enriched


def _lookup(
    vision: VisionResult,
    primary_cache: dict[str, Optional[PlaceDetail]],
) -> EnrichmentResult:
    gmaps = _get_client()
    if not gmaps:
        return EnrichmentResult(primary=None)

    if vision.lat is not None and vision.lng is not None:
        # Nearby Search — returns primary + suggestions in one call
        return _nearby_lookup(gmaps, vision.lat, vision.lng, primary_cache)
    elif vision.landmark_name:
        # Text Search fallback (no coordinates from Gemini)
        query = " ".join(p for p in [vision.landmark_name, vision.city, vision.country] if p)
        return _text_lookup(gmaps, query, primary_cache)

    return EnrichmentResult(primary=None)


def _nearby_lookup(
    gmaps: googlemaps.Client,
    lat: float,
    lng: float,
    primary_cache: dict[str, Optional[PlaceDetail]],
) -> EnrichmentResult:
    """
    Single Nearby Search call returns up to MAX_SUGGESTIONS+1 results.
    Top result → full Place Details (primary).
    Remaining results → lightweight cards from Nearby response (suggestions, free).
    """
    try:
        response = gmaps.places_nearby(
            location=(lat, lng),
            radius=NEARBY_RADIUS,
            rank_by="prominence",
            language="en",
        )
    except Exception as e:
        logger.warning(f"Nearby search failed ({lat},{lng}): {e}")
        return EnrichmentResult(primary=None)

    results = response.get("results", [])
    if not results:
        return EnrichmentResult(primary=None)

    primary_raw = results[0]
    suggestion_raws = results[1: MAX_SUGGESTIONS + 1]

    # Full Place Details for primary (cached)
    primary_id = primary_raw.get("place_id")
    if primary_id in primary_cache:
        primary = primary_cache[primary_id]
    else:
        primary = _get_place_details(gmaps, primary_id)
        primary_cache[primary_id] = primary

    # Lightweight suggestion cards — built from Nearby Search data, no extra API call
    suggestions = [_parse_nearby_result(r) for r in suggestion_raws if r.get("place_id")]

    return EnrichmentResult(primary=primary, suggestions=suggestions)


def _text_lookup(
    gmaps: googlemaps.Client,
    query: str,
    primary_cache: dict[str, Optional[PlaceDetail]],
) -> EnrichmentResult:
    cache = _get_cache()
    cache_key = f"text_search:{query.lower().strip()}"

    place_id = cache.get(cache_key)
    if not place_id:
        try:
            result = gmaps.places(query=query, language="en")
            results = result.get("results", [])
            if not results:
                return EnrichmentResult(primary=None)
            place_id = results[0]["place_id"]
            cache.set(cache_key, place_id, expire=settings.places_cache_ttl_seconds)
        except Exception as e:
            logger.warning(f"Text search failed ('{query}'): {e}")
            return EnrichmentResult(primary=None)

    if place_id in primary_cache:
        return EnrichmentResult(primary=primary_cache[place_id])

    primary = _get_place_details(gmaps, place_id)
    primary_cache[place_id] = primary
    return EnrichmentResult(primary=primary)


def _get_place_details(gmaps: googlemaps.Client, place_id: str) -> Optional[PlaceDetail]:
    cache = _get_cache()
    cache_key = f"place_details:{place_id}"

    if cache_key in cache:
        logger.debug(f"Places cache hit: {place_id}")
        return cache[cache_key]

    try:
        response = gmaps.place(
            place_id=place_id,
            fields=PLACE_DETAIL_FIELDS,
            language="en",
        )
        raw = response.get("result", {})
        if not raw:
            return None
        detail = _parse_place_detail(raw)
        cache.set(cache_key, detail, expire=settings.places_cache_ttl_seconds)
        return detail
    except Exception as e:
        logger.warning(f"Place Details failed ({place_id}): {e}")
        return None


def _parse_nearby_result(raw: dict) -> PlaceDetail:
    """
    Build a lightweight PlaceDetail from a Nearby Search result.
    No extra API call — uses the fields included free in the Nearby Search response.
    """
    loc = raw.get("geometry", {}).get("location", {})
    coords = Coordinates(lat=loc["lat"], lng=loc["lng"]) if loc else None

    types = raw.get("types", [])
    place_id = raw.get("place_id", "")

    # Build a Maps URL for the suggestion using place_id (precise deep-link)
    maps_url = (
        f"https://www.google.com/maps/place/?q=place_id:{place_id}"
        if place_id else None
    )

    # Thumbnail photo from Nearby Search (photo_reference)
    photos = []
    for photo in raw.get("photos", [])[:1]:
        ref = photo.get("photo_reference")
        if ref and settings.google_maps_api_key:
            photos.append(PlacePhoto(
                url=(
                    f"https://maps.googleapis.com/maps/api/place/photo"
                    f"?maxwidth=400&photo_reference={ref}&key={settings.google_maps_api_key}"
                ),
                width=photo.get("width"),
                height=photo.get("height"),
            ))

    return PlaceDetail(
        place_id=place_id,
        name=raw.get("name", ""),
        category=_infer_category(types),
        address=PlaceAddress(
            formatted=raw.get("vicinity", ""),
        ) if raw.get("vicinity") else None,
        coordinates=coords,
        rating=raw.get("rating"),
        rating_count=raw.get("user_ratings_total"),
        photos=photos,
        google_maps_url=maps_url,
        types=types,
    )


def _parse_place_detail(raw: dict) -> PlaceDetail:
    address = _parse_address(raw)

    coords = None
    if "geometry" in raw and "location" in raw["geometry"]:
        loc = raw["geometry"]["location"]
        coords = Coordinates(lat=loc["lat"], lng=loc["lng"])

    photos = []
    for photo in raw.get("photos", [])[:3]:
        ref = photo.get("photo_reference")
        if ref and settings.google_maps_api_key:
            photos.append(PlacePhoto(
                url=(
                    f"https://maps.googleapis.com/maps/api/place/photo"
                    f"?maxwidth=800&photo_reference={ref}&key={settings.google_maps_api_key}"
                ),
                width=photo.get("width"),
                height=photo.get("height"),
            ))

    types = raw.get("types", [])

    return PlaceDetail(
        place_id=raw.get("place_id", ""),
        name=raw.get("name", ""),
        category=_infer_category(types),
        address=address,
        coordinates=coords,
        rating=raw.get("rating"),
        rating_count=raw.get("user_ratings_total"),
        photos=photos,
        website=raw.get("website"),
        phone=raw.get("international_phone_number"),
        google_maps_url=raw.get("url"),
        types=types,
    )


def _parse_address(raw: dict) -> Optional[PlaceAddress]:
    formatted = raw.get("formatted_address", "")
    if not formatted:
        return None

    component_map: dict[str, str] = {}
    for comp in raw.get("address_components", []):
        for t in comp.get("types", []):
            component_map[t] = comp.get("long_name", "")

    number = component_map.get("street_number", "")
    route = component_map.get("route", "")
    street = f"{number} {route}".strip() if number and route else route or None

    return PlaceAddress(
        formatted=formatted,
        street=street,
        city=(
            component_map.get("locality")
            or component_map.get("sublocality_level_1")
            or component_map.get("administrative_area_level_2")
        ),
        region=component_map.get("administrative_area_level_1"),
        country=component_map.get("country"),
        country_code=next(
            (c.get("short_name") for c in raw.get("address_components", [])
             if "country" in c.get("types", [])),
            None,
        ),
        postal_code=component_map.get("postal_code"),
    )


def _infer_category(types: list[str]) -> Optional[str]:
    priority_map = {
        "tourist_attraction": "tourist_attraction",
        "natural_feature": "nature",
        "park": "park",
        "museum": "museum",
        "restaurant": "restaurant",
        "cafe": "cafe",
        "bar": "bar",
        "lodging": "hotel",
        "shopping_mall": "shopping",
        "store": "shopping",
        "place_of_worship": "place_of_worship",
        "airport": "transport",
        "train_station": "transport",
        "transit_station": "transport",
        "beach": "beach",
        "amusement_park": "entertainment",
        "art_gallery": "art",
        "night_club": "nightlife",
        "spa": "wellness",
    }
    for t in types:
        if t in priority_map:
            return priority_map[t]
    return "point_of_interest"
