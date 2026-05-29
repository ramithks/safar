"""
Booking.com Hotels API integration.
POST https://www.booking.com/dml/graphql  (FullSearch operation)

Two-step flow per search:
  1. Resolve city name → Booking.com destId via autocomplete API
  2. Fetch fresh 24h CSRF token from search page HTML (cached in memory)
  3. POST FullSearch GraphQL with dates + destId

curl_cffi with impersonate="chrome" required — AWS WAF blocks plain requests.
CSRF token is a JWT embedded in the search results page HTML.

Key response paths:
  results[].basicPropertyData.id                           → hotel ID
  results[].displayName.text                               → name
  results[].basicPropertyData.location.{latitude,longitude,address}
  results[].blocks[0].finalPrice.amount                    → per-night price
  results[].priceDisplayInfoIrene.averagePricePerNight.amountUnformatted
  results[].priceDisplayInfoIrene.displayPrice.amountPerStay.amountUnformatted
  results[].policies.showFreeCancellation
  results[].topPhotos[].highResJpegUrl.relativeUrl         → prefix https://cf.bstatic.com
"""
import re
import time
import base64
import secrets
import json as _json

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _CURL_AVAILABLE = True
except ImportError:
    _CURL_AVAILABLE = False
    CurlAsyncSession = None

_GRAPHQL_URL  = "https://www.booking.com/dml/graphql"
_AUTOCOMPLETE_URL = "https://accommodations.booking.com/autocomplete.en-gb.json"
_SEARCH_PAGE  = "https://www.booking.com/searchresults.html"
_PHOTO_BASE   = "https://cf.bstatic.com"

_COMMON_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.booking.com",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "ect": "4g",
    "priority": "u=1, i",
}
_GQL_HEADERS = {
    **_COMMON_HEADERS,
    "content-type": "application/json",
    "apollographql-client-name": "b-search-web-searchresults",
    # apollographql-client-version changes with Booking.com frontend deploys.
    # If the API returns errors about the client version, update from network tab.
    "apollographql-client-version": "aYSHeUCR",
    "x-booking-context-action-name": "searchresults_irene",
    "x-booking-context-aid": "304142",
    "x-booking-site-type-id": "1",
    "x-booking-topic": "capla_browser_b-search-web-searchresults",
}

# Known destIds for common Indian destinations (city name → Booking.com numeric ID).
# Extend via the autocomplete API for anything not listed here.
_DEST_IDS: dict[str, int] = {
    "gurgaon": -2096897, "gurugram": -2096897,
}

# In-process CSRF token cache — valid for 24h per Booking.com's JWT exp field.
_csrf_cache: dict = {"token": None, "exp": 0.0}

# Minimal GraphQL query — only the fields we actually use.
_GQL_QUERY = """
query FullSearch($input: SearchQueryInput!) {
  searchQueries {
    search(input: $input) {
      pagination { nbResultsTotal }
      results {
        basicPropertyData {
          id
          location { address city latitude longitude }
          photos { main { highResJpegUrl { relativeUrl } } }
          reviewScore: reviews {
            score: totalScore
            reviewCount: reviewsCount
            totalScoreTextTag { translation }
          }
          starRating { value }
          pageName
        }
        displayName { text }
        location { mainDistance geoDistanceMeters }
        policies { showFreeCancellation showNoPrepayment }
        mealPlanIncluded { text }
        priceDisplayInfoIrene {
          displayPrice { amountPerStay { amount amountUnformatted currency } }
          averagePricePerNight { amount amountUnformatted currency }
          priceBeforeDiscount { amountPerStay { amount amountUnformatted currency } }
        }
        blocks {
          finalPrice { amount currency }
          originalPrice { amount currency }
          freeCancellationUntil
        }
        topPhotos { highResJpegUrl { relativeUrl } }
        soldOutInfo { isSoldOut }
      }
    }
  }
}
"""


