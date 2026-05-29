"""
Expedia Hotels API integration (expedia.co.in — India point-of-sale).

Two endpoints used:
  1. Typeahead REST  → GET /api/v4/typeahead/{query}  → resolves city name → gaiaId + coords
  2. GraphQL         → POST /graphql (PropertyListingQuery, persisted query)  → hotel list

Bot protection: Expedia uses Akamai Bot Manager.
curl_cffi with impersonate="chrome" is required — plain httpx/requests returns 429.

Key response notes (confirmed from live data):
  - Hotel __typename = "LodgingCard" (NOT "Property")
  - Hotel ID lives in the cardLink URL pattern: .h{id}.
  - Price is in priceSection.priceSummary.displayMessages[].lineItems
    role=LEAD → current price, role=STRIKEOUT → original price
  - Total price (incl. taxes) is the second displayMessage's value text
  - Images: mediaSection.gallery.media[].media.url  (trvl-media.com CDN)
  - Free cancellation: "Fully refundable" in tnlFields JSON blob
  - 97 properties found for Shangarh/Tirthan Valley area

Persisted query hash may change when Expedia deploys a new frontend build.
If PropertyListingQuery starts returning PersistedQueryNotFound, update
_PROPERTY_QUERY_HASH from the network tab.
"""
import re
import asyncio
from datetime import datetime

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _CURL_AVAILABLE = True
except ImportError:
    _CURL_AVAILABLE = False
    CurlAsyncSession = None

_GRAPHQL_URL  = "https://www.expedia.co.in/graphql"
_TYPEAHEAD_URL = "https://www.expedia.co.in/api/v4/typeahead/{query}"

# SHA-256 hash of the PropertyListingQuery document (Apollo persisted query).
# Update this if Expedia returns PersistedQueryNotFound.
_PROPERTY_QUERY_HASH = "2e04ea7ec1acdac9235f792efa884ab17f70e513e73a286fbc67d852740a1c5f"

_GRAPHQL_HEADERS = {
    "accept": "application/json, multipart/mixed",
    "accept-language": "en-US,en;q=0.9",
    "cache-control": "max-age=0",
    "client-info": "shopping-pwa,522c3c77340ff756eea2a318598836a9df5ea00a,us-west-2",
    "content-type": "application/json",
    "origin": "https://www.expedia.co.in",
    "referer": "https://www.expedia.co.in/Hotel-Search",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "x-enable-apq": "true",
    "x-page-id": "page.Hotel-Search,H,20",
    "x-shopping-product-line": "lodging",
}
_TYPEAHEAD_HEADERS = {
    "accept": "*/*",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "origin": "https://www.expedia.co.in",
    "referer": "https://www.expedia.co.in/",
}

# Long-lived anonymous session cookies — refresh if API returns 401/403.
_COOKIES = {
    "tpid": "v.1,27",
    "iEAPID": "0",
    "currency": "INR",
    "CRQS": "t|27`s|27`l|en_GB`c|INR",
    "CRQSS": "e|0",
    "linfo": "v.4,|0|0|255|1|0||||||||2057|0|0||0|0|0|-1|-1",
    "EG_ANONTOKEN": (
        "ClAKJgokYmYzMzEwZGUtMzgyNi00Mzc0LTk3ZDMtZTk2NWQzNjEwNDQ2"
        "EiYKJDk5NDY5OWIxLTk5ZGQtNDlhMy1hNTVjLTVlODY2NGQ2MzBhYQ."
        "LPstlEFF2dn9rJWW.GMvrpBwir8blqmz9_ZMdEUQUswXBWwq7yxN_8lzt"
        "muUjSpzS3kK6xajNQUJpMAbP-IvN5atFf2T-KvHAGYZKvaDGbDwRiQqo6"
        "wS4HWMCVQ56o79qwi1r_TdwWIDBdgvUwDm1v-KCqE5pyp_HTtCCb830eZj"
        "7rzVrDkWCn5xODporVAOhTthZyKP2nWdK9dZnfc9is1Gy-WoNFrIOukmaq"
        "9T1x0fY7U3pah07x6p8VE.aGgkb8A64TU5OgxlBZdS9Q"
    ),
    "HMS": "6c246252-65f0-35a7-acad-2f8bf3729952",
    "MC1": "GUID=44175266be3544e8ad969f5fe1836983",
    "DUAID": "44175266-be35-44e8-ad96-9f5fe1836983",
    "eg_ppid": "90fcecb2-2db9-47f4-8b17-c815d64cf887",
    "pageVisited": "true",
}

