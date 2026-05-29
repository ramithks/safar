"""
EaseMyTrip Flights API integration.
POST https://flightservice-node.easemytrip.com/AirAvail_Lights/AirBus_New

Key facts:
- TKN is a static frontend token — expiry not enforced server-side (same pattern as hotels).
- _ query param is a ms-timestamp cache-buster; ngsw-bypass skips service worker.
- Cabin: 0=Economy, 1=Business, 2=First, 3=Premium Economy
- isDomestic: true  → only Indian IATA codes apply; set False for international.
- Response key may be "AirSearchResult", "FlightDataList", or "d" (base64 blob).
  _extract_flights() handles all three variants seen in the wild.
"""
import time
from curl_cffi.requests import AsyncSession as CurlAsyncSession

_BASE = "https://flightservice-node.easemytrip.com/AirAvail_Lights/AirBus_New"
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json",
    "origin": "https://www.easemytrip.com",
    "referer": "https://www.easemytrip.com/",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

# Static frontend token — replace if API starts returning 401/403.
_TKN = "Q2QNfiowerLvVcQ4XoTWIRgbs/kAsa/uZaPKfw2pqQv7pN8ypf0EpmUdv9MMj/+t"

_CABIN_CODE = {
    "economy": 0, "business": 1, "first": 2, "premium_economy": 3,
}
_CABIN_NAME = {v: k for k, v in _CABIN_CODE.items()}



async def search_flights(
    from_airport: str,   # IATA e.g. "DEL"
    to_airport: str,     # IATA e.g. "BOM"
    depart_date: str,    # "YYYY-MM-DD"
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    cabin_class: str = "economy",
    limit: int = 5,
) -> list[dict]:
    """
    Search one-way domestic flights on EaseMyTrip.
    Returns a normalized list sorted by total fare ascending.
    """
    cabin_code = _CABIN_CODE.get(cabin_class.lower(), 0)
    ts = int(time.time() * 1000)

    payload = {
        "org": from_airport.upper(),
        "dept": to_airport.upper(),
        "adt": str(adults),
        "chd": str(children),
        "inf": str(infants),
        "deptDT": depart_date,
        "arrDT": "",
        "userid": "",
        "IsDoubelSeat": False,
        "isDomestic": True,
        "isOneway": True,
        "airline": "undefined",
        "Cabin": cabin_code,
        "currCode": "INR",
        "appType": 1,
        "isSingleView": False,
        "ResType": 2,
        "IsNBA": False,
        "CouponCode": "",
        "IsArmedForce": False,
        "AgentCode": "",
        "IsWLAPP": False,
        "IsFareFamily": False,
        "serviceid": "EMTSERVICE;",
        "serviceDepatment": "",
        "IpAddress": "",
        "LoginKey": "",
        "UUID": "",
        "TKN": _TKN,
        "TraceId": "",
        "queryname": "",
        "FareTypeUI": 0,
    }

    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.post(
                _BASE,
                headers=_HEADERS,
                params={"_": ts, "ngsw-bypass": "true"},
                json=payload,
                timeout=25,
            )
        r.raise_for_status()
        raw = r.json()
        flights = _extract_flights(raw)
        flights.sort(key=lambda f: f.get("price_total") or 999999)
        return flights[:limit]
    except Exception as e:
        print(f"[emt_flights] {from_airport}→{to_airport} on {depart_date}: {e}")
        return []


def _extract_flights(raw: dict) -> list[dict]:
    """
    Handles multiple response shapes seen across EMT API versions.
    Tries known top-level keys and returns an empty list on mismatch.
    """
    # Shape 1: {"AirSearchResult": {"FlightDetails": [...]}}
    result_block = raw.get("AirSearchResult") or {}
    items = result_block.get("FlightDetails") or result_block.get("FlightDetail") or []
    if items:
        return [_normalise(f) for f in items]

    # Shape 2: {"FlightDataList": {"results": [...]}}
    fdl = raw.get("FlightDataList") or {}
    items = fdl.get("results") or fdl.get("Results") or []
    if items:
        return [_normalise(f) for f in items]

    # Shape 3: flat array at top level
    if isinstance(raw, list):
        return [_normalise(f) for f in raw]

    # Shape 4: direct "data" wrapper
    data = raw.get("data") or {}
    if isinstance(data, dict):
        items = data.get("FlightDetails") or data.get("results") or []
        return [_normalise(f) for f in items]

    return []


