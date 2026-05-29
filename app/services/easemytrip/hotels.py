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
import uuid
from datetime import date, datetime
from curl_cffi.requests import AsyncSession as CurlAsyncSession

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
    limit: int = 10,
    sort: str = "Popular|DESC",
) -> list[dict]:
    """
    Search hotels on EaseMyTrip for a given city and date range.
    Returns a normalized list of hotel options.
    """
    city_lower = city_name.lower().strip()
    search_key = _build_search_key(city_lower, checkin, checkout, rooms, adults, children)
    cid = _build_cid(city_name)
    traceid = datetime.now().strftime("%Y%d%m%H%M%S")

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
        "sorttype": sort,
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
        "selectedProp": [],
        "selectedTARating": [],
        "selectedRating": [],
        "wlcode": "",
        "userprice": "",
        "isLMR": False,
        "isLPG": False,
        "isfifty": False,
    }

    try:
        async with CurlAsyncSession(impersonate="chrome") as s:
            r = await s.post(_BASE, headers=_HEADERS, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            print(f"[easemytrip] API error for '{city_name}': {data['error']}")
            return []
        return [_normalise_hotel(h, checkin, checkout) for h in (data.get("htllist") or [])]
    except Exception as e:
        print(f"[easemytrip] hotel search '{city_name}' {checkin}→{checkout}: {e}")
        return []


def _normalise_hotel(h: dict, checkin: str, checkout: str) -> dict:
    try:
        nights = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days
    except Exception:
        nights = 1

    images = h.get("imgarry") or []
    if h.get("imgU"):
        images = [h["imgU"]] + [img for img in images if img != h["imgU"]]

    amenities_raw = h.get("amen") or []
    amenities = [a.strip() for a in amenities_raw if a and a.strip()]

    return {
        "hotel_id": h.get("hid"),
        "name": h.get("nm"),
        "category": h.get("catgry"),       # "Hotel" | "Resort Property" | "Homestay"
        "star_rating": _safe_float(h.get("rat")),
        "ta_rating": _safe_float(h.get("tr")),
        "address": h.get("adrs"),
        "distance_from_dest": h.get("dist"),
        "distance_km": h.get("distKM"),
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
        "is_breakfast_included": h.get("isBreakFast", False),
        "cancellation_policy": h.get("plcy"),
        "highlights": h.get("highlt"),     # e.g. "Couple Friendly"
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
