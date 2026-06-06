"""
Booking.com Hotels API integration.
POST https://www.booking.com/dml/graphql  (FullSearch operation)

Two-step flow per search:
  1. Resolve city name → destId + destType via autocomplete API
  2. POST FullSearch GraphQL with dates + destId

curl_cffi with impersonate="chrome" required — AWS WAF blocks plain requests.

Key confirmed response paths (live-tested 2026-05-30):
  search.__typename = "SearchQueryOutput"  (inline fragment required)
  results[].basicPropertyData.id / pageName / location / reviewScore / starRating
  results[].basicPropertyData.photos.main.highResUrl.relativeUrl  → webp photo
  results[].displayName.text
  results[].priceDisplayInfoIrene.displayPrice.amountPerStay.amountUnformatted  → per-stay total
  results[].blocks[0].finalPrice.amount  → same total (backup)
  results[].policies.showFreeCancellation
  Photo CDN: https://cf.bstatic.com + relativeUrl  (includes ?k=HASH)
"""
import logging
import re
import time
import base64
import secrets
import json as _json

from app.config import settings

logger = logging.getLogger(__name__)

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    _CURL_AVAILABLE = True
except ImportError:
    _CURL_AVAILABLE = False
    CurlAsyncSession = None

_GRAPHQL_URL  = "https://www.booking.com/dml/graphql"
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
    "apollographql-client-version": "aYSHeUCR",
    "x-booking-context-action-name": "searchresults_irene",
    "x-booking-context-aid": settings.booking_affiliate_id,
    "x-booking-site-type-id": "1",
    "x-booking-topic": "capla_browser_b-search-web-searchresults",
}
_AUTOCOMPLETE_GQL_HEADERS = {
    **_COMMON_HEADERS,
    "content-type": "application/json",
    "apollographql-client-name": "b-index-lp-web-mfe",
    "apollographql-client-version": "PDRKZEQM",
    "x-booking-context-action": "index",
    "x-booking-context-action-name": "index",
    "x-booking-context-aid": settings.booking_affiliate_id,
    "x-booking-site-type-id": "1",
    "x-booking-topic": "capla_browser_b-index-lp-web-mfe",
    "referer": "https://www.booking.com/",
}

_AUTOCOMPLETE_GQL_QUERY = """query AutoComplete($input: AutoCompleteRequestInput!) {
  autoCompleteSuggestions(input: $input) {
    results {
      destination { destId destType latitude longitude countryCode __typename }
      displayInfo { title label __typename }
      __typename
    }
    __typename
  }
}"""

# Known Indian destinations — (destId, destType)
_DEST_CACHE: dict[str, tuple[int, str]] = {
    "gurgaon":   (-2096897, "CITY"),
    "gurugram":  (-2096897, "CITY"),
    "goa":       (4127,     "REGION"),
    "north goa": (5052,     "REGION"),
    "south goa": (5303,     "REGION"),
}

# Booking.com's `search` field returns SearchQueryOutput — inline fragment required.
# Price comes from priceDisplayInfoIrene.displayPrice.amountPerStay.amountUnformatted.
# Photos from basicPropertyData.photos.main.highResUrl.relativeUrl (webp, includes ?k= hash).
_GQL_QUERY = """query FullSearch($input: SearchQueryInput!) {
  searchQueries {
    search(input: $input) {
      ... on SearchQueryOutput {
        results {
          basicPropertyData {
            id
            pageName
            location { address city latitude longitude }
            photos {
              main {
                highResUrl { relativeUrl }
                highResJpegUrl { relativeUrl }
              }
            }
            reviewScore: reviews {
              score: totalScore
              reviewCount: reviewsCount
              totalScoreTextTag { translation }
            }
            starRating { value }
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
          soldOutInfo { isSoldOut }
        }
      }
    }
  }
}
"""


