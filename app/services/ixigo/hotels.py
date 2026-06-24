"""
Ixigo Hotels API integration.
POST https://www.ixigo.com/accommodation-search/api/v3/accommodations/search

Two-step flow (like Booking/EaseMyTrip):
  1. Resolve city name → {locationId, masterLocationId, cityId, countryId} via
     Ixigo's accommodation autocomplete.
  2. POST the search with those ids + dates.

STATUS: the search request below is reproduced from a verified browser curl. Two
pieces still need a verified sample to lock in:
  - the autocomplete endpoint (only a static seed is wired today — see _SEED), and
  - the exact response field names (the normaliser is defensive / best-effort).
Both are isolated so they fail closed: an unresolved city or unexpected shape
returns [] and the aggregator simply uses its other sources.
"""
import logging
import uuid
from datetime import datetime

from curl_cffi.requests import AsyncSession as CurlAsyncSession

from app.services.scraper_proxy import with_proxy_fallback, ScraperBlocked

logger = logging.getLogger(__name__)

_BASE = "https://www.ixigo.com/accommodation-search/api/v3/accommodations/search"
_AUTOCOMPLETE = "https://www.ixigo.com/hotels-autocompleter/api/v2/results"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)


def _headers() -> dict:
    # NOTE: no Origin header — the autocomplete endpoint 403s ("Invalid CORS
    # request") when Origin is present; same-origin sec-fetch headers are what the
    # browser actually sends. Search tolerates this too.
    dev = uuid.uuid4().hex[:20]
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "apikey": "ixiweb!2$",
        "clientid": "ixiweb",
        "content-type": "application/json; charset=UTF-8",
        "ixisrc": "ixiweb",
        "deviceid": dev,
        "referer": "https://www.ixigo.com/hotels/search/result",
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "user-agent": _UA,
        "uuid": dev,
        "x-search-id": str(uuid.uuid4()),
        "x-session-id": str(uuid.uuid4()),
        "x-webapp-version": "3.95.0",
    }


# Seed resolved from the verified curl (Manali). Extend via autocomplete once
# the endpoint is confirmed. key = normalized city name.
_SEED: dict[str, dict] = {
    "manali": {"locationId": "184", "masterLocationId": "9425", "cityId": "184",
               "countryId": "1", "locationName": "Manali", "locationType": "C"},
}

# city name → resolved location dict, populated by _SEED + autocomplete.
_LOC_CACHE: dict[str, dict] = dict(_SEED)


async def resolve_location(city_name: str) -> dict | None:
    """
    Resolve a city name → Ixigo location ids via the hotels-autocompleter API.
    Static seed first, then the live autocomplete (cached). Returns None if no
    match is found.
    """
    key = city_name.lower().strip()
    if key in _LOC_CACHE:
        return _LOC_CACHE[key]

    async def _do(proxy):
        async with CurlAsyncSession(impersonate="chrome", proxy=proxy) as s:
            r = await s.get(_AUTOCOMPLETE, headers=_headers(),
                            params={"query": city_name, "size": 10}, timeout=10)
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        return r.json()

    try:
        data = await with_proxy_fallback("ixigo_suggest", _do)
        loc = _pick_location(data, city_name)
        _LOC_CACHE[key] = loc          # cache misses too, to avoid re-querying
        if loc:
            logger.info("[ixigo] resolved %r → locationId %s", city_name, loc.get("locationId"))
        else:
            logger.info("[ixigo] no location match for %r", city_name)
        return loc
    except Exception as e:
        logger.warning("[ixigo] autocomplete %r: %s", city_name, e)
        return None


def _pick_location(data, query: str) -> dict | None:
    """
    Extract a location object from the autocompleter payload. Defensive across the
    response's likely shapes/keys; prefers a city-type result and a name match.
    """
    # Real shape: {"data": {"suggestions": [...]}}. Descend one level if needed.
    container = data
    if isinstance(container, dict) and isinstance(container.get("data"), dict):
        container = container["data"]
    items = container
    if isinstance(container, dict):
        for k in ("suggestions", "results", "items", "locations", "data"):
            v = container.get(k)
            if isinstance(v, list):
                items = v
                break
    if not isinstance(items, list):
        return None

    q = query.lower().strip()

    def _build(it: dict) -> dict | None:
        loc_id = it.get("locationId") or it.get("id") or it.get("locId")
        if loc_id is None:
            return None
        return {
            "locationId": str(loc_id),
            "masterLocationId": str(it.get("masterLocationId") or it.get("masterLocId") or loc_id),
            "cityId": str(it.get("cityId") or it.get("city_id") or loc_id),
            "countryId": str(it.get("countryId") or it.get("country_id") or "1"),
            "locationName": it.get("locationName") or it.get("name") or it.get("displayName") or query,
            "locationType": it.get("locationType") or it.get("type") or "C",
        }

    best: dict | None = None
    best_score = -1.0
    for it in items:
        if not isinstance(it, dict):
            continue
        built = _build(it)
        if not built:
            continue
        name = str(built["locationName"]).lower()
        score = 0.0
        if name == q:
            score += 3.0
        elif q in name or name.startswith(q):
            score += 1.5
        if str(built["locationType"]).upper() in ("C", "CITY"):   # city beats hotel/area
            score += 2.0
        if score > best_score:
            best_score = score
            best = built
    # API already ranks by relevance, so the top built item is a safe fallback
    # even when the name-match score is weak.
    return best


