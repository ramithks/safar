"""
Booking.com Flights API integration.
GET https://flights.booking.com/api/flights/

IATA resolution: EaseMyTrip autosuggest API.
  POST https://www.easemytrip.com/api/Flight/GetAutoSuggestNew {"Prefix": "<city>"}
  Response City field format: "New Delhi(DEL)" → extract 3-letter code.
No hardcoded values — cache populated on first lookup.
"""
import logging
import re as _re
import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession

from app.config import settings
from app.services.scraper_proxy import with_proxy_fallback

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


async def resolve_iata(city: str) -> str | None:
    """
    Resolve city name → IATA code via EaseMyTrip autosuggest API.
    Response City field format: "New Delhi(DEL)" — IATA extracted from parentheses.
    Results cached in-memory after first call.
    """
    key = city.lower().strip()
    if key in _IATA_CACHE:
        return _IATA_CACHE[key]

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
        for item in items:
            city_field = item.get("City") or ""   # e.g. "New Delhi(DEL)"
            country    = (item.get("Country") or "").lower()
            m = _re.search(r'\(([A-Z]{3})\)', city_field)
            if m:
                code = m.group(1)
                # Prefer Indian airports; fall back to any
                if country in ("india", "in"):
                    _IATA_CACHE[key] = code
                    logger.info("[flights] IATA '%s' → %s", city, code)
                    return code
        # Second pass: accept any country
        for item in items:
            city_field = item.get("City") or ""
            m = _re.search(r'\(([A-Z]{3})\)', city_field)
            if m:
                code = m.group(1)
                _IATA_CACHE[key] = code
                logger.info("[flights] IATA '%s' → %s", city, code)
                return code
    except Exception as e:
        logger.warning("[flights] IATA autocomplete '%s': %s", city, e)

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
                timeout=20,
            )
        r.raise_for_status()
        return r.json()

    try:
        offers = (await with_proxy_fallback("flights_booking", _do)).get("flightOffers") or []
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
