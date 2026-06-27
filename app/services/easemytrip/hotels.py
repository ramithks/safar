"""
EaseMyTrip Hotels API integration.
POST https://hotelservice.easemytrip.com/api/HotelService/HotelListIdWiseNew

Key facts confirmed from live testing:
- The JWT token expiry is NOT enforced server-side — a static token works indefinitely.
- CID is a tracking param (not used for filtering); pattern is "AR" + city[:6].upper().
- SearchKey format: "15~INR~{CITY_UPPER}~{YYYY-MM-DD}~{YYYY-MM-DD}~{rooms}~{adults}_{children}~~~EASEMYTRIP~NA~NA~NA~IN"
- Hotels are in the "htllist" key of the response.
- tPr = total price for stay; prc = price per night; tax = tax amount.
"""
import logging
import uuid
from datetime import date, datetime
from curl_cffi.requests import AsyncSession as CurlAsyncSession

from app.services.scraper_proxy import with_proxy_fallback

logger = logging.getLogger(__name__)

_BASE = "https://hotelservice.easemytrip.com/api/HotelService/HotelListIdWiseNew"
_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://www.easemytrip.com",
    "referer": "https://www.easemytrip.com/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
}

# Static credentials embedded in the EaseMyTrip frontend — token expiry is not enforced.
_AUTH = {
    "AgentCode": 1,
    "UserName": "EaseMyTrip",
    "Password": "C2KYph9PJFy6XyF6GT7SAeTq2d5e9Psrq5vmH34S",
    "loginInfo": "",
}
_EMT_TOKEN = (
    "yBAP2WJqhwAQBMyu9kNBUZ3I1W6kSIuGcjFoLCku/QE7BkECtmM9PVH9c8hNl23BVBQmIcert31b2Z4qZ"
    "+rmabkDwv+c+ueRclp76OC64cDJs+Sekt9kwrYeTffFDw63dDeu+ouftE4b4TGh6luO2fFryNnQKUSFdq4"
    "f8ElhQ0IzWvVpwbh46WIn/KN4BDFrImax3uBpctbfHeW0osBOIw7eBEkU39FEX7E0xX6ng/JTMJ51Rehh"
    "9mDUVlWHfndZ+bJDIurkv2BqdNa/HTY5XGcsLz0zNjRhpZBPN7UvwzcoiTuQF2z6RUw80qpo2rLBaiuJN"
    "7D3BqSD0qSWlNhlLepkgxTuNzwto5xzfXQWS/0lwwXd70NlZI6Os11AtMo+4SAnX6lUGXdIiYaTBlrHkA==f"
)
_JWT_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJ1bmlxdWVfbmFtZSI6IkVBU0VNWVRSSVAiLCJuYmYiOjE3ODAwNDgyMzYsImV4cCI6MTc4MDA1MDAzNiwiaWF0IjoxNzgwMDQ4MjM2fQ"
    ".BcyiSrb_Q4J4wvNUCdKDP4NZofUxL3dH6947Y-CobvEf"
)

# Native sort codes — canonical sort key → EaseMyTrip `sorttype` ("Field|DIR").
_SORTS = {
    "price_asc":   "Price|ASC",
    "price_desc":  "Price|DESC",
    "recommended": "Popular|DESC",
    "rating":      "Rating|DESC",
}
# Native property-type filter — canonical type → EMT `selectedProp` value
# (matches the `catgry` field in responses; verified live: Hostel/Hotel/Resort
# Property/Homestay/Apartment all filter correctly).
_PROP_FILTER = {
    "hostel":    "Hostel",
    "hotel":     "Hotel",
    "resort":    "Resort Property",
    "homestay":  "Homestay",
    "apartment": "Apartment",
}


def _build_search_key(city_name: str, checkin: str, checkout: str, rooms: int, adults: int, children: int) -> str:
    city_upper = city_name.upper().replace(" ", ",").replace(",,", ",")
    return f"15~INR~{city_upper}~{checkin}~{checkout}~{rooms}~{adults}_{children}~~~EASEMYTRIP~NA~NA~NA~IN"


def _build_cid(city_name: str) -> str:
    code = city_name.upper().replace(" ", "").replace(",", "")[:6]
    return f"AR{code}"


