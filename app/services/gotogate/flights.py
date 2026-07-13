"""
Gotogate Flights integration (in.gotogate.com — Etraveli/ETG GraphQL gateway).

Two-step flow (like Booking/EaseMyTrip):
  1. Resolve a city name → IATA code via the `searchLocations` GraphQL operation.
  2. Search flights via the `SearchOnResultPage` GraphQL operation (routes use
     IATA codes directly).

The GraphQL gateway validates the `x-etg-graphql-operation` header against the
query's operation name, so the full verified query/fragment bodies are embedded
verbatim below — trimming them risks an allowlist rejection. The search itself
is reproduced from a verified browser curl; the `ibe-salesprocess` header is a
per-search session id and a fresh UUID is accepted on the result page request.

Fails closed everywhere: an unresolved city, a block, or an unexpected response
shape returns [] so the aggregator simply falls back to its other sources.
"""
import asyncio
import logging
import re
import uuid
from datetime import datetime

from app.services.curl_pool import get_curl_session

from app.services.scraper_proxy import with_proxy_fallback, ScraperBlocked

logger = logging.getLogger(__name__)

_ORIGIN = "https://in.gotogate.com"
_SEARCH_LOCATIONS_URL = f"{_ORIGIN}/graphql/searchLocations"
_SEARCH_URL = f"{_ORIGIN}/graphql/SearchOnResultPage"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Gotogate CabinClass enum values.
_CABIN = {
    "economy": "ECONOMY",
    "premium_economy": "PREMIUM_ECONOMY",
    "premiumeconomy": "PREMIUM_ECONOMY",
    "business": "BUSINESS",
    "first": "FIRST",
}

# Native server-side sort — canonical sort → Gotogate sortTypeCode (verified live
# via availableSortTypes). Driving sort upstream decides which flights land in the
# returned slice, so a later local rerank can't miss them.
_SORT_CODES = {
    "CHEAPEST": "CHEAP_TRIP",
    "FASTEST":  "SHORT_TRIP",
    "BEST":     "RECOMMENDATION",
}

_SEARCH_LOCATIONS_QUERY = (
    "query searchLocations($searchTerm: String) {\n"
    "  searchLocations(searchTerm: $searchTerm) {\n"
    "    city\n    cityCode\n    continent\n    continentCode\n    country\n"
    "    countryCode\n    iataCode\n    id\n    multipleAirports\n    name\n"
    "    state\n    type\n    __typename\n  }\n}"
)

