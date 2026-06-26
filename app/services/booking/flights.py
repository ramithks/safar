"""
Booking.com Flights API integration.
GET https://flights.booking.com/api/flights/

IATA resolution: EaseMyTrip autosuggest API.
  POST https://www.easemytrip.com/api/Flight/GetAutoSuggestNew {"Prefix": "<city>"}
  Response City field format: "New Delhi(DEL)" → extract 3-letter code.
No hardcoded values — cache populated on first lookup.
"""
import asyncio
import logging
import re as _re
import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

from app.config import settings
from app.services.scraper_proxy import with_proxy_fallback, ScraperBlocked
from app.services.geo import iata_of as geo_iata, country_of, country_of_iata

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

_BASE       = "https://flights.booking.com/api/flights/"
_EMT_AUTOSUGGEST = "https://www.easemytrip.com/api/Flight/GetAutoSuggestNew"


def _build_headers() -> dict:
    return {
        "accept": "*/*",
        "user-agent": _UA,
        "x-booking-affiliate-id": settings.booking_affiliate_id,
        "x-flights-context-name": "search_results",
        "x-flights-context-pos": "in",
        "x-requested-from": "clientFetch",
    }

# In-memory cache populated by API calls only — no hardcoded values
_IATA_CACHE: dict[str, str] = {}


def _country_matches(city: str, code: str) -> bool:
    """
    True unless the candidate airport is in a *different* known country than the
    city. Rejects cross-country garbage (e.g. 'Kasol' → KUL/Kuala Lumpur) while
    giving the benefit of the doubt when either side's country is unknown.
    """
    expected = country_of(city)
    if not expected:
        return True                      # no expectation → accept
    actual = country_of_iata(code)
    if actual is None:
        return True                      # unknown airport country → accept
    return actual == expected


async def resolve_iata(city: str) -> str | None:
    """
    Resolve city name → IATA code (the single shared flight-location resolver).

    Order: in-memory cache → static geo table (instant, offline, incl. nearest
    airport for airport-less towns) → EaseMyTrip autosuggest → Gotogate
    searchLocations. Every live candidate is country-validated against the city's
    known country so a confident wrong-country match can never win. The static
    table removes the single point of failure that once caused 'no flights'.
    Results (including misses) cached in-memory.
    """
    key = city.lower().strip()
    if key in _IATA_CACHE:
        return _IATA_CACHE[key]

    static = geo_iata(city)
    if static:
        _IATA_CACHE[key] = static
        logger.info("[flights] IATA '%s' → %s (static)", city, static)
        return static

    # ── Fallback source 1: EaseMyTrip autosuggest (validated) ────────────────
    async def _do(proxy):
        async with CurlAsyncSession(impersonate="chrome", proxy=proxy) as s:
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
                json={"Prefix": city},
                timeout=8,
            )
        return r.json()

    try:
        parsed = await with_proxy_fallback("flights_booking", _do)
        items = parsed if isinstance(parsed, list) else []
        # Prefer Indian airports, then any — but only when the country is consistent.
        for india_only in (True, False):
            for item in items:
                city_field = item.get("City") or ""   # e.g. "New Delhi(DEL)"
                country    = (item.get("Country") or "").lower()
                if india_only and country not in ("india", "in"):
                    continue
                m = _re.search(r'\(([A-Z]{3})\)', city_field)
                if not m:
                    continue
                code = m.group(1)
                if not _country_matches(city, code):
                    continue
                _IATA_CACHE[key] = code
                logger.info("[flights] IATA '%s' → %s (emt)", city, code)
                return code
    except Exception as e:
        logger.warning("[flights] IATA autocomplete '%s': %s", city, e)

    # ── Fallback source 2: Gotogate searchLocations (validated) ──────────────
    try:
        from app.services.gotogate.flights import resolve_iata as gotogate_resolve
        code = await gotogate_resolve(city)
        if code and _country_matches(city, code):
            _IATA_CACHE[key] = code
            logger.info("[flights] IATA '%s' → %s (gotogate)", city, code)
            return code
    except Exception as e:
        logger.warning("[flights] gotogate IATA fallback '%s': %s", city, e)

    _IATA_CACHE[key] = None
    return None


# Sync wrapper — returns from cache only (use async resolve_iata for fresh lookup)
def city_to_iata(city: str) -> str | None:
    return _IATA_CACHE.get(city.lower().strip())