_CONTEXT = {
    "siteId": 27,
    "locale": "en_GB",
    "eapid": 0,
    "tpid": 27,
    "currency": "INR",
    "device": {"type": "DESKTOP"},
    "identity": {
        "duaid": "44175266-be35-44e8-ad96-9f5fe1836983",
        "authState": "ANONYMOUS",
    },
    "privacyTrackingState": "CAN_TRACK",
}


async def search_region(city_name: str) -> dict | None:
    """
    Resolve a city/destination name to an Expedia region.
    Returns {"gaiaId": str, "regionName": str, "lat": float, "lon": float} or None.
    """
    if not _CURL_AVAILABLE:
        print("[expedia] curl_cffi not installed — pip install curl-cffi")
        return None
    try:
        async with CurlAsyncSession() as s:
            resp = await s.get(
                _TYPEAHEAD_URL.format(query=city_name),
                headers=_TYPEAHEAD_HEADERS,
                cookies=_COOKIES,
                impersonate="chrome",
                timeout=10,
            )
            data = resp.json()
            results = data.get("sr") or []
            if not results:
                return None
            r = results[0]
            coords = r.get("coordinates") or {}
            return {
                "gaiaId": r.get("gaiaId"),
                "regionName": (r.get("regionNames") or {}).get("fullName", city_name),
                "lat": float(coords.get("lat", 0)),
                "lon": float(coords.get("long", 0)),
            }
    except Exception as e:
        print(f"[expedia] typeahead '{city_name}': {e}")
        return None


async def search_hotels(
    city_name: str,
    checkin: str,           # "YYYY-MM-DD"
    checkout: str,          # "YYYY-MM-DD"
    rooms: int = 1,
    adults: int = 2,
    limit: int = 10,
    sort: str = "RECOMMENDED",
    region_id: str | None = None,
) -> list[dict]:
    """
    Search hotels on Expedia for a city and date range.
    Resolves city → gaiaId via typeahead if region_id not provided.
    Returns a normalized list of hotels.
    Requires curl_cffi (impersonates Chrome to bypass Akamai).
    """
    if not _CURL_AVAILABLE:
        print("[expedia] curl_cffi not installed — skipping")
        return []

    # Step 1: resolve region
    region = None
    if region_id:
        region = {"gaiaId": region_id, "regionName": city_name, "lat": 0.0, "lon": 0.0}
    else:
        region = await search_region(city_name)

    if not region:
        print(f"[expedia] could not resolve region for '{city_name}'")
        return []

    # Step 2: parse dates
    try:
        ci = datetime.strptime(checkin, "%Y-%m-%d")
        co = datetime.strptime(checkout, "%Y-%m-%d")
    except ValueError as e:
        print(f"[expedia] bad date: {e}")
        return []

    payload = {
        "operationName": "PropertyListingQuery",
        "variables": {
            "context": _CONTEXT,
            "criteria": {
                "primary": {
                    "dateRange": {
                        "checkInDate":  {"day": ci.day, "month": ci.month, "year": ci.year},
                        "checkOutDate": {"day": co.day, "month": co.month, "year": co.year},
                    },
                    "destination": {
                        "regionName": region["regionName"],
                        "regionId": region["gaiaId"],
                        "coordinates": {"latitude": region["lat"], "longitude": region["lon"]},
                        "pinnedPropertyId": None,
                        "propertyIds": None,
                        "mapBounds": None,
                    },
                    "rooms": [{"adults": adults, "children": []} for _ in range(rooms)],
                },
                "secondary": {
                    "counts": [
                        {"id": "resultsStartingIndex", "value": 0},
                        {"id": "resultsSize", "value": limit},
                    ],
                    "booleans": [],
                    "selections": [
                        {"id": "privacyTrackingState", "value": "CAN_TRACK"},
                        {"id": "sort", "value": sort},
                        {"id": "useRewards", "value": "SHOP_WITHOUT_POINTS"},
                    ],
                    "ranges": [],
                },
            },
            "shoppingContext": {"multiItem": None, "queryTriggeredBy": "PAGE-LOAD"},
        },
        "extensions": {
            "persistedQuery": {"sha256Hash": _PROPERTY_QUERY_HASH, "version": 1}
        },
    }

    import asyncio as _asyncio, random as _random
    await _asyncio.sleep(_random.uniform(0.3, 1.2))   # stagger parallel calls
    for attempt in range(2):   # 1 retry on 429 — keeps total latency under ~4s
        try:
            async with CurlAsyncSession() as s:
                resp = await s.post(
                    _GRAPHQL_URL,
                    headers=_GRAPHQL_HEADERS,
                    cookies=_COOKIES,
                    json=payload,
                    impersonate="chrome",
                    timeout=25,
                )

            if resp.status_code == 429:
                wait = 2 + _random.uniform(0, 1)
                print(f"[expedia] rate limited (429) — retry {attempt + 1}/2 in {wait:.1f}s")
                await _asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                print(f"[expedia] HTTP {resp.status_code} for '{city_name}'")
                return []

            data = resp.json()
            if isinstance(data, list):
                data = data[0]

            errors = data.get("errors") or []
            for err in errors:
                if "PersistedQueryNotFound" in (err.get("message") or ""):
                    print("[expedia] PersistedQueryNotFound — update _PROPERTY_QUERY_HASH")
                    return []

            listings = (data.get("data") or {}).get("propertySearch", {}).get("propertySearchListings", [])
            hotels = [l for l in listings if l.get("__typename") == "LodgingCard"]
            return [_normalise(h, checkin, checkout) for h in hotels[:limit]]

        except Exception as e:
            print(f"[expedia] attempt {attempt + 1} error for '{city_name}': {e}")
            if attempt < 1:
                await _asyncio.sleep(1.5)

    return []


