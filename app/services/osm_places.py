"""
Free place grounding via OpenStreetMap — no API key, no billing.

Per destination city (one user action each, both results cached 7 days):
  1. geocode the CITY once via Nominatim (a single on-demand lookup - the
     use the public endpoint exists for; systematic per-activity geocoding
     is what its policy forbids);
  2. fetch the region's named tourism POIs in ONE Overpass query (the OSM
     service built for data queries; one small query per city per trip is
     trivial volume under its fair-use quota);
  3. match activity titles against that local list - used both to verify
     planned activities and to backfill sparse days with REAL nearby places.
Photon (Komoot) is the per-activity fallback when Overpass is unreachable:
its public API is built for per-name lookups ("please be fair - extensive
usage will be throttled"); calls are paced ~1/s and a 403/429 stops
grounding with no retry.

A match confirms the place EXISTS and WHERE it is (lat/lng + OSM link). It
says nothing about opening hours, prices, reviews or narrative details -
nothing may claim those from this source.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

import diskcache
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_OVERPASS = "https://overpass-api.de/api/interpreter"
_PHOTON = "https://photon.komoot.io/api/"
_UA = "SafarTripPlanner/1.0 (https://safar-3uc0.onrender.com; self-hosted, low volume)"
_BBOX_DEG = 0.35  # ~35 km around the city centre - covers day-trip detours

_pace_lock = threading.Lock()
_next_slot = 0.0
_MIN_GAP = 1.1  # seconds (Photon politeness; Overpass is one query/city)

_cache: Optional[diskcache.Cache] = None


def _get_cache() -> diskcache.Cache:
    global _cache
    if _cache is None:
        _cache = diskcache.Cache(f"{settings.places_cache_dir}/osm")
    return _cache


def _cache_get(key: str):
    try:
        return _get_cache().get(key)
    except Exception:  # noqa: BLE001
        return None


def _cache_set(key: str, value, ttl: Optional[int] = None) -> None:
    try:
        _get_cache().set(key, value, expire=ttl or settings.places_cache_ttl_seconds)
    except Exception:  # noqa: BLE001
        pass


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


async def _geocode_city(city: str) -> Optional[tuple[float, float]]:
    key = "geo:" + " ".join(city.lower().split())
    hit = _cache_get(key)
    if hit:
        return tuple(hit)
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
    if not isinstance(results, list) or not results:
        return None
    try:
        coords = (float(results[0]["lat"]), float(results[0]["lon"]))
    except (KeyError, TypeError, ValueError):
        return None
    _cache_set(key, coords)
    return coords


async def city_pois(city: str) -> list[dict]:
    """Named tourism POIs within ~35 km of the city centre. One Overpass
    query per city, cached 7 days; empty on failure (callers fall back)."""
    cache_key = "pois:" + " ".join(city.lower().split())
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    coords = await _geocode_city(city)
    if not coords:
        return []
    lat, lng = coords
    bbox = f"{lat - _BBOX_DEG},{lng - _BBOX_DEG},{lat + _BBOX_DEG},{lng + _BBOX_DEG}"
    query = (
        "[out:json][timeout:35]"
        f"[bbox:{bbox}];"
        'nwr["tourism"~"^(attraction|viewpoint|museum|zoo|theme_park|gallery|artwork)$"]["name"];'
        "out center tags 120;"
    )
    data = None
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(_OVERPASS, data={"data": query},
                                  headers={"User-Agent": _UA})
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] overpass failed for %r: %s", city, type(exc).__name__)
    if data is None:
        pois = await _wikidata_pois(lat, lng)
        if pois:
            logger.info("[osm] %r POI list from wikidata fallback (%d)", city, len(pois))
        _cache_set(cache_key, pois, ttl=600 if not pois else None)
        return pois

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
            "category": tags.get("tourism"),
            "source": "openstreetmap",
        })
    if not pois:  # Overpass unreachable or mapped nothing: try Wikidata
        pois = await _wikidata_pois(lat, lng)
        if pois:
            logger.info("[osm] %r POI list from wikidata fallback (%d)", city, len(pois))
    _cache_set(cache_key, pois, ttl=600 if not pois else None)
    return pois


_WIKIDATA_TYPES = {
    "Q570116": "attraction",   # tourist attraction
    "Q8502": "peak",           # mountain
    "Q34038": "waterfall",
    "Q44539": "temple",        # Hindu temple
    "Q33506": "museum",
    "Q839954": "viewpoint",
}


async def _wikidata_pois(lat: float, lng: float) -> list[dict]:
    """Fallback POI list when Overpass is unreachable: real named places
    near the city from Wikidata (Wikimedia API, one cached query per city).
    Only items with an English label are kept; admin/junk entries excluded."""
    types = " ".join(f"wd:{q}" for q in _WIKIDATA_TYPES)
    query = (
        "SELECT ?item ?itemLabel ?coord ?type WHERE { "
        "?item wdt:P31/wdt:P279* ?type . "
        f"VALUES ?type {{ {types} }} . "
        "SERVICE wikibase:around { ?item wdt:P625 ?coord . "
        f'bd:serviceParam wikibase:center "Point({lng} {lat})"^^geo:wktLiteral . '
        'bd:serviceParam wikibase:radius "40" . } '
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . } } LIMIT 60'
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                "https://query.wikidata.org/sparql",
                params={"query": query, "format": "json"},
                headers={"User-Agent": _UA, "Accept": "application/sparql-results+json"},
            )
            r.raise_for_status()
            rows = (r.json().get("results") or {}).get("bindings") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] wikidata pois failed: %s", type(exc).__name__)
        return []
    seen: set[str] = set()
    pois = []
    for row in rows:
        name = (row.get("itemLabel") or {}).get("value") or ""
        item = (row.get("item") or {}).get("value") or ""
        if not name or name.startswith("Q") or item in seen:
            continue
        seen.add(item)
        coord = (row.get("coord") or {}).get("value") or ""
        m = re.match(r"Point\(([-0-9.]+) ([-0-9.]+)\)", coord)
        if not m:
            continue
        qtype = ((row.get("type") or {}).get("value") or "").rsplit("/", 1)[-1]
        pois.append({
            "name": name,
            "lat": float(m.group(2)),
            "lng": float(m.group(1)),
            "osm_type": None,
            "osm_id": None,
            "category": _WIKIDATA_TYPES.get(qtype, "attraction"),
            "source": "wikidata",
            "wikidata_id": item.rsplit("/", 1)[-1],
        })
    return pois


def _normtext(v: str) -> str:
    v = unicodedata.normalize("NFKD", (v or "").lower())
    v = "".join(c for c in v if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", v).strip()


def _matches(title: str, name: str) -> bool:
    """Verify a name match two ways only: near-identical spelling
    (Chennakesava/Chennakeshava), or every token of the shorter string
    contained in the longer with a length floor — so a town or a
    different-venue lookalike ('Madhur Caf\xe9' vs the Madhugiri peak)
    cannot verify the place."""
    name = _normtext(name)
    if not name:
        return False
    for cand in {_normtext(title), _normtext(title.split(",")[0])}:
        if not cand:
            continue
        if _sim(cand, name) >= 0.8:
            return True
        short, long = (cand, name) if len(cand) <= len(name) else (name, cand)
        if len(short) / max(len(long), 1) < 0.5:
            continue
        stoks, ltoks = set(short.split()), set(long.split())
        if stoks and stoks <= ltoks:
            return True
    return False


def match_poi(title: str, pois: list[dict]) -> Optional[dict]:
    best, best_score = None, 0.0
    for poi in pois:
        for cand in {title, title.split(",")[0].strip()}:
            score = _sim(cand, poi["name"])
            if score > best_score:
                best, best_score = poi, score
    if best and _matches(title, best["name"]):
        return best
    return None


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from math import radians, sin, cos, asin, sqrt
    p = 6371.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * p * asin(sqrt(a))


async def _photon(title: str, city: str) -> Optional[dict]:
    """Per-activity fallback when Overpass is unreachable. Paced; throttling
    stops grounding with no retry (Photon terms). A name match far from the
    city (a same-named place in another state) is NOT verification."""
    coords = await _geocode_city(city)
    key = "ph:" + " ".join(f"{title}|{city}".lower().split())
    cached = _cache_get(key)
    if cached is not None:
        return cached or None

    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + _MIN_GAP
    delay = slot - time.monotonic()
    if delay > 0:
        await asyncio.sleep(delay)

    hit = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            for q in (f"{title}, {city}", title):
                r = await client.get(_PHOTON, params={"q": q, "limit": 3, "lang": "en"},
                                     headers={"User-Agent": _UA})
                if r.status_code in (403, 429):
                    logger.warning("[osm] photon throttled (%s); grounding skipped", r.status_code)
                    return None
                r.raise_for_status()
                for f in (r.json().get("features") or []):
                    props = f.get("properties") or {}
                    name = props.get("name")
                    lon, lat = (f.get("geometry") or {}).get("coordinates") or [None, None]
                    if name and lat is not None and _matches(title, name):
                        if coords and _km(coords[0], coords[1], float(lat), float(lon)) > 150:
                            continue  # same-named place in another region
                        hit = {
                            "name": name,
                            "lat": float(lat),
                            "lng": float(lon),
                            "osm_type": (props.get("osm_type") or "").lower() or None,
                            "osm_id": props.get("osm_id"),
                            "category": props.get("osm_value"),
                        }
                        break
                if hit:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("[osm] photon failed for %r: %s", title, type(exc).__name__)
        return None

    _cache_set(key, hit or {})
    return hit


async def ground_activity(title: str, city: str) -> Optional[dict]:
    """Verify `title` is a real place near `city`: the city's OSM POI list
    first, Photon per-name lookup as fallback. Returns the matched place
    (name, lat, lng, osm_type, osm_id, category) or None."""
    if not title:
        return None
    pois = await city_pois(city)
    if pois:
        return match_poi(title, pois)
    return await _photon(title, city)


def backfill_pois(pois: list[dict], existing_titles: list[str], limit: int = 3) -> list[dict]:
    """Real POIs not already planned, for sparse-day backfill. Never invents
    a name: every returned entry is an OSM feature."""
    out = []
    for poi in pois:
        if any(_matches(t, poi["name"]) or _matches(poi["name"], t) for t in existing_titles):
            continue
        out.append(poi)
        if len(out) >= limit:
            break
    return out
