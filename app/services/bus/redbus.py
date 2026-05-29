"""
RedBus API integration — city suggestion + bus search.

City suggestion: GET /rpw/api/citySuggestion?search=<name>&limit=10
Bus search:      POST /rpw/api/searchResults  (query params + JSON filter body)
"""
from datetime import datetime
from curl_cffi.requests import AsyncSession as CurlAsyncSession

_BASE = "https://www.redbus.in"
_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "origin": _BASE,
    "referer": f"{_BASE}/",
    "content-type": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}
_EMPTY_FILTERS = {
    "appliedFilterCount": 0, "onlyShow": [], "dt": [], "SeaterType": [],
    "AcType": [], "travelsList": [], "amtList": [], "bpList": [], "dpList": [],
    "CampaignFilter": [], "at": [], "persuasionList": [], "bpIdentifier": [],
    "dpIdentifier": [], "bcf": [], "opBusTypeFilterList": [], "priceRange": [],
    "RouteIds": [], "bpKeys": [], "dpKeys": [], "streaksFilter": [],
    "preRouteFilters": None,
}
_AMENITY = {
    4: "Charging Point", 5: "Water Bottle", 6: "Blanket",
    7: "Snacks", 9: "Reading Light", 19: "Live Tracking",
    26: "WiFi", 91: "Premium Seat", 93: "USB Port",
}

async def search_city(name: str) -> list[dict]:
    """
    Resolve a city name to RedBus city/area IDs.
    Returns [{id, name, type, boarding_points: [{id, name}]}]
    """
    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.get(
                f"{_BASE}/rpw/api/citySuggestion",
                headers=_HEADERS,
                params={"search": name, "limit": 10, "routeDetection": "false"},
                timeout=20,
            )
        r.raise_for_status()
        docs = r.json().get("response", {}).get("docs", [])
        return [
            {
                "id": d["ID"],
                "name": d["locationName"],
                "type": d["locationType"],  # "CITY" or "AREA"
                "boarding_points": [
                    {"id": bp["ID"], "name": bp["locationName"]}
                    for bp in d.get("BpList", [])
                ],
            }
            for d in docs
        ]
    except Exception as e:
        print(f"[redbus] city search '{name}': {e}")
        return []


_CITY_SLUG_CACHE: dict[int, str] = {}   # city_id → url-slug (populated from search results)


async def search_buses(
    from_city_id: int,
    to_city_id: int,
    date: str,
    limit: int = 10,
    from_city_name: str = "",
    to_city_name: str = "",
) -> list[dict]:
    """
    Search buses between two RedBus city IDs.
    date: 'YYYY-MM-DD' (auto-converted) or 'DD-Mon-YYYY' (e.g. '31-May-2026').
    Returns normalized list of bus options sorted by departure time.
    """
    try:
        doj = datetime.strptime(date, "%Y-%m-%d").strftime("%d-%b-%Y")
    except ValueError:
        doj = date

    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.post(
                f"{_BASE}/rpw/api/searchResults",
                headers=_HEADERS,
                params={
                    "fromCity": from_city_id, "toCity": to_city_id,
                    "DOJ": doj, "limit": limit, "offset": 0,
                    "meta": "true", "groupId": 0, "sectionId": 0,
                    "sort": 0, "sortOrder": 0,
                    "from": "initialLoad", "getUuid": "true",
                    "bT": 1, "isFilterApplied": "false",
                },
                json=_EMPTY_FILTERS,
                timeout=25,
            )
        r.raise_for_status()
        data = r.json().get("data", {})
        inventories = data.get("inventories") or []
        # Build a search-level booking URL for this route+date
        search_url = _build_search_url(from_city_id, to_city_id, doj, from_city_name, to_city_name)
        return [_normalise_inventory(inv, search_url) for inv in inventories[:limit]]
    except Exception as e:
        print(f"[redbus] bus search {from_city_id}→{to_city_id} on {doj}: {e}")
        return []


def _build_search_url(from_id: int, to_id: int, doj: str, from_name: str, to_name: str) -> str:
    def _slug(name: str) -> str:
        return name.lower().strip().replace(" ", "-").replace(",", "") if name else ""

    slug_from = _slug(from_name) or str(from_id)
    slug_to   = _slug(to_name)   or str(to_id)
    return (
        f"https://www.redbus.in/bus-tickets/{slug_from}-to-{slug_to}"
        f"?fromCityId={from_id}&toCityId={to_id}&onward={doj}"
    )


def _normalise_inventory(inv: dict, search_url: str = "") -> dict:
    fare_list = inv.get("fareList") or []
    campaign_list = (inv.get("operatorOfferCampaign") or {}).get("CmpgList") or []
    discounted = (campaign_list[0].get("DiscountedPrices") or []) if campaign_list else []

    return {
        "operator": inv.get("travelsName"),
        "operator_id": inv.get("operatorId"),
        "route_id": inv.get("routeId"),
        "bus_type": inv.get("busType"),
        "departure": inv.get("departureTime"),        # "YYYY-MM-DD HH:MM:SS"
        "arrival": inv.get("arrivalTime"),
        "duration_min": inv.get("journeyDurationMin"),
        "boarding_point": inv.get("standardBpName"),
        "dropping_point": inv.get("standardDpName"),  # real last-mile handoff point
        "fare_original": min(fare_list) if fare_list else None,
        "fare_discounted": min(discounted) if discounted else None,
        "available_seats": inv.get("availableSeats"),
        "total_seats": inv.get("totalSeats"),
        "rating": inv.get("totalRatings"),
        "is_ac": inv.get("isAc", False),
        "is_live_tracking": inv.get("isLiveTrackingAvailable", False),
        "is_m_ticket": inv.get("isMticketEnabled", False),
        "amenities": [_AMENITY.get(a, str(a)) for a in (inv.get("amenities") or [])],
        "cancellation_policy": _parse_cancellation(inv.get("cancellationPolicy", "")),
        "logo_url": "https://origin-st.redbus.in/buslogos/country/" + inv.get("operatorLogoPath", ""),
        "bp_id": (inv.get("locationSearchParams") or {}).get("bpId"),
        "dp_id": (inv.get("locationSearchParams") or {}).get("dpId"),
        "booking_url": search_url,
    }


def _parse_cancellation(policy: str) -> list[dict]:
    """'0:12:100:0;12:24:80:0;24:48:50:0' → [{window, refund_percent}]"""
    if not policy:
        return []
    tiers = []
    for part in policy.split(";"):
        try:
            from_h, to_h, pct, _ = part.split(":")
            from_h, to_h, pct = int(from_h), int(to_h), int(pct)
            if to_h == -1:
                window = f"More than {from_h}h before"
            elif from_h == 0:
                window = f"Within {to_h}h of departure"
            else:
                window = f"{from_h}–{to_h}h before"
            tiers.append({"window": window, "refund_percent": pct})
        except Exception:
            continue
    return tiers