def _normalise(h: dict, checkin: str, checkout: str) -> dict:
    try:
        nights = (
            datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")
        ).days
    except Exception:
        nights = 1

    # --- ID & booking URL ---
    card_link   = h.get("cardLink") or {}
    resource    = card_link.get("resource") or {}
    booking_url = resource.get("value") or resource.get("relativePath") or ""
    hid_match   = re.search(r"\.h(\d+)\.", booking_url)
    hotel_id    = hid_match.group(1) if hid_match else None

    # --- Name & location ---
    heading  = h.get("headingSection") or {}
    name     = (heading.get("heading") or "").strip()
    msgs     = heading.get("messages") or []
    location = msgs[0].get("text", "") if msgs else ""

    # --- Images ---
    media_section = h.get("mediaSection") or {}
    media_list    = (media_section.get("gallery") or {}).get("media") or []
    images = [
        (m.get("media") or {}).get("url", "")
        for m in media_list
        if (m.get("media") or {}).get("url")
    ]

    # --- Price: LEAD (current) and STRIKEOUT (original) ---
    price_sec = h.get("priceSection") or {}
    price_sum = price_sec.get("priceSummary") or {}
    disp_msgs = price_sum.get("displayMessages") or []

    price_lead = price_strikeout = price_total_text = None
    for dm in disp_msgs:
        for item in (dm.get("lineItems") or []):
            role = item.get("role")
            if role == "LEAD":
                price_lead = (item.get("price") or {}).get("formatted")
            elif role == "STRIKEOUT":
                price_strikeout = (item.get("price") or {}).get("formatted")
            # Total price message (no role, has "total" in value text)
            elif item.get("__typename") == "LodgingEnrichedMessage":
                val = item.get("value", "")
                if "total" in val.lower() and not price_total_text:
                    price_total_text = val

    # Extract numeric amount from formatted string e.g. "₹1,728"
    price_numeric = _parse_amount(price_lead)

    # --- Free cancellation ---
    tnl_fields  = h.get("tnlFields") or []
    tnl_str     = json_str = ""
    for f in tnl_fields:
        tnl_str += (f.get("value") or "")
    free_cancel = "Fully refundable" in tnl_str or "Free cancellation" in tnl_str.lower()

    # --- Amenities from headingSection.amenities ---
    amen_items = heading.get("amenities") or []
    amenities  = [a.get("text", "") for a in amen_items if a.get("text")]

    # Also check summarySections
    for ss in (h.get("summarySections") or []):
        for a in (ss.get("amenities") or []):
            txt = a.get("text") or a.get("value") or ""
            if txt and txt not in amenities:
                amenities.append(txt)

    return {
        "hotel_id": hotel_id,
        "name": name or "Unknown",
        "location": location,
        "price_per_night_formatted": price_lead,
        "price_per_night": price_numeric,
        "price_original_formatted": price_strikeout,
        "price_total_text": price_total_text,
        "currency": "INR",
        "nights": nights,
        "free_cancellation": free_cancel,
        "amenities": amenities[:8],
        "photos": [img for img in images[:6] if img],
        "booking_url": booking_url,
        "source": "expedia",
    }


def _parse_amount(formatted: str | None) -> int | None:
    if not formatted:
        return None
    digits = re.sub(r"[^\d]", "", formatted)
    return int(digits) if digits else None