async def search_hotels(
    city_name: str,
    checkin: str,      # "YYYY-MM-DD"
    checkout: str,     # "YYYY-MM-DD"
    rooms: int = 1,
    adults: int = 2,
    children: int = 0,
    child_ages: list[int] | None = None,
    page: int = 1,
    limit: int = 10,
    sort: str = "SC_P",   # SC_P = price (from the verified curl)
) -> list[dict]:
    """Search hotels on Ixigo for a city + date range. Returns a normalized list."""
    loc = await resolve_location(city_name)
    if not loc:
        return []

    payload = {
        "roomCount": rooms,
        "locationId": loc["locationId"],
        "countryId": loc.get("countryId", "1"),
        "masterLocationId": loc.get("masterLocationId", loc["locationId"]),
        "locationName": loc.get("locationName", city_name),
        "adultCount": adults,
        "childAges": child_ages or [],
        "childCount": children,
        "locationType": loc.get("locationType", "C"),
        "checkinDate": f"{checkin} 00:00:00",
        "checkoutDate": f"{checkout} 00:00:00",
        "unifiedSearchHotelIds": [],
        "checkinTime": None,
        "cc": "",
        "radius": None,
        "source": "Autocomplete",
        "subsource": "Autocomplete",
        "cityId": loc.get("cityId", loc["locationId"]),
        "filterCriteria": [],
        "sortCriteria": sort,
        "pageNumber": page,
        "pageSize": limit,
        "isNearByHotelsEnabled": False,
        "paginationUsed": False,
        "variantType": "H",
        "hasUpcomingTrip": False,
        "basePrice": "",
        "tax": "",
        "hotelInfoToken": "",
        "targetHotelId": "",
    }

    async def _do(proxy):
        async with CurlAsyncSession(impersonate="chrome", proxy=proxy) as s:
            r = await s.post(_BASE, headers=_headers(), json=payload, timeout=25)
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        return r.json()

    try:
        data = await with_proxy_fallback("ixigo", _do)
        out = []
        for h in _extract_hotels(data):
            nh = _normalise(h, checkin, checkout)
            nh["booking_url"] = _booking_url(nh["hotel_id"], loc, checkin, checkout, adults, rooms, children)
            out.append(nh)
        return out
    except Exception as e:
        logger.warning("[ixigo] hotel search %r %s→%s: %s", city_name, checkin, checkout, e)
        return []


def _extract_hotels(data) -> list[dict]:
    """Hotels live at data.result.inventoryInfoList[] (with fallbacks)."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    result = inner.get("result")
    if isinstance(result, dict):
        for key in ("inventoryInfoList", "hotels", "items"):
            v = result.get(key)
            if isinstance(v, list):
                return v
    if isinstance(result, list):
        return result
    for key in ("inventoryInfoList", "hotels", "accommodations", "results", "items"):
        v = inner.get(key)
        if isinstance(v, list):
            return v
    return []


def _booking_url(hotel_id, loc: dict, checkin: str, checkout: str,
                 adults: int, rooms: int, children: int) -> str | None:
    """Build an Ixigo SRP deep-link targeting this hotel (no URL in the payload)."""
    if not hotel_id:
        return None
    from urllib.parse import urlencode

    def _ddmmyyyy(d: str) -> str:
        try:
            return datetime.strptime(d, "%Y-%m-%d").strftime("%d%m%Y")
        except Exception:
            return d

    params = {
        "locationId": loc["locationId"], "locationName": loc.get("locationName", ""),
        "locationType": loc.get("locationType", "C"),
        "masterLocationId": loc.get("masterLocationId", loc["locationId"]),
        "countryId": loc.get("countryId", "1"),
        "checkinDate": _ddmmyyyy(checkin), "checkoutDate": _ddmmyyyy(checkout),
        "adultCount": adults, "roomCount": rooms, "childCount": children,
        "cityId": loc.get("cityId", loc["locationId"]),
        "source": "Autocomplete", "targetHotelId": hotel_id,
    }
    return "https://www.ixigo.com/hotels/search/result?" + urlencode(params)


def _normalise(h: dict, checkin: str, checkout: str) -> dict:
    """Normalise one Ixigo inventoryInfoList entry (shape verified live 2026-06-24)."""
    try:
        nights = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days or 1
    except Exception:
        nights = 1

    fare = h.get("fareDetail") or {}
    ppn = _to_int(fare.get("displayedBaseFare"))
    total_base = _to_int(fare.get("totalBaseFare"))
    total_tax = _to_int(fare.get("totalTaxesAndFees"))
    if total_base is not None and total_tax is not None:
        total_price = total_base + total_tax
    else:
        total_price = ppn * nights if ppn else None

    geo = h.get("geoLocationInfo") or {}
    photos = [img.get("urlHigh") or img.get("url")
              for img in (h.get("imageInfoList") or []) if isinstance(img, dict)]
    photos = [p for p in photos if p][:6]
    amenities = [a.get("name") for a in (h.get("amenities") or [])
                 if isinstance(a, dict) and a.get("name")][:10]

    return {
        "hotel_id": str(h.get("hotelId")) if h.get("hotelId") is not None else None,
        "name": h.get("name"),
        "address": h.get("locality") or h.get("city"),
        "city": h.get("city"),
        "lat": _to_float(geo.get("lat")),
        "lon": _to_float(geo.get("lon")),
        "star_rating": _to_float(h.get("starRating")),
        "review_score": _to_float(h.get("userRating")),     # /10 scale
        "review_count": _to_int(h.get("userRatingCount")),
        "review_label": h.get("userRatingCategory"),
        "price_per_night": ppn,
        "total_price": total_price,
        "currency": "INR",
        "nights": nights,
        "amenities": amenities,
        "couple_friendly": bool(h.get("coupleFriendly")),
        "photos": photos,
        "booking_url": None,   # set by caller via _booking_url
        "source": "ixigo",
    }


def _to_int(v) -> int | None:
    try:
        return int(float(str(v).replace(",", "").replace("₹", "").strip())) if v not in (None, "") else None
    except Exception:
        return None


def _to_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None