async def resolve_dest_id(city_name: str) -> tuple[int | None, str, str]:
    """
    Resolve a city name to a Booking.com destId + destType via the autocomplete API.
    Returns (destId, canonical_name, destType). destId is None if resolution fails.
    destType is "CITY", "REGION", or "DISTRICT".
    """
    key = city_name.lower().strip()

    if key in _DEST_CACHE:
        dest_id, dest_type = _DEST_CACHE[key]
        return dest_id, city_name, dest_type
    for k, (did, dtype) in _DEST_CACHE.items():
        if k in key or key in k:
            return did, city_name, dtype

    try:
        pageview_id = secrets.token_hex(8)
        payload = {
            "operationName": "AutoComplete",
            "variables": {
                "input": {
                    "prefixQuery": city_name,
                    "nbSuggestions": 8,
                    "requestConfig": {"enableRequestContextBoost": True},
                    "requestContext": {"pageviewId": pageview_id, "location": None},
                    "fallbackConfig": {
                        "mergeResults": True,
                        "nbMaxMergedResults": 6,
                        "nbMaxThirdPartyResults": 3,
                        "sources": ["GOOGLE", "HERE"],
                    },
                }
            },
            "extensions": {},
            "query": _AUTOCOMPLETE_GQL_QUERY,
        }
        async with CurlAsyncSession() as s:
            r = await s.post(
                f"{_GRAPHQL_URL}?lang=en-us",
                headers={**_AUTOCOMPLETE_GQL_HEADERS, "x-booking-pageview-id": pageview_id},
                json=payload,
                impersonate="chrome",
                timeout=10,
            )
        r.raise_for_status()
        results = (
            (r.json().get("data") or {})
            .get("autoCompleteSuggestions", {})
            .get("results") or []
        )
        # Two passes: first prefer India, then accept any country
        for prefer_india in (True, False):
            for priority in ("CITY", "REGION", "DISTRICT"):
                for item in results:
                    dest = item.get("destination") or {}
                    if dest.get("destType") != priority or not dest.get("destId"):
                        continue
                    country = (dest.get("countryCode") or "").lower()
                    if prefer_india and country not in ("in", "ind"):
                        continue
                    dest_id = int(dest["destId"])
                    dest_type = dest["destType"]
                    label = (item.get("displayInfo") or {}).get("title") or city_name
                    _DEST_CACHE[key] = (dest_id, dest_type)
                    logger.debug(f"[booking_hotels] resolved '{city_name}' → destId={dest_id} ({dest_type}: {label})")
                    return dest_id, label, dest_type
        if results:
            dest = (results[0].get("destination") or {})
            dest_id_str = dest.get("destId")
            if dest_id_str:
                dest_id = int(dest_id_str)
                dest_type = dest.get("destType", "CITY")
                label = (results[0].get("displayInfo") or {}).get("title") or city_name
                _DEST_CACHE[key] = (dest_id, dest_type)
                return dest_id, label, dest_type
    except Exception as e:
        logger.debug(f"[booking_hotels] AutoComplete '{city_name}': {e}")

    return None, city_name, "CITY"