# Full SearchOnResultPage query/fragment body — verbatim from the verified curl.
# The gateway validates it against the operation name; do not trim.
_SEARCH_QUERY = "query SearchOnResultPage($routes: [Route!]!, $cabinClass: CabinClass, $direct: Boolean, $carrierCodes: [String], $adults: Int!, $childAges: [Int], $offset: Int, $sortTypeCode: String, $filters: [Filter!], $validWithVoucher: Boolean) {\n  search(\n    routes: $routes\n    cabinClass: $cabinClass\n    direct: $direct\n    carrierCodes: $carrierCodes\n    adults: $adults\n    childAges: $childAges\n    offset: $offset\n    sortTypeCode: $sortTypeCode\n    filters: $filters\n    validWithVoucher: $validWithVoucher\n  ) {\n    availableFilters {\n      code\n      group\n      label\n      ... on RangeFilter {\n        __typename\n        code\n        group\n        label\n        max\n        min\n        type\n      }\n      ... on SelectionFilter {\n        __typename\n        code\n        group\n        label\n        isMultiChoice\n        options {\n          label\n          value\n          __typename\n        }\n        type\n      }\n      __typename\n    }\n    availableSortTypes {\n      code\n      name\n      __typename\n    }\n    carrierCodes\n    carrierNames\n    flights {\n      ...Flights\n      __typename\n    }\n    flightsCount\n    filteredFlightsCount\n    resultSetMetaData {\n      priceRange {\n        min\n        max\n        __typename\n      }\n      travelTimeRange {\n        min\n        max\n        __typename\n      }\n      marketingCarriers {\n        code\n        name\n        logo {\n          small\n          large\n          darkModeSmall\n          darkModeLarge\n          __typename\n        }\n        __typename\n      }\n      __typename\n    }\n    routes {\n      origin {\n        code\n        name\n        cityCode\n        cityName\n        countryCode\n        countryName\n        continentCode\n        continentName\n        __typename\n      }\n      destination {\n        code\n        name\n        cityCode\n        cityName\n        countryCode\n        countryName\n        continentCode\n        continentName\n        __typename\n      }\n      departureAt\n      departureDate\n      departureTimeOfDay\n      __typename\n    }\n    searchPath\n    type\n    validWithVoucher\n    __typename\n  }\n}\n\nfragment IncludedCabinBaggage on IncludedCabinBaggage {\n  pieces\n  weight\n  weightUnit\n  __typename\n}\n\nfragment IncludedCheckedBaggage on IncludedCheckedBaggage {\n  pieces\n  weight\n  weightUnit\n  __typename\n}\n\nfragment TravelerPrices on TravelerPrice {\n  id\n  price {\n    markup {\n      value\n      __typename\n    }\n    price {\n      value\n      currency {\n        code\n        exponent\n        __typename\n      }\n      __typename\n    }\n    vat {\n      value\n      __typename\n    }\n    __typename\n  }\n  taxesAndFees {\n    category\n    taxes {\n      code\n      title\n      amount {\n        value\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n  travelerId\n  __typename\n}\n\nfragment Flights on Trip {\n  id\n  tripId\n  isVI\n  includedCabinBaggage {\n    includedPersonalItem\n    ...IncludedCabinBaggage\n    __typename\n  }\n  includedCheckedBaggage {\n    ...IncludedCheckedBaggage\n    __typename\n  }\n  bounds {\n    boundId: id\n    includedCheckedBaggage {\n      ...IncludedCheckedBaggage\n      __typename\n    }\n    segments {\n      ... on TripSegment {\n        __typename\n        segmentId: id\n        aircraftType\n        arrivedAt\n        cabinClassName\n        departuredAt\n        destination {\n          code\n          name\n          cityCode\n          cityName\n          airportCode\n          airportName\n          __typename\n        }\n        duration\n        flightNumber\n        marketingCarrier {\n          code\n          name\n          logo {\n            small\n            large\n            __typename\n          }\n          __typename\n        }\n        operatingCarrier {\n          code\n          name\n          __typename\n        }\n        numberOfTechnicalStops\n        origin {\n          code\n          name\n          cityCode\n          cityName\n          airportCode\n          airportName\n          __typename\n        }\n        segmentDetails {\n          paxType\n          numberOfSeatsLeft\n          __typename\n        }\n      }\n      ... on EventSegment {\n        __typename\n        segmentId: id\n        types\n        duration\n      }\n      __typename\n    }\n    __typename\n  }\n  selectionKey\n  type\n  tripCharacteristics\n  travelerPrices {\n    ...TravelerPrices\n    __typename\n  }\n  tripTags\n  systems\n  shareableUrl\n  preferredDisplayCarrier\n  __typename\n}"


def _loc_headers() -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": _ORIGIN,
        "referer": f"{_ORIGIN}/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": _UA,
        "x-etg-graphql-operation": "searchLocations",
    }


def _search_headers() -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "ibe-salesprocess": str(uuid.uuid4()),
        "origin": _ORIGIN,
        "referer": f"{_ORIGIN}/rf/result",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": _UA,
        "x-etg-graphql-operation": "SearchOnResultPage",
    }


# city name → IATA code, populated by searchLocations.
_IATA_CACHE: dict[str, str | None] = {}


async def resolve_iata(city: str) -> str | None:
    """Resolve a city name → IATA code via the searchLocations operation (cached)."""
    key = city.lower().strip()
    if key in _IATA_CACHE:
        return _IATA_CACHE[key]

    async def _do(proxy):
        async with get_curl_session(impersonate="chrome", proxy=proxy) as s:
            r = await s.post(
                _SEARCH_LOCATIONS_URL,
                headers=_loc_headers(),
                json={"query": _SEARCH_LOCATIONS_QUERY, "variables": {"searchTerm": city}},
                timeout=10,
            )
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        return r.json()

    try:
        data = await with_proxy_fallback("gotogate_locations", _do)
        code = _pick_iata(data, city)
        _IATA_CACHE[key] = code
        if code:
            logger.info("[gotogate] IATA '%s' → %s", city, code)
        else:
            logger.info("[gotogate] no location match for '%s'", city)
        return code
    except Exception as e:
        logger.warning("[gotogate] searchLocations '%s': %s", city, e)
        return None