async def resolve_dest_id(city_name: str) -> tuple[int | None, str]:
    """
    Resolve a city name to a Booking.com destId and canonical name.
    Returns (destId, canonical_name). destId is None if resolution fails.
    """
    key = city_name.lower().strip()

    # Fast path: hardcoded dict
    if key in _DEST_IDS:
        return _DEST_IDS[key], city_name

    # Partial match in hardcoded dict
    for k, dest_id in _DEST_IDS.items():
        if k in key or key in k:
            return dest_id, city_name

    # Autocomplete API (no bot protection — use curl_cffi without impersonation)
    try:
        async with CurlAsyncSession() as s:
            r = await s.get(
                _AUTOCOMPLETE_URL,
                headers={"user-agent": _COMMON_HEADERS["user-agent"]},
                params={"query": city_name, "rows": 5, "languagecode": "en-us"},
                timeout=10,
            )
        r.raise_for_status()
        results = r.json().get("results") or []
        # Prefer city (ci) over district/region
        for priority_type in ("ci", "di", "re"):
            for item in results:
                if item.get("type") == priority_type:
                    dest_id = item.get("value")
                    label = item.get("label") or city_name
                    city = label.split(",")[0].strip()
                    # Cache for future calls
                    _DEST_IDS[key] = dest_id
                    return dest_id, city
        if results:
            dest_id = results[0].get("value")
            if dest_id:
                _DEST_IDS[key] = dest_id
                return dest_id, results[0].get("label", city_name).split(",")[0].strip()
    except Exception as e:
        print(f"[booking_hotels] destId resolve '{city_name}': {e}")

    return None, city_name


