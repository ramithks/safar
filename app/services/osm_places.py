"""
Free place grounding via OpenStreetMap Nominatim — no API key, no billing.

Used to verify that a named activity actually exists where the planner claims:
a match confirms the place name and its location (lat/lng + OSM link). It does
NOT confirm opening hours, prices or review counts — nothing may claim those
from this source.

Policy compliance (https://operations.osmfoundation.org/policies/nominatim/):
- at most 1 request/second, enforced process-wide across event loops;
- a descriptive User-Agent;
- results cached on disk (7-day TTL) so repeat trips don't re-query.

Anything else (no match, timeout, HTTP error) returns None and the caller
keeps the activity as an explicitly unverified suggestion.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

import diskcache
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_URL = "https://nominatim.openstreetmap.org/search"
_UA = "SafarTripPlanner/1.0 (https://safar-3uc0.onrender.com; contact: self-hosted)"

# Process-wide pacing: reserve the next allowed send time under a threading
# lock so concurrent event loops (Celery tasks) cannot exceed 1 req/s.
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


async def search_osm(query: str) -> Optional[dict]:
    """Search Nominatim for `query`; return the top result or None.

    Result: {name, lat, lng, display_name, osm_type, osm_id, category, type,
    importance}. Cached by normalized query for 7 days.
    """
    key = "osm:" + " ".join(query.lower().split())
    try:
        cached = _get_cache().get(key)
        if cached is not None:
            return cached or None
    except Exception:  # noqa: BLE001 - cache trouble must not break grounding
        pass

    slot = _reserve_slot()
    delay = slot - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                _URL,
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "extratags": 0,
                    "addressdetails": 0,
                },
                headers={"User-Agent": _UA, "Accept-Language": "en"},
            )
            r.raise_for_status()
            results = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] search failed for %r: %s", query, type(exc).__name__)
        return None

    hit = None
    if results:
        top = results[0]
        hit = {
            "name": top.get("name") or (top.get("display_name") or "").split(",")[0],
            "lat": float(top["lat"]),
            "lng": float(top["lon"]),
            "display_name": top.get("display_name"),
            "osm_type": top.get("osm_type"),
            "osm_id": top.get("osm_id"),
            "category": top.get("category"),
            "type": top.get("type"),
            "importance": top.get("importance"),
        }

    try:
        _get_cache().set(key, hit or {}, expire=settings.places_cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        pass
    return hit