def _pick_iata(data, query: str) -> str | None:
    """Pick the best IATA code from a searchLocations payload (prefers a city match)."""
    items = ((data or {}).get("data") or {}).get("searchLocations") or []
    if not isinstance(items, list):
        return None

    q = query.lower().strip()
    best: str | None = None
    best_score = -1.0
    for it in items:
        if not isinstance(it, dict):
            continue
        code = it.get("iataCode") or it.get("cityCode")
        if not code:
            continue
        name = str(it.get("name") or it.get("city") or "").lower()
        score = 0.0
        if name == q:
            score += 3.0
        elif q and (q in name or name.startswith(q)):
            score += 1.5
        # A multi-airport city entry maps to all its airports — best for routing.
        if str(it.get("type") or "").upper() in ("CITY", "AIRPORT"):
            score += 1.0
        if it.get("multipleAirports"):
            score += 0.5
        if str(it.get("countryCode") or "").upper() == "IN":
            score += 0.5
        if score > best_score:
            best_score = score
            best = str(code).upper()
    return best


async def search_flights(
    from_airport: str,
    to_airport: str,
    depart_date: str,   # "YYYY-MM-DD"
    adults: int = 1,
    cabin_class: str = "ECONOMY",
    child_ages: list[int] | None = None,
    nonstop_only: bool = False,
    sort: str | None = None,
) -> list[dict]:
    """
    Search one-way flights on Gotogate. `sort` is a canonical flight sort
    (CHEAPEST|FASTEST|BEST); it is pushed to the server via `sortTypeCode` so the
    returned slice already favours the requested ranking.
    """
    cabin = _CABIN.get(cabin_class.lower(), "ECONOMY")
    variables = {
        "adults": adults,
        "cabinClass": cabin,
        "childAges": child_ages or [],
        "filters": [],
        "routes": [
            {
                "origin": from_airport.upper(),
                "destination": to_airport.upper(),
                "departureDate": depart_date,
            }
        ],
    }
    if nonstop_only:
        variables["direct"] = True
    sort_code = _SORT_CODES.get((sort or "").upper())
    if sort_code:
        variables["sortTypeCode"] = sort_code

    async def _do(proxy):
        async with get_curl_session(impersonate="chrome", proxy=proxy) as s:
            r = await s.post(
                _SEARCH_URL,
                headers=_search_headers(),
                json={"query": _SEARCH_QUERY, "variables": variables},
                timeout=30,
            )
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        j = r.json()
        # A real "no flights" response still carries data.search. Its absence
        # (GraphQL errors / WAF body) is a block, not genuine emptiness.
        if not (isinstance(j, dict) and (j.get("data") or {}).get("search")):
            raise ScraperBlocked("search node missing")
        return j

    try:
        # Cap the retry loop at 25s (< FlightService's 30s merge budget) so this
        # provider's proxy retries can never time out the shared gather.
        data = await asyncio.wait_for(with_proxy_fallback("gotogate", _do), timeout=25.0)
        trips = ((data.get("data") or {}).get("search") or {}).get("flights") or []
        flights = [_normalise(t, from_airport, to_airport, adults) for t in trips]
        flights = [f for f in flights if f]
        if nonstop_only:
            flights = [f for f in flights if (f.get("stops") or 0) == 0]
        # Keep the server order for FASTEST/BEST (it already ranked the slice);
        # only price-sort for the cheapest case (and as the default).
        if (sort or "CHEAPEST").upper() == "CHEAPEST":
            flights.sort(key=lambda f: (f.get("price_total") is None, f.get("price_total") or 999_999))
        return flights
    except Exception as e:
        logger.warning("[gotogate] %s→%s on %s: %s", from_airport, to_airport, depart_date, e)
        return []


