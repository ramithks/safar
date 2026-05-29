"""
Booking.com Flights API integration.
GET https://flights.booking.com/api/flights/

This is the same API the booking.com website uses for flight search.
No auth key needed — only a few stable custom headers.
totalTime in the response is in SECONDS, not minutes.
"""
from curl_cffi.requests import AsyncSession as CurlAsyncSession

_BASE = "https://flights.booking.com/api/flights/"
_HEADERS = {
    "accept": "*/*",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "x-booking-affiliate-id": "304142",
    "x-flights-context-name": "search_results",
    "x-flights-context-pos": "in",   # "in" = India point-of-sale for INR pricing
    "x-requested-from": "clientFetch",
}

# Major Indian airports: city name variants → IATA code
INDIA_AIRPORTS: dict[str, str] = {
    "delhi": "DEL", "new delhi": "DEL", "indira gandhi": "DEL",
    "mumbai": "BOM", "bombay": "BOM",
    "bengaluru": "BLR", "bangalore": "BLR",
    "hyderabad": "HYD",
    "chennai": "MAA", "madras": "MAA",
    "kolkata": "CCU", "calcutta": "CCU",
    "ahmedabad": "AMD",
    "pune": "PNQ",
    "goa": "GOI", "dabolim": "GOI",
    "kochi": "COK", "cochin": "COK",
    "jaipur": "JAI",
    "lucknow": "LKO",
    "chandigarh": "IXC",
    "amritsar": "ATQ",
    "srinagar": "SXR",
    "leh": "IXL", "ladakh": "IXL",
    "dehradun": "DED",
    "varanasi": "VNS",
    "bhopal": "BHO",
    "nagpur": "NAG",
    "patna": "PAT",
    "bhubaneswar": "BBI",
    "guwahati": "GAU",
    "coimbatore": "CJB",
    "tiruchirappalli": "TRZ", "trichy": "TRZ",
    "madurai": "IXM",
    "visakhapatnam": "VTZ", "vizag": "VTZ",
    "mangalore": "IXE",
    "calicut": "CCJ", "kozhikode": "CCJ",
    "imphal": "IMF",
    "port blair": "IXZ",
    "bagdogra": "IXB", "siliguri": "IXB",
    "udaipur": "UDR",
    "jodhpur": "JDH",
    "jammu": "IXJ",
    "indore": "IDR",
    "raipur": "RPR",
    "ranchi": "IXR",
    # Himachal Pradesh
    "kullu": "KUU", "bhuntar": "KUU", "kullu manali": "KUU", "manali": "KUU",
    "shimla": "SLV",
    "dharamshala": "DHM", "kangra": "DHM", "gaggal": "DHM", "mcleod ganj": "DHM",
    # Additional metros & tier-2
    "surat": "STV", "vadodara": "BDQ", "baroda": "BDQ",
    "rajkot": "RAJ", "bhuj": "BHJ",
    "hubli": "HBX", "hubballi": "HBX",
    "belgaum": "IXG", "belagavi": "IXG",
    "tirupati": "TIR", "mysore": "MYQ", "mysuru": "MYQ",
    "aurangabad": "IXU", "nashik": "ISK", "kolhapur": "KLH",
    "jaisalmer": "JSA", "bikaner": "BKB",
    "gorakhpur": "GOP", "allahabad": "IXD", "prayagraj": "IXD",
    "agra": "AGR", "gwalior": "GWL",
    "dibrugarh": "DIB", "jorhat": "JRH", "silchar": "IXS",
    "agartala": "IXA", "aizawl": "AJL",
    "port blair": "IXZ", "andaman": "IXZ",
    "dimapur": "DMU",
}


def city_to_iata(city: str) -> str | None:
    """Best-effort city name → IATA code lookup."""
    key = city.lower().strip()
    if key in INDIA_AIRPORTS:
        return INDIA_AIRPORTS[key]
    # partial match
    for k, code in INDIA_AIRPORTS.items():
        if k in key or key in k:
            return code
    return None


async def search_flights(
    from_airport: str,    # IATA code e.g. "DEL"
    to_airport: str,      # IATA code e.g. "IXC"
    depart_date: str,     # "YYYY-MM-DD"
    adults: int = 1,
    cabin_class: str = "ECONOMY",
    sort: str = "CHEAPEST",
    limit: int = 5,
) -> list[dict]:
    """
    Search one-way flights. Returns normalized list sorted by sort param.
    sort: 'CHEAPEST' | 'FASTEST' | 'BEST'
    """
    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.get(
                _BASE,
                headers=_HEADERS,
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
                    "aid": "304142",
                    "enableVI": "1",
                },
                timeout=20,
            )
        r.raise_for_status()
        offers = r.json().get("flightOffers") or []
        return [_normalise_offer(o) for o in offers[:limit]]
    except Exception as e:
        print(f"[flights_booking] {from_airport}→{to_airport} on {depart_date}: {e}")
        return []


def _normalise_offer(offer: dict) -> dict:
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

    total_sec = seg.get("totalTime") or 0  # response is in SECONDS
    stops = len(legs) - 1

    return {
        "flight_number": f"{carrier_info.get('marketingCarrier', '')}{flight_info.get('flightNumber', '')}",
        "airline": carriers[0].get("name") if carriers else None,
        "airline_code": carriers[0].get("code") if carriers else None,
        "airline_logo": carriers[0].get("logo") if carriers else None,
        "from_airport": (seg.get("departureAirport") or {}).get("code"),
        "to_airport": (seg.get("arrivalAirport") or {}).get("code"),
        "from_city": (seg.get("departureAirport") or {}).get("cityName"),
        "to_city": (seg.get("arrivalAirport") or {}).get("cityName"),
        "departure_time": seg.get("departureTime"),       # ISO datetime string
        "arrival_time": seg.get("arrivalTime"),
        "duration_min": total_sec // 60,                  # converted from seconds
        "stops": stops,
        "cabin_class": leg0.get("cabinClass", "ECONOMY"),
        "departure_terminal": leg0.get("departureTerminal"),
        "price_total": _amt("totalRounded") or _amt("total"),
        "price_base_fare": _amt("baseFare"),
        "price_tax": _amt("tax"),
        "currency": (price_bd.get("total") or {}).get("currencyCode", "INR"),
        "booking_token": offer.get("token"),              # opaque token for booking URL
    }
