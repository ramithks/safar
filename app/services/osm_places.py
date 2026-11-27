"""
Free place grounding via OpenStreetMap — no API key, no billing.

Design choice driven by the endpoint usage policies: Nominatim's policy
forbids heavy/systematic machine-generated geocoding, so we do NOT geocode
each activity. Instead, per destination city (one user action each):
  1. geocode the CITY once via Nominatim (cached 7 days; this is the kind of
     single on-demand lookup the public endpoint exists for), and
  2. fetch the city's named POIs in ONE Overpass query (Overpass is the OSM
     service built for data queries; its fair-use quota is per-IP and one
     small query per city per trip is trivial volume), then
  3. match activities against that local list by name similarity.

A match confirms the place EXISTS and WHERE it is (lat/lng + OSM link). It
says nothing about opening hours, prices or reviews — nothing may claim
those from this source.

Cache: both the city geocode and the POI list are disk-cached for 7 days, so
repeat trips to the same city issue zero requests.
"""
from __future__ import annotations

import asyncio
import logging
from difflib import SequenceMatcher
from typing import Optional

import diskcache
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_OVERPASS = "https://overpass-api.de/api/interpreter"
_UA = "SafarTripPlanner/1.0 (https://safar-3uc0.onrender.com; self-hosted, low volume)"
_RADIUS_M = 30000  # covers day-trip detours (e.g. Belur from Chikmagalur)
_MAX_POIS = 400

_cache: Optional[diskcache.Cache] = None


def _get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        _cache = diskcache.Cache(f"{settings.places_cache_dir}/osm")
    return _cache


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


async def _geocode_city(city: str) -> Optional[tuple[float, float]]:
    key = "geo:" + " ".join(city.lower().split())
    try:
        hit = _get_cache().get(key)
        if hit:
            return tuple(hit)
    except Exception:  # noqa: BLE001
        pass
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                _NOMINATIM,
                params={"q": f"{city}, India", "format": "jsonv2", "limit": 1},
                headers={"User-Agent": _UA, "Accept-Language": "en"},
            )
            r.raise_for_status()
            results = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] city geocode failed for %r: %s", city, type(exc).__name__)
        return None
    if not results:
        return None
    coords = (float(results[0]["lat"]), float(results[0]["lon"]))
    try:
        _get_cache().set(key, coords, expire=settings.places_cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return coords


async def city_pois(city: str) -> list[dict]:
    """Named tourism/historic/amenity POIs within _RADIUS_M of the city centre.
    One Overpass query per city, cached 7 days."""
    cache_key = "pois:" + " ".join(city.lower().split())
    try:
        cached = _get_cache().get(cache_key)
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001
        pass

    coords = await _geocode_city(city)
    if not coords:
        return []
    lat, lng = coords
    query = (
        "[out:json][timeout:20];("
        f'node["tourism"]["name"](around:{_RADIUS_M},{lat},{lng});'
        f'way["tourism"]["name"](around:{_RADIUS_M},{lat},{lng});'
        f'node["historic"]["name"](around:{_RADIUS_M},{lat},{lng});'
        f'way["historic"]["name"](around:{_RADIUS_M},{lat},{lng});'
        f'node["amenity"~"restaurant|cafe"]["name"](around:{_RADIUS_M},{lat},{lng});'
        f'way["amenity"~"restaurant|cafe"]["name"](around:{_RADIUS_M},{lat},{lng});'
        f");out center tags {_MAX_POIS};"
    )
    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(
                _OVERPASS, data={"data": query}, headers={"User-Agent": _UA}
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] overpass failed for %r: %s", city, type(exc).__name__)
        return []

    pois = []
    for el in data.get("elements") or []:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        elat = el.get("lat") or (el.get("center") or {}).get("lat")
        elng = el.get("lon") or (el.get("center") or {}).get("lon")
        if elat is None or elng is None:
            continue
        pois.append({
            "name": name,
            "lat": float(elat),
            "lng": float(elng),
            "osm_type": el.get("type"),
            "osm_id": el.get("id"),
            "category": tags.get("tourism") or tags.get("historic") or tags.get("amenity"),
        })
    try:
        _get_cache().set(cache_key, pois, expire=settings.places_cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return pois


def match_poi(title: str, pois: list[dict]) -> Optional[dict]:
    """Best name-similarity match. A match must resemble the requested title;
    a vague or unrelated POI is not verification."""
    title_clean = title.split(",")[0].strip()
    best, best_score = None, 0.0
    for poi in pois:
        score = max(_sim(title, poi["name"]), _sim(title_clean, poi["name"]))
        if score > best_score:
            best, best_score = poi, score
    return best if best_score > 0.45 else None


async def ground_activity(title: str, city: str) -> Optional[dict]:
    """Verify `title` against the city's OSM POIs. Returns the matched POI
    (name, lat, lng, osm_type, osm_id, category) or None."""
    if not title:
        return None
    pois = await city_pois(city)
    if not pois:
        return None
    return match_poi(title, pois)
