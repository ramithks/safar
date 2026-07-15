"""
Location autocomplete — one fast resolver shared by flight and stay search.

Two surfaces, both backed by the same offline geo tables the search pipeline
already trusts (app.services.geo):

  • suggest_airports(q)      → cities that have a servicing airport, with IATA.
  • suggest_destinations(q)  → any city/town a stay can be searched in.

Offline-first by design: the geo tables (~780 cities) answer instantly with no
network call and no anti-bot exposure, which is exactly what an as-you-type box
needs. The live EaseMyTrip autosuggest is a long-tail FALLBACK, fired only when
the offline pass is sparse, and always best-effort (never raises).
"""
import asyncio
import logging
import re as _re

from app.services.curl_pool import get_curl_session

from app.services import geo
from app.services.scraper_proxy import with_proxy_fallback

logger = logging.getLogger(__name__)

# Below this many offline hits we reach for the live airport autosuggest.
_LIVE_THRESHOLD = 3

_EMT_AUTOSUGGEST = "https://www.easemytrip.com/api/Flight/GetAutoSuggestNew"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


async def suggest_airports(q: str, limit: int = 8) -> list[dict]:
    """
    Airport/city suggestions for flight search. Every row carries an IATA code so
    the picked value can be passed straight to /search/flights as from_iata/to_iata
    (skipping re-resolution). Offline table first; live EMT fills the long tail.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []

    offline = geo.search_locations(q, limit=limit, require_iata=True)
    if len(offline) >= _LIVE_THRESHOLD or len(offline) >= limit:
        return offline[:limit]

    # Sparse offline result → enrich from EaseMyTrip autosuggest (best-effort).
    live = await _live_airports(q)
    return _merge(offline, live, limit)


async def suggest_destinations(q: str, limit: int = 8) -> list[dict]:
    """
    Destination suggestions for stay search. Cities need not have an airport
    (hill towns, beach towns). Returns the canonical city string to pass to
    /search/hotels as `destination`. Pure offline — instant and reliable.
    """
    q = (q or "").strip()
    if len(q) < 2:
        return []
    return geo.search_locations(q, limit=limit, require_iata=False)


# ── Live fallback (EaseMyTrip airport autosuggest) ───────────────────────────

async def _live_airports(q: str) -> list[dict]:
    """
    Hit EMT autosuggest and normalise rows to the same shape geo.search_locations
    returns. City field looks like "New Delhi(DEL)". Never raises.
    """
    async def _do(proxy):
        async with get_curl_session(impersonate="chrome", proxy=proxy) as s:
            r = await s.post(
                _EMT_AUTOSUGGEST,
                headers={
                    "accept": "application/json, text/javascript, */*; q=0.01",
                    "content-type": "application/json; charset=UTF-8",
                    "origin": "https://www.easemytrip.com",
                    "referer": "https://www.easemytrip.com/",
                    "user-agent": _UA,
                    "x-requested-with": "XMLHttpRequest",
                },
                json={"Prefix": q},
                timeout=6,
            )
        return r.json()

    try:
        parsed = await asyncio.wait_for(
            with_proxy_fallback("flights_booking", _do), timeout=8.0
        )
    except Exception as e:
        logger.info("[autocomplete] live airport fallback %r: %s", q, e)
        return []

    nq = geo.normalize(q)
    items = parsed if isinstance(parsed, list) else []
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        city_field = it.get("City") or ""          # e.g. "New Delhi(DEL)"
        m = _re.search(r"\(([A-Z]{3})\)", city_field)
        if not m:
            continue
        code = m.group(1)
        if code in seen:
            continue
        name = _re.sub(r"\s*\([A-Z]{3}\)\s*", "", city_field).strip()
        # EMT autosuggest fuzzes hard (returns "Indore" for "jai"). Keep only rows
        # whose name actually contains the query so the tail stays relevant.
        if nq not in geo.normalize(name) and nq not in code.lower():
            continue
        seen.add(code)
        cc = geo.country_of_iata(code)
        out.append({
            "city": name or code,
            "country_code": cc,
            "country": geo.country_name(cc) or (it.get("Country") or None),
            "iata": code,
        })
    return out


def _merge(offline: list[dict], live: list[dict], limit: int) -> list[dict]:
    """Offline rows first (trusted), then live rows whose IATA isn't already shown."""
    seen = {r["iata"] for r in offline if r.get("iata")}
    merged = list(offline)
    for r in live:
        if r.get("iata") and r["iata"] not in seen:
            seen.add(r["iata"])
            merged.append(r)
        if len(merged) >= limit:
            break
    return merged[:limit]