async def search_flights(
    from_airport: str,
    to_airport: str,
    depart_date: str,
    adults: int = 1,
    cabin_class: str = "ECONOMY",
    sort: str = "CHEAPEST",
) -> list[dict]:
    """Search one-way flights. Returns normalized list sorted by sort param."""
    async def _do(proxy):
        # Tighter per-request timeout (12s) so several fresh-IP proxy retries fit
        # inside this scraper's own budget instead of hanging on one slow attempt.
        async with CurlAsyncSession(impersonate="chrome", proxy=proxy) as s:
            r = await s.get(
                _BASE,
                headers=_build_headers(),
                params={
                    "type": "ONEWAY",
                    "adults": adults,
                    "cabinClass": cabin_class,
                    "children": "",
                    "from": f"{from_airport}.AIRPORT",
                    "to": f"{to_airport}.AIRPORT",
                    "depart": depart_date,
                    "sort": sort,
                    "travelPurpose": "leisure",
                    "aid": settings.booking_affiliate_id,
                    "enableVI": "1",
                },
                timeout=12,
            )
        r.raise_for_status()
        j = r.json()
        # A genuine "no flights" response still carries the flightOffers key (empty
        # list). Its absence means a WAF/error body, not real emptiness — treat as
        # a block so the proxy retry fires and it isn't reported as "0 results".
        if "flightOffers" not in j:
            raise ScraperBlocked("flightOffers key missing")
        return j

    try:
        # 4 fresh-IP attempts for the brittle WAF, but cap the whole retry loop at
        # 25s (< the 30s FlightService merge budget) so Booking's retries can never
        # time out the shared gather and zero EMT/Gotogate too.
        raw = await asyncio.wait_for(
            with_proxy_fallback("flights_booking", _do, proxy_attempts=4),
            timeout=25.0,
        )
        offers = raw.get("flightOffers") or []
        return [_normalise_offer(o, depart_date, adults) for o in offers]
    except Exception as e:
        logger.warning("[flights_booking] %s→%s on %s: %s", from_airport, to_airport, depart_date, e)
        return []


def _normalise_offer(offer: dict, depart_date: str = "", adults: int = 1) -> dict:
    seg = (offer.get("segments") or [{}])[0]
    legs = seg.get("legs") or [{}]
    leg0 = legs[0]
    carriers = leg0.get("carriersData") or []
    flight_info = leg0.get("flightInfo") or {}
    carrier_info = flight_info.get("carrierInfo") or {}
    price_bd = offer.get("priceBreakdown") or {}

    def _amt(key: str) -> int | None:
        v = price_bd.get(key) or {}
        return v.get("units")

    total_sec = seg.get("totalTime") or 0
    stops = len(legs) - 1

    from_ap = (seg.get("departureAirport") or {}).get("code") or ""
    to_ap   = (seg.get("arrivalAirport")   or {}).get("code") or ""
    token   = offer.get("token") or ""
    booking_url = None
    if token and from_ap and to_ap and depart_date:
        booking_url = (
            f"https://flights.booking.com/flights/{from_ap}.AIRPORT-{to_ap}.AIRPORT/{token}/"
            f"?type=ONEWAY&adults={adults}&cabinClass=ECONOMY&children="
            f"&from={from_ap}.AIRPORT&to={to_ap}.AIRPORT"
            f"&depart={depart_date}&sort=BEST&travelPurpose=leisure&aid={settings.booking_affiliate_id}"
        )

    return {
        "flight_number": f"{carrier_info.get('marketingCarrier', '')}{flight_info.get('flightNumber', '')}",
        "airline": carriers[0].get("name") if carriers else None,
        "airline_code": carriers[0].get("code") if carriers else None,
        "airline_logo": carriers[0].get("logo") if carriers else None,
        "from_airport": from_ap,
        "to_airport": to_ap,
        "from_city": (seg.get("departureAirport") or {}).get("cityName"),
        "to_city": (seg.get("arrivalAirport") or {}).get("cityName"),
        "departure_time": seg.get("departureTime"),
        "arrival_time": seg.get("arrivalTime"),
        "duration_min": total_sec // 60,
        "stops": stops,
        "cabin_class": leg0.get("cabinClass", "ECONOMY"),
        "departure_terminal": leg0.get("departureTerminal"),
        "price_total": _amt("totalRounded") or _amt("total"),
        "price_base_fare": _amt("baseFare"),
        "price_tax": _amt("tax"),
        "currency": (price_bd.get("total") or {}).get("currencyCode", "INR"),
        "booking_token": token,
        "booking_url": booking_url,
    }
