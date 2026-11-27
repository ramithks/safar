"""
Free place grounding via Photon (Komoot's OpenStreetMap geocoder) — no API
key, no billing.

Why Photon: Nominatim's usage policy sits badly with machine-generated batch
geocoding, and Overpass (built for data queries) proved too unreliable from
production (repeated timeouts from multiple mirrors). Photon's public API is
built for per-name place lookups, so verifying activity names one by one is
its intended use. Volume stays low: lookups happen once per generated trip,
are disk-cached for 7 days, and are paced at ~1 request/second process-wide.

A match confirms the place EXISTS and WHERE it is (lat/lng + OSM link). It
says nothing about opening hours, prices or reviews — nothing may claim
those from this source.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from difflib import SequenceMatcher
from typing import Optional

import diskcache
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_URL = "https://photon.komoot.io/api/"
_UA = "SafarTripPlanner/1.0 (https://safar-3uc0.onrender.com; self-hosted, low volume)"

# Politeness pacing, process-wide across event loops.
_pace_lock = threading.Lock()
_next_slot = 0.0
_MIN_GAP = 1.1  # seconds

_cache: Optional[diskcache.Cache] = None


def _get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        _cache = diskcache.Cache(f"{settings.places_cache_dir}/osm")
    return _cache


def _reserve_slot() -> float:
    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + _MIN_GAP
        return slot


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


async def _photon(query: str) -> Optional[dict]:
    key = "ph:" + " ".join(query.lower().split())
    try:
        cached = _get_cache().get(key)
        if cached is not None:
            return cached or None
    except Exception:  # noqa: BLE001
        pass

    slot = _reserve_slot()
    delay = slot - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                _URL,
                params={"q": query, "limit": 3, "lang": "en"},
                headers={"User-Agent": _UA},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] photon failed for %r: %s", query, type(exc).__name__)
        return None

    hit = None
    for f in data.get("features") or []:
        props = f.get("properties") or {}
        name = props.get("name")
        lon, lat = (f.get("geometry") or {}).get("coordinates") or [None, None]
        if not name or lat is None:
            continue
        hit = {
            "name": name,
            "lat": float(lat),
            "lng": float(lon),
            "display_name": ", ".join(str(p) for p in [name, props.get("city") or props.get("district"), props.get("state")] if p),
            "osm_type": (props.get("osm_type") or "").lower() or None,
            "osm_id": props.get("osm_id"),
            "category": props.get("osm_value"),
        }
        break

    try:
        _get_cache().set(key, hit or {}, expire=settings.places_cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return hit


def _matches(title: str, hit: dict) -> bool:
    """The returned place name must resemble the requested title — a vague or
    unrelated hit is not verification. A much shorter generic name (a town,
    a region) also cannot verify a longer specific venue title."""
    name = (hit.get("name") or "").lower().strip()
    if not name:
        return False
    for cand in {title.lower().strip(), title.split(",")[0].strip().lower()}:
        if _sim(cand, name) > 0.45 and min(len(cand), len(name)) / max(len(cand), len(name), 1) >= 0.6:
            return True
    return False


async def ground_activity(title: str, city: str) -> Optional[dict]:
    """Verify `title` is a real place near `city`. Returns the matched place
    (name, lat, lng, osm_type, osm_id, category, display_name) or None."""
    if not title:
        return None
    for query in (f"{title}, {city}", title):
        hit = await _photon(query)
        if hit and _matches(title, hit):
            return hit
    return None