def _normalise(trip: dict, from_iata: str, to_iata: str, adults: int) -> dict | None:
    """Normalise one Gotogate Trip into the shared flight shape."""
    if not isinstance(trip, dict):
        return None
    bounds = trip.get("bounds") or []
    if not bounds:
        return None
    segments = [s for s in (bounds[0].get("segments") or []) if s.get("departuredAt")]
    if not segments:
        return None

    first, last = segments[0], segments[-1]
    origin = first.get("origin") or {}
    dest = last.get("destination") or {}
    carrier = first.get("marketingCarrier") or {}
    logo = carrier.get("logo") or {}

    # Connection detail: change airports + total wait between segments.
    layover_airports: list[str] = []
    layover_minutes = 0
    for i in range(len(segments) - 1):
        stop = (segments[i].get("destination") or {})
        code = stop.get("airportCode") or stop.get("code")
        if code:
            layover_airports.append(code)
        gap = _duration(segments[i].get("arrivedAt"), segments[i + 1].get("departuredAt"))
        if gap:
            layover_minutes += gap

    dep_at = first.get("departuredAt")
    arr_at = last.get("arrivedAt")
    duration_min = _duration(dep_at, arr_at)

    # Per-traveler prices; total fare = sum across the listed travelers. Values
    # are in minor units (e.g. paise) — scaled down by the currency exponent.
    currency = "INR"
    exponent = 0
    total = 0.0
    has_price = False
    for tp in (trip.get("travelerPrices") or []):
        price_obj = ((tp.get("price") or {}).get("price") or {})
        val = price_obj.get("value")
        if val is not None:
            total += _to_float(val) or 0.0
            has_price = True
        cur = price_obj.get("currency") or {}
        if cur.get("code"):
            currency = cur["code"]
        if cur.get("exponent") is not None:
            exponent = int(cur["exponent"])
    price_total = int(round(total / (10 ** exponent))) if has_price else None

    # flightNumber already carries the carrier code (e.g. "QP2074").
    flight_no = str(first.get("flightNumber") or "")
    code = carrier.get("code") or ""
    if flight_no and code and not flight_no.upper().startswith(code.upper()):
        flight_no = f"{code}{flight_no}"

    return {
        "flight_number": flight_no,
        "airline": carrier.get("name") or carrier.get("code"),
        "airline_code": carrier.get("code"),
        "airline_logo": logo.get("large") or logo.get("small"),
        "from_airport": origin.get("airportCode") or origin.get("code") or from_iata.upper(),
        "to_airport": dest.get("airportCode") or dest.get("code") or to_iata.upper(),
        "from_city": origin.get("cityName"),
        "to_city": dest.get("cityName"),
        "departure_time": dep_at,
        "arrival_time": arr_at,
        "duration_min": duration_min,
        "stops": len(segments) - 1,
        "cabin_class": first.get("cabinClassName") or "ECONOMY",
        "baggage_kg": _checked_baggage_kg(trip),
        "price_total": price_total,
        "currency": currency,
        "booking_url": trip.get("shareableUrl"),
        "source": "gotogate",
        # ── Enriched signals ──
        "cabin_baggage_kg": _cabin_baggage_kg(trip),
        "fare_tags": trip.get("tripTags") or None,      # ["PRICE","WEIGHTED"] → cheapest / best value
        "layover_airports": layover_airports or None,
        "layover_minutes": layover_minutes or None,
    }


def _checked_baggage_kg(trip: dict) -> int | None:
    """Included checked-baggage allowance in kg (trip level, else first bound)."""
    candidates = [trip.get("includedCheckedBaggage")]
    for bound in (trip.get("bounds") or []):
        candidates.append(bound.get("includedCheckedBaggage"))
    for bag in candidates:
        if not isinstance(bag, dict):
            continue
        weight = bag.get("weight")
        unit = (bag.get("weightUnit") or "").upper()
        if isinstance(weight, (int, float)) and weight > 0 and unit in ("", "KG", "KILOGRAM", "KILOGRAMS"):
            return int(weight)
    return None


def _cabin_baggage_kg(trip: dict) -> int | None:
    """Included cabin/carry-on allowance in kg (trip level, else first bound)."""
    candidates = [trip.get("includedCabinBaggage")]
    for bound in (trip.get("bounds") or []):
        candidates.append(bound.get("includedCabinBaggage"))
    for bag in candidates:
        if not isinstance(bag, dict):
            continue
        weight = bag.get("weight")
        if isinstance(weight, (int, float)) and weight > 0:
            return int(weight)
    return None


def _duration(dep: str | None, arr: str | None) -> int | None:
    """Minutes between two ISO-8601 timestamps; tolerant of trailing 'Z'."""
    d, a = _parse_iso(dep), _parse_iso(arr)
    if d and a:
        return max(0, int((a - d).total_seconds() // 60))
    return None


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
        if m:
            try:
                return datetime.fromisoformat(m.group(1))
            except Exception:
                return None
        return None


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip()) if v not in (None, "") else None
    except Exception:
        return None
