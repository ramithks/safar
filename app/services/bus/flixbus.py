"""
FlixBus API integration — city search + bus/coach search.

Endpoints:
  City autocomplete: GET https://global.api.flixbus.com/search/autocomplete/cities
  Route search:      GET https://global.api.flixbus.com/search/service/v4/search

FlixBus operates in Europe and select markets. Indian routes may return no results
depending on FlixBus's current coverage in India.
"""
from datetime import datetime
from curl_cffi.requests import AsyncSession as CurlAsyncSession

_AUTOCOMPLETE_URL = "https://global.api.flixbus.com/search/autocomplete/cities"
_SEARCH_URL = "https://global.api.flixbus.com/search/service/v4/search"

_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "x-version": "5",
}

async def search_city(name: str, country: str = "in") -> list[dict]:
    """
    Resolve a city name to a FlixBus city ID.
    Returns [{id, name, lat, lon, country}]
    """
    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.get(
                _AUTOCOMPLETE_URL,
                headers=_HEADERS,
                params={
                    "q": name,
                    "language": "en",
                    "country": country,
                    "flixbus_cities_only": "false",
                },
                timeout=20,
            )
        r.raise_for_status()
        results = r.json() if isinstance(r.json(), list) else (r.json().get("results") or [])
        return [
            {
                "id": c.get("id") or c.get("city_id"),
                "name": c.get("name"),
                "lat": (c.get("location") or {}).get("lat"),
                "lon": (c.get("location") or {}).get("lon"),
                "country": c.get("country") or c.get("country_code"),
            }
            for c in results
            if c.get("id") or c.get("city_id")
        ]
    except Exception as e:
        print(f"[flixbus] city search '{name}': {e}")
        return []


async def search_buses(
    from_city_id: str,
    to_city_id: str,
    date: str,          # "YYYY-MM-DD"
    adults: int = 1,
    currency: str = "INR",
    limit: int = 8,
) -> list[dict]:
    """
    Search FlixBus routes. date: 'YYYY-MM-DD' — converted to dd.mm.yyyy for the API.
    Returns normalized list of trip options.
    """
    try:
        dep_date = datetime.strptime(date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError as e:
        print(f"[flixbus] bad date: {e}")
        return []

    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.get(
                _SEARCH_URL,
                headers=_HEADERS,
                params={
                    "from_city_id": from_city_id,
                    "to_city_id": to_city_id,
                    "departure_date": dep_date,
                    "adult": adults,
                    "_locale": "en",
                    "currency": currency,
                    "include_after_midnight_rides": 1,
                    "bikes_allowed": 0,
                },
                timeout=20,
            )
        r.raise_for_status()
        data = r.json()
        trips = data.get("trips") or []
        return [_normalise(t) for t in trips[:limit] if t.get("status") == "available"]
    except Exception as e:
        print(f"[flixbus] bus search {from_city_id}→{to_city_id} on {date}: {e}")
        return []


def _normalise(t: dict) -> dict:
    dep = t.get("departure") or {}
    arr = t.get("arrival") or {}
    dur = t.get("duration") or {}
    price = t.get("price_summary") or t.get("price") or {}

    duration_min = None
    if dur.get("hours") is not None:
        duration_min = dur["hours"] * 60 + (dur.get("minutes") or 0)

    # Price: check nested original / budget_price or flat total_price
    original_price = (
        (price.get("original") or {}).get("amount")
        or price.get("total_price")
        or price.get("amount")
    )
    discounted_price = (
        (price.get("budget_price") or {}).get("amount")
        or (price.get("discount_price") or {}).get("amount")
    )
    currency = (
        (price.get("original") or {}).get("currency")
        or price.get("currency")
        or "INR"
    )

    return {
        "trip_id": t.get("uid") or t.get("id"),
        "operator": "FlixBus",
        "from_city": dep.get("city_name") or (dep.get("city") or {}).get("name"),
        "to_city": arr.get("city_name") or (arr.get("city") or {}).get("name"),
        "from_station": dep.get("station_name") or dep.get("name"),
        "to_station": arr.get("station_name") or arr.get("name"),
        "departure_datetime": dep.get("date") or dep.get("datetime"),
        "arrival_datetime": arr.get("date") or arr.get("datetime"),
        "duration_min": duration_min,
        "fare_original": original_price,
        "fare_discounted": discounted_price,
        "currency": currency,
        "available_seats": t.get("available_seats_count") or t.get("seats_available"),
        "transfer_type": t.get("transfer_type", "direct"),
        "legs": len(t.get("legs") or []),
        "is_eco": t.get("is_eco", False),
        "amenities": _extract_amenities(t),
        "source": "flixbus",
    }


def _extract_amenities(t: dict) -> list[str]:
    raw = t.get("amenities") or []
    if isinstance(raw, list):
        return [
            a.get("name") or a.get("amenity") or str(a)
            for a in raw
            if isinstance(a, dict) and (a.get("name") or a.get("amenity"))
        ]
    return []