async def _fetch_csrf_token(session) -> str | None:
    """
    Fetch a fresh CSRF token from Booking.com's search page HTML.
    Cached in memory until ~5 minutes before the JWT expires.
    """
    now = time.time()
    if _csrf_cache["token"] and _csrf_cache["exp"] > now + 300:
        return _csrf_cache["token"]

    try:
        resp = await session.get(
            _SEARCH_PAGE,
            params={"ss": "Delhi", "dest_type": "city", "lang": "en-us"},
            headers={**_COMMON_HEADERS, "sec-fetch-site": "same-origin"},
            impersonate="chrome",
            timeout=15,
        )
        html = resp.text

        # Try several patterns Booking.com has used across frontend versions
        patterns = [
            r'"csrfToken"\s*:\s*"(eyJ[A-Za-z0-9._-]+)"',
            r'"csrf_token"\s*:\s*"(eyJ[A-Za-z0-9._-]+)"',
            r'x-booking-csrf-token["\s,]+:\s*"(eyJ[A-Za-z0-9._-]+)"',
            r'name="x-booking-csrf-token"[^>]+content="(eyJ[A-Za-z0-9._-]+)"',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                token = m.group(1)
                # Parse exp from JWT payload (no signature needed — we're just reading it)
                try:
                    padded = token.split(".")[1] + "=="
                    payload = _json.loads(base64.b64decode(padded + "=" * (4 - len(padded) % 4)))
                    _csrf_cache["exp"] = float(payload.get("exp", now + 86400))
                except Exception:
                    _csrf_cache["exp"] = now + 86400
                _csrf_cache["token"] = token
                return token
    except Exception as e:
        print(f"[booking_hotels] CSRF fetch error: {e}")

    return None


async def search_hotels(
    city_name: str,
    checkin: str,       # "YYYY-MM-DD"
    checkout: str,      # "YYYY-MM-DD"
    rooms: int = 1,
    adults: int = 2,
    children: int = 0,
    sort: str | None = None,   # None=recommended, "price"=cheapest, "class"=stars
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    """
    Search hotels on Booking.com. Returns a normalized list.
    Requires curl_cffi (bypasses AWS WAF with Chrome impersonation).
    """
    if not _CURL_AVAILABLE:
        print("[booking_hotels] curl_cffi not installed — pip install curl-cffi")
        return []

    dest_id, city_canonical = await resolve_dest_id(city_name)
    # For small/obscure Indian destinations, try broader fallback name variants
    if dest_id is None:
        for variant in [f"{city_name} India", f"{city_name} Himachal Pradesh", f"{city_name} Uttarakhand"]:
            dest_id, city_canonical = await resolve_dest_id(variant)
            if dest_id is not None:
                print(f"[booking_hotels] resolved '{city_name}' via fallback '{variant}'")
                break
    if dest_id is None:
        print(f"[booking_hotels] could not resolve destId for '{city_name}' — skipping")
        return []

    pageview_id = secrets.token_hex(8)  # 16-char random hex, fresh per request

    try:
        async with CurlAsyncSession() as s:
            csrf_token = await _fetch_csrf_token(s)
            if not csrf_token:
                print("[booking_hotels] could not obtain CSRF token")
                return []

            gql_headers = {
                **_GQL_HEADERS,
                "x-booking-csrf-token": csrf_token,
                "x-booking-pageview-id": pageview_id,
                "referer": (
                    f"{_SEARCH_PAGE}?ss={city_name}&dest_id={dest_id}&dest_type=city"
                    f"&checkin={checkin}&checkout={checkout}"
                    f"&group_adults={adults}&no_rooms={rooms}&group_children={children}"
                ),
            }

            raw_query = (
                f"/searchresults.html?ss={city_name}&dest_id={dest_id}&dest_type=city"
                f"&checkin={checkin}&checkout={checkout}"
                f"&group_adults={adults}&no_rooms={rooms}&group_children={children}"
                f"&lang=en-us&aid=304142"
            )

            sorters_block = {
                "selectedSorter": sort,
                "referenceGeoId": None,
                "tripTypeIntentId": None,
            }

            payload = {
                "operationName": "FullSearch",
                "variables": {
                    "includeBundle": False,
                    "input": {
                        "acidCarouselContext": None,
                        "childrenAges": [],
                        "doAvailabilityCheck": False,
                        "encodedAutocompleteMeta": None,
                        "enableCampaigns": True,
                        "filters": {},
                        "flexibleDatesConfig": {
                            "broadDatesCalendar": {
                                "checkinMonths": [], "los": [], "startWeekdays": []
                            },
                            "dateFlexUseCase": "DATE_RANGE",
                            "dateRangeCalendar": {
                                "checkin": [checkin],
                                "checkout": [checkout],
                            },
                        },
                        "forcedBlocks": None,
                        "location": {
                            "searchString": city_canonical,
                            "destType": "CITY",
                            "destId": dest_id,
                        },
                        "metaContext": {
                            "metaCampaignId": 0,
                            "externalTotalPrice": None,
                            "feedPrice": None,
                            "hotelCenterAccountId": None,
                            "rateRuleId": None,
                            "dragongateTraceId": None,
                            "pricingProductsTag": None,
                        },
                        "nbRooms": rooms,
                        "nbAdults": adults,
                        "nbChildren": children,
                        "showAparthotelAsHotel": True,
                        "needsRoomsMatch": False,
                        "optionalFeatures": {
                            "forceArpExperiments": True,
                            "testProperties": False,
                        },
                        "pagination": {
                            "rowsPerPage": limit,
                            "offset": offset,
                        },
                        "rawQueryForSession": raw_query,
                        "referrerBlock": {"blockName": "searchbox"},
                        "sbCalendarOpen": False,
                        "sorters": sorters_block,
                        "travelPurpose": 2,
                        "seoThemeIds": [],
                        "useSearchParamsFromSession": False,
                        "clientSideRequestId": pageview_id,
                    },
                    "carouselLowCodeExp": False,
                },
                "query": _GQL_QUERY,
            }

            resp = await s.post(
                _GRAPHQL_URL,
                headers=gql_headers,
                json=payload,
                impersonate="chrome",
                timeout=25,
            )

        if resp.status_code != 200:
            print(f"[booking_hotels] HTTP {resp.status_code} for '{city_name}'")
            return []

        data = resp.json()
        errors = data.get("errors") or []
        if errors:
            print(f"[booking_hotels] GraphQL errors: {[e.get('message') for e in errors[:2]]}")
            return []

        results = (
            (data.get("data") or {})
            .get("searchQueries", {})
            .get("search", {})
            .get("results") or []
        )

        from datetime import datetime
        try:
            nights = (
                datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")
            ).days
        except Exception:
            nights = 1

        return [_normalise(r, checkin, checkout, nights) for r in results[:limit]]

    except Exception as e:
        print(f"[booking_hotels] search '{city_name}' {checkin}→{checkout}: {e}")
        return []


def _normalise(r: dict, checkin: str, checkout: str, nights: int) -> dict:
    basic = r.get("basicPropertyData") or {}
    loc = basic.get("location") or {}
    reviews = basic.get("reviewScore") or {}
    star = basic.get("starRating") or {}

    name = (r.get("displayName") or {}).get("text") or "Unknown"
    hotel_id = basic.get("id")
    page_name = basic.get("pageName") or ""
    booking_url = f"https://www.booking.com/hotel/in/{page_name}.html" if page_name else None

    # Photos — prefix CDN base URL
    photos: list[str] = []
    for tp in (r.get("topPhotos") or []):
        rel = (tp.get("highResJpegUrl") or {}).get("relativeUrl")
        if rel:
            photos.append(_PHOTO_BASE + rel)
    if not photos:
        main_photo = ((basic.get("photos") or {}).get("main") or {})
        rel = (main_photo.get("highResJpegUrl") or {}).get("relativeUrl")
        if rel:
            photos.append(_PHOTO_BASE + rel)

    # Price
    price_info = r.get("priceDisplayInfoIrene") or {}
    avg_night = (price_info.get("averagePricePerNight") or {}).get("amountUnformatted")
    per_stay = (
        (price_info.get("displayPrice") or {})
        .get("amountPerStay", {})
        .get("amountUnformatted")
    )
    original_stay = (
        (price_info.get("priceBeforeDiscount") or {})
        .get("amountPerStay", {})
        .get("amountUnformatted")
    )
    currency = (
        (price_info.get("averagePricePerNight") or {}).get("currency") or "INR"
    )

    # Block price (per room per night shown on card)
    blocks = r.get("blocks") or []
    block0 = blocks[0] if blocks else {}
    block_price = _safe_float(block0.get("finalPrice", {}).get("amount"))
    free_cancel_until = block0.get("freeCancellationUntil")

    price_per_night = avg_night or block_price
    policies = r.get("policies") or {}
    free_cancel = policies.get("showFreeCancellation", False) or bool(free_cancel_until)

    meal = (r.get("mealPlanIncluded") or {}).get("text")
    dist = r.get("location") or {}
    sold_out = (r.get("soldOutInfo") or {}).get("isSoldOut", False)

    return {
        "hotel_id": str(hotel_id) if hotel_id else None,
        "name": name,
        "address": loc.get("address"),
        "city": loc.get("city"),
        "lat": _safe_float(loc.get("latitude")),
        "lon": _safe_float(loc.get("longitude")),
        "distance_from_centre": dist.get("mainDistance"),
        "star_rating": _safe_float(star.get("value")),
        "review_score": _safe_float(reviews.get("score")),
        "review_count": reviews.get("reviewCount"),
        "review_label": (reviews.get("totalScoreTextTag") or {}).get("translation"),
        "price_per_night": _safe_int(price_per_night),
        "price_per_stay": _safe_int(per_stay),
        "price_original_stay": _safe_int(original_stay),
        "currency": currency,
        "nights": nights,
        "checkin": checkin,
        "checkout": checkout,
        "free_cancellation": free_cancel,
        "free_cancellation_until": free_cancel_until,
        "no_prepayment": policies.get("showNoPrepayment", False),
        "meal_plan": meal,
        "photos": photos[:6],
        "booking_url": booking_url,
        "is_sold_out": sold_out,
        "source": "booking",
    }


def _safe_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _safe_int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except Exception:
        return None