def _normalise(f: dict) -> dict:
    """
    Normalize a single flight entry.
    Field names: EMT uses both abbreviated (al, fn, oa, da) and verbose forms.
    """
    # Airline
    airline_code = f.get("al") or f.get("AirlineCode") or f.get("airlineCode") or ""
    airline_name = f.get("aln") or f.get("AirlineName") or f.get("airlineName") or airline_code
    flight_no = f.get("fn") or f.get("FlightNo") or f.get("flightNo") or ""
    if flight_no and airline_code and not flight_no.startswith(airline_code):
        flight_no = f"{airline_code}-{flight_no}"

    # Route
    origin = f.get("oa") or f.get("org") or f.get("Origin") or f.get("SrcAirport") or ""
    dest = f.get("da") or f.get("dept") or f.get("Dest") or f.get("DstAirport") or ""
    departure_time = f.get("dt") or f.get("DeptTime") or f.get("departureTime") or ""
    arrival_time = f.get("at") or f.get("ArrTime") or f.get("arrivalTime") or ""
    dep_date = f.get("dd") or f.get("DeptDate") or f.get("departureDate") or ""
    arr_date = f.get("ad") or f.get("ArrDate") or f.get("arrivalDate") or ""

    # Duration — stored as minutes (int) or "H:MM" string
    dur_raw = f.get("dur") or f.get("Duration") or f.get("duration") or 0
    duration_min = _parse_duration(dur_raw)

    # Stops — "s" field or nested segments
    stops = f.get("s") or f.get("stops") or f.get("StopCount") or 0
    fg = f.get("fg") or []
    if isinstance(fg, list) and len(fg) > 1:
        stops = len(fg) - 1

    # Fare — nested "f" block or flat fields
    fare_block = f.get("f") or {}
    if not fare_block:
        fare_block = {
            "bf": f.get("bf") or f.get("BaseFare") or 0,
            "tax": f.get("tax") or f.get("Tax") or 0,
            "tf": f.get("tf") or f.get("TotalFare") or f.get("totalFare") or 0,
            "disc": f.get("disc") or f.get("Discount") or 0,
        }

    base_fare = fare_block.get("bf") or fare_block.get("BaseFare") or 0
    tax = fare_block.get("tax") or fare_block.get("Tax") or 0
    total = fare_block.get("tf") or fare_block.get("TotalFare") or (base_fare + tax) or None
    discount = fare_block.get("disc") or fare_block.get("Discount") or 0

    cabin_code = f.get("cc") or f.get("CabinClass") or 0
    cabin = _CABIN_NAME.get(cabin_code, "economy") if isinstance(cabin_code, int) else str(cabin_code)

    return {
        "flight_number": flight_no,
        "airline": airline_name,
        "airline_code": airline_code,
        "from_airport": origin.upper(),
        "to_airport": dest.upper(),
        "departure_date": dep_date,
        "departure_time": departure_time,
        "arrival_date": arr_date,
        "arrival_time": arrival_time,
        "duration_min": duration_min,
        "stops": stops,
        "cabin_class": cabin,
        "price_base_fare": int(base_fare) if base_fare else None,
        "price_tax": int(tax) if tax else None,
        "price_total": int(total) if total else None,
        "discount": int(discount) if discount else None,
        "currency": "INR",
        "seats_available": f.get("sa") or f.get("SeatsAvailable") or None,
        "is_refundable": bool(f.get("refund") or f.get("IsRefundable") or f.get("isRefundable")),
        "source": "easemytrip",
    }


def _parse_duration(raw) -> int | None:
    """Parse duration from int (minutes), 'H:MM', or 'HH:MM:SS' string."""
    if isinstance(raw, int) and raw > 0:
        return raw
    if not raw:
        return None
    s = str(raw).strip()
    parts = s.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        pass
    try:
        return int(s)
    except Exception:
        return None