async def _run_fullsearch(
    dest_id: int,
    dest_type: str,
    city_canonical: str,
    checkin: str,
    checkout: str,
    rooms: int,
    adults: int,
    children: int,
    sort: str | None,
    limit: int,
    offset: int,
    nights: int,
) -> list[dict]:
    """Execute a single Booking.com FullSearch GraphQL query. Returns normalised list."""
    dest_type_lower = dest_type.lower()
    pageview_id = secrets.token_hex(8)

    # Booking.com caps meaningful results around 25 per page; sending more causes empty responses.
    rows = min(limit, 25)

    gql_url = (
        f"{_GRAPHQL_URL}"
        f"?ss={city_canonical.replace(' ', '+')}"
        f"&dest_id={dest_id}&dest_type={dest_type_lower}"
        f"&checkin={checkin}&checkout={checkout}"
        f"&group_adults={adults}&no_rooms={rooms}&group_children={children}"
    )
    referer = (
        f"{_SEARCH_PAGE}"
        f"?ss={city_canonical.replace(' ', '+')}"
        f"&dest_id={dest_id}&dest_type={dest_type_lower}"
        f"&checkin={checkin}&checkout={checkout}"
        f"&group_adults={adults}&no_rooms={rooms}&group_children={children}"
    )
    raw_query = (
        f"/searchresults.html?ss={city_canonical}&dest_id={dest_id}&dest_type={dest_type_lower}"
        f"&checkin={checkin}&checkout={checkout}"
        f"&group_adults={adults}&no_rooms={rooms}&group_children={children}"
        f"&lang=en-us&aid=304142"
    )
    gql_headers = {
        **_GQL_HEADERS,
        "x-booking-pageview-id": pageview_id,
        "referer": referer,
    }
    payload = {
        "operationName": "FullSearch",
        "variables": {
            "input": {
                "acidCarouselContext": None,
                "childrenAges": [],
                "dates": {"checkin": checkin, "checkout": checkout},
                "doAvailabilityCheck": False,
                "encodedAutocompleteMeta": None,
                "enableCampaigns": True,
                "filters": {},
                "flexibleDatesConfig": {
                    "broadDatesCalendar": {"checkinMonths": [], "los": [], "startWeekdays": []},
                    "dateFlexUseCase": "DATE_RANGE",
                    "dateRangeCalendar": {"checkin": [checkin], "checkout": [checkout]},
                },
                "forcedBlocks": None,
                "location": {
                    "searchString": city_canonical,
                    "destType": dest_type,
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
                "optionalFeatures": {"forceArpExperiments": True, "testProperties": False},
                "pagination": {"rowsPerPage": rows, "offset": offset},
                "rawQueryForSession": raw_query,
                "referrerBlock": {"blockName": "searchbox"},
                "sbCalendarOpen": False,
                "sorters": {
                    "selectedSorter": sort,
                    "referenceGeoId": None,
                    "tripTypeIntentId": None,
                },
                "travelPurpose": 2,
                "seoThemeIds": [],
                "useSearchParamsFromSession": True,
                "webSearchContext": {
                    "reason": "CLIENT_SIDE_UPDATE",
                    "source": "SEARCH_RESULTS",
                    "outcome": "SEARCH_RESULTS",
                },
                "clientSideRequestId": pageview_id,
            }
        },
        "extensions": {},
        "query": _GQL_QUERY,
    }

    try:
        async with CurlAsyncSession() as s:
            resp = await s.post(
                gql_url,
                headers=gql_headers,
                json=payload,
                impersonate="chrome",
                timeout=25,
            )

        if resp.status_code != 200:
            logger.info(f"[booking_hotels] HTTP {resp.status_code} for '{city_canonical}' (destType={dest_type})")
            return []

        data = resp.json()
        errors = data.get("errors") or []
        if errors:
            msgs = [e.get("message", "") for e in errors[:2]]
            logger.info(f"[booking_hotels] GraphQL errors for '{city_canonical}' ({dest_type}): {msgs}")

        results = (
            (data.get("data") or {})
            .get("searchQueries", {})
            .get("search", {})
            .get("results") or []
        )

        return [_normalise(r, checkin, checkout, nights) for r in results]

    except Exception as e:
        logger.debug(f"[booking_hotels] _run_fullsearch '{city_canonical}' ({dest_type}): {e}")
        return []


async def search_hotels(
    city_name: str,
    checkin: str,
    checkout: str,
    rooms: int = 1,
    adults: int = 2,
    children: int = 0,
    sort: str | None = None,
    limit: int = 10,
    offset: int = 0,
) -> list[dict]:
    """
    Search hotels on Booking.com. Returns a normalized list.
    Tries CITY destType first; retries as REGION then DISTRICT when results are empty.
    Requires curl_cffi (bypasses AWS WAF with Chrome impersonation).
    """
    if not _CURL_AVAILABLE:
        logger.warning("[booking_hotels] curl_cffi not installed — pip install curl-cffi")
        return []

    dest_id, city_canonical, dest_type = await resolve_dest_id(city_name)
    if dest_id is None:
        for variant in [
            f"{city_name} India",
            f"{city_name} Himachal Pradesh",
            f"{city_name} Uttarakhand",
            f"{city_name} Rajasthan",
            f"{city_name} Goa",
            f"{city_name} Kerala",
        ]:
            dest_id, city_canonical, dest_type = await resolve_dest_id(variant)
            if dest_id is not None:
                logger.info(f"[booking_hotels] resolved '{city_name}' via variant '{variant}'")
                break
    if dest_id is None:
        logger.info(f"[booking_hotels] could not resolve destId for '{city_name}' — skipping")
        return []

    from datetime import datetime as _dt
    try:
        nights = (_dt.strptime(checkout, "%Y-%m-%d") - _dt.strptime(checkin, "%Y-%m-%d")).days
    except Exception:
        nights = 1

    # First attempt with the resolved destType
    hotels = await _run_fullsearch(
        dest_id, dest_type, city_canonical,
        checkin, checkout, rooms, adults, children,
        sort, limit, offset, nights,
    )

    # If empty, retry with the other destTypes — some cities are indexed differently
    if not hotels:
        for fallback_type in ("CITY", "REGION", "DISTRICT"):
            if fallback_type == dest_type:
                continue
            logger.info(
                f"[booking_hotels] '{city_name}': 0 results as {dest_type}, "
                f"retrying as {fallback_type}"
            )
            hotels = await _run_fullsearch(
                dest_id, fallback_type, city_canonical,
                checkin, checkout, rooms, adults, children,
                sort, limit, offset, nights,
            )
            if hotels:
                logger.info(
                    f"[booking_hotels] '{city_name}': ✓ {len(hotels)} results "
                    f"as {fallback_type} (was {dest_type})"
                )
                break

    logger.info(f"[booking_hotels] '{city_name}' ({dest_type}): {'✓ ' + str(len(hotels)) if hotels else '✗ 0'} results")
    return hotels


def _normalise(r: dict, checkin: str, checkout: str, nights: int) -> dict:
    basic = r.get("basicPropertyData") or {}
    loc   = basic.get("location") or {}
    reviews = basic.get("reviewScore") or {}
    star = basic.get("starRating") or {}

    name      = (r.get("displayName") or {}).get("text") or "Unknown"
    hotel_id  = basic.get("id")
    page_name = basic.get("pageName") or ""
    booking_url = f"https://www.booking.com/hotel/in/{page_name}.html" if page_name else None

    # Photos: highResUrl (webp, includes ?k= hash) > highResJpegUrl fallback
    main_photo = basic.get("photos", {}).get("main") or {}
    photo_rel = (
        (main_photo.get("highResUrl") or main_photo.get("highResJpegUrl") or {})
        .get("relativeUrl", "")
    )
    photos = [_PHOTO_BASE + photo_rel] if photo_rel else []

    # Price: displayPrice.amountPerStay is the total for the stay
    price_info  = r.get("priceDisplayInfoIrene") or {}
    per_stay_raw = (
        (price_info.get("displayPrice") or {})
        .get("amountPerStay", {})
        .get("amountUnformatted")
    )
    avg_night_raw = (price_info.get("averagePricePerNight") or {}).get("amountUnformatted")
    original_raw  = (
        (price_info.get("priceBeforeDiscount") or {})
        .get("amountPerStay", {})
        .get("amountUnformatted")
    )
    currency = (price_info.get("averagePricePerNight") or {}).get("currency") or "INR"

    # Block price as backup
    blocks     = r.get("blocks") or []
    block0     = blocks[0] if blocks else {}
    block_price = _safe_int((block0.get("finalPrice") or {}).get("amount"))
    free_cancel_until = block0.get("freeCancellationUntil")

    per_stay   = _safe_int(per_stay_raw)
    avg_night  = _safe_int(avg_night_raw) or (per_stay // nights if per_stay and nights else None) or block_price
    original   = _safe_int(original_raw)

    policies   = r.get("policies") or {}
    free_cancel = policies.get("showFreeCancellation", False) or bool(free_cancel_until)
    dist       = r.get("location") or {}
    sold_out   = (r.get("soldOutInfo") or {}).get("isSoldOut", False)
    meal       = (r.get("mealPlanIncluded") or {}).get("text")

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
        "price_per_night": avg_night,
        "price_per_stay": per_stay,
        "price_original_stay": original,
        "currency": currency,
        "nights": nights,
        "checkin": checkin,
        "checkout": checkout,
        "free_cancellation": free_cancel,
        "free_cancellation_until": free_cancel_until,
        "no_prepayment": policies.get("showNoPrepayment", False),
        "meal_plan": meal,
        "photos": photos,
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