async def search_hotels(
    city_name: str,
    checkin: str,      # "YYYY-MM-DD"
    checkout: str,     # "YYYY-MM-DD"
    rooms: int = 1,
    adults: int = 2,
    children: int = 0,
    page: int = 1,
    limit: int = 40,
    sort: str | None = "recommended",
    property_types: list[str] | None = None,
) -> list[dict]:
    """
    Search hotels on EaseMyTrip for a given city and date range.
    `sort` is a canonical key (price_asc|price_desc|recommended|rating); a native
    "Field|DIR" string is also accepted as-is. `property_types` is a list of
    canonical types — mapped to EMT's `selectedProp` to restrict the result set.
    Returns a normalized list of hotel options.
    """
    city_lower = city_name.lower().strip()
    search_key = _build_search_key(city_lower, checkin, checkout, rooms, adults, children)
    cid = _build_cid(city_name)
    traceid = datetime.now().strftime("%Y%d%m%H%M%S")

    sorttype = _SORTS.get(sort) or (sort if (sort and "|" in sort) else "Popular|DESC")
    selected_prop = [
        _PROP_FILTER[t] for t in (property_types or []) if t in _PROP_FILTER
    ]

    payload = {
        "PageNo": page,
        "CID": cid,
        "searchType": "area",
        "RoomDetails": [{"NoOfRooms": rooms, "NoOfAdult": adults, "NoOfChild": children, "childAge": ""}],
        "SearchKey": search_key,
        "HotelCount": limit,
        "CheckInDate": checkin,
        "CheckOut": checkout,
        "CityCode": city_lower,
        "CityName": city_lower,
        "EMTCommonID": "",
        "NoOfRooms": rooms,
        "sorttype": sorttype,
        "auth": _AUTH,
        "minPrice": 1,
        "maxPrice": 10_000_000,
        "hotelid": [],
        "emtToken": _EMT_TOKEN,
        "token": _JWT_TOKEN,
        "traceid": traceid,
        "vid": str(uuid.uuid4()).replace("-", ""),
        "ipaddress": "",
        "lat": "",
        "lon": "",
        "name": "",
        "Name": "",
        "lstLoc": [],
        "selectchain": [],
        "selectedAreas": [],
        "selectedAmen": [],
        "selectedProp": selected_prop,
        "selectedTARating": [],
        "selectedRating": [],
        "wlcode": "",
        "userprice": "",
        "isLMR": False,
        "isLPG": False,
        "isfifty": False,
    }

    async def _do(proxy):
        async with CurlAsyncSession(impersonate="chrome", proxy=proxy) as s:
            r = await s.post(_BASE, headers=_HEADERS, json=payload, timeout=20)
        r.raise_for_status()
        return r.json()

    try:
        data = await with_proxy_fallback("easemytrip", _do)
        if data.get("error"):
            logger.warning("[easemytrip] API error for %r: %s", city_name, data["error"])
            return []
        return [_normalise_hotel(h, checkin, checkout) for h in (data.get("htllist") or [])]
    except Exception as e:
        logger.warning("[easemytrip] hotel search %r %s→%s: %s", city_name, checkin, checkout, e)
        return []


def _normalise_hotel(h: dict, checkin: str, checkout: str) -> dict:
    try:
        nights = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days
    except Exception:
        nights = 1

    from app.services.hotels_filters import classify_property_type

    images = h.get("imgarry") or []
    if h.get("imgU"):
        images = [h["imgU"]] + [img for img in images if img != h["imgU"]]

    amenities_raw = h.get("amen") or []
    amenities = [a.strip() for a in amenities_raw if a and a.strip()]

    # Cancellation policy: htlPlcy has human-readable text (e.g. "NonRefundable Booking")
    # plcy is the raw policy blob; prefer htlPlcy when it has content
    cancellation_policy = h.get("htlPlcy") or h.get("plcy") or None

    # Meal plan: EMT has both a `meal` text field and `isBreakFast` flag
    meal_text = h.get("meal")
    is_breakfast = h.get("isBreakFast", False)
    meal_plan = meal_text or ("Breakfast included" if is_breakfast else None)

    # Review count from TripAdvisor count
    try:
        review_count = int(h.get("tCount") or 0) or None
    except Exception:
        review_count = None

    # Distance: distKM=0 means no distance data (hotel is at the destination)
    dist_km = h.get("distKM")
    if dist_km is not None:
        try:
            dist_km = float(dist_km) or None   # treat 0.0 as None (unknown)
        except Exception:
            dist_km = None

    category = h.get("catgry")
    return {
        "hotel_id": h.get("hid"),
        "name": h.get("nm"),
        "category": category,              # "Hotel" | "Resort Property" | "Homestay"
        "property_type": classify_property_type({"category": category, "name": h.get("nm")}),
        "star_rating": _safe_float(h.get("rat")),
        "ta_rating": _safe_float(h.get("tr")),
        "review_count": review_count,
        "address": h.get("adrs"),
        "distance_from_dest": h.get("dist"),
        "distance_km": dist_km,
        "lat": _safe_float(h.get("lat")),
        "lon": _safe_float(h.get("lon")),
        "price_per_night": h.get("prc"),
        "total_price": h.get("tPr"),       # includes tax
        "tax": h.get("tax"),
        "discount": h.get("disc"),
        "currency": h.get("curr", "INR"),
        "nights": nights,
        "checkin_time": h.get("cinTime"),
        "checkout_time": h.get("coutTime"),
        "is_breakfast_included": is_breakfast,
        "meal_plan": meal_plan,
        "cancellation_policy": cancellation_policy,
        "couple_friendly": bool(h.get("isCF", False)),
        "highlights": h.get("highlt"),     # e.g. "Couple Friendly|Solo Traveller Friendly"
        "tags": h.get("tags"),              # e.g. "New on EMT"
        "amenities": amenities,
        "photos": images[:6],
        "booking_url": h.get("durl"),
        "is_sold_out": h.get("isSold", False),
        "coupon_code": h.get("cpn"),
        "coupon_note": h.get("apTxt"),
        "source": "easemytrip",
    }


def _safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "0") else None
    except Exception:
        return None
