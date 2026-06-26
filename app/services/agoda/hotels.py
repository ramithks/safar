"""
Agoda Hotels API integration.
POST https://www.agoda.com/graphql/search  (citySearch operation)

Two-step flow:
  1. Resolve city name → Agoda numeric cityId.
  2. POST the citySearch GraphQL query with cityId + dates.

The GraphQL query here is a trimmed subset of the verified browser query —
field paths are taken verbatim from it, so the response shape is known. The
variables mirror the verified request with the dynamic bits parameterised.

STATUS: city resolution is seeded statically (Manali=9028 from the verified curl);
the autocomplete endpoint still needs a verified sample to cover all cities
(see _SEED / resolve_city_id). Everything fails closed — an unresolved city or a
blocked/unexpected response returns [] and the aggregator uses its other sources.
"""
import logging
import uuid
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession as CurlAsyncSession

from app.services.scraper_proxy import with_proxy_fallback, ScraperBlocked

logger = logging.getLogger(__name__)

_BASE = "https://www.agoda.com/graphql/search"
_SUGGEST = "https://www.agoda.com/api/cronos/search/GetUnifiedSuggestResult/3/15/1/0/en-in/"
_SITE = "https://www.agoda.com"
_CID = "1844104"

# Native sort codes — canonical sort key → Agoda (sortField, sortOrder).
# Verified valid sortFields: Price, Ranking, StarRating (others 400).
_SORTS = {
    "price_asc":   ("Price", "Asc"),
    "price_desc":  ("Price", "Desc"),
    "recommended": ("Ranking", "Desc"),
    "rating":      ("StarRating", "Desc"),
}
# Persona → Agoda travellerType string.
_TRAVELLER = {"solo": "Solo", "couple": "Couple", "friends": "Group", "family": "Family"}
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Trimmed citySearch query — selects only the fields we normalise. Paths verbatim
# from the verified full query, so it is schema-valid.
_QUERY = """query citySearch($CitySearchRequest: CitySearchRequest!, $ContentSummaryRequest: ContentSummaryRequest!, $PricingSummaryRequest: PricingRequestParameters) {
  citySearch(CitySearchRequest: $CitySearchRequest) {
    properties(ContentSummaryRequest: $ContentSummaryRequest, PricingSummaryRequest: $PricingSummaryRequest) {
      propertyId
      content {
        informationSummary {
          displayName
          rating
          propertyLinks { propertyPage }
          geoInfo { latitude longitude }
          address { countryCode country { id name } city { id name } area { id name } }
        }
        reviews { cumulative { reviewCount score } }
        images { hotelImages { urls { value } } }
      }
      pricing {
        isAvailable
        offers {
          roomOffers {
            room {
              pricing {
                currency
                price {
                  perRoomPerNight { exclusive { display } inclusive { display } }
                  perBook { exclusive { display } inclusive { display } }
                }
              }
            }
          }
        }
      }
    }
    searchResult {
      searchInfo {
        objectInfo { cityName cityId countryId countryEnglishName }
      }
    }
  }
}"""


def _headers(city_id: int) -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "ag-cid": _CID,
        "ag-correlation-id": str(uuid.uuid4()),
        "ag-debug-override-origin": "IN",
        "ag-language-locale": "en-in",
        "ag-page-type-id": "103",
        "ag-request-id": str(uuid.uuid4()),
        "content-type": "application/json",
        "origin": _SITE,
        "referer": f"{_SITE}/en-in/search?city={city_id}",
        "user-agent": _UA,
    }


# Seed resolved from the verified curl (Manali). key = normalized city name.
_SEED: dict[str, int] = {"manali": 9028}
_CITY_CACHE: dict[str, int] = dict(_SEED)


def _suggest_headers() -> dict:
    return {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9,hi;q=0.8",
        "ag-correlation-id": str(uuid.uuid4()),
        "ag-language-id": "15",
        "ag-language-locale": "en-in",
        "ag-request-attempt": "1",
        "content-type": "application/json; charset=utf-8",
        "cr-currency-code": "INR",
        "cr-currency-id": "27",
        "origin": _SITE,
        "referer": f"{_SITE}/en-in/?cid={_CID}",
        "user-agent": _UA,
        "x-requested-with": "XMLHttpRequest",
    }


async def resolve_city_id(city_name: str) -> int | None:
    """
    Resolve a city name → Agoda numeric cityId via the cronos unified-suggest API.
    Static seed first, then the live autocomplete (cached). Returns None if no
    city-type match is found.
    """
    key = city_name.lower().strip()
    if key in _CITY_CACHE:
        return _CITY_CACHE[key]

    params = {
        "searchText": city_name,
        "guid": str(uuid.uuid4()),
        "origin": "IN",
        "cid": _CID,
        "pageTypeId": "1",
        "logtime": datetime.now().strftime("%a %b %d %Y %H:%M:%S GMT+0530"),
        "logTypeId": "1",
        "isHotelLandSearch": "true",
    }

    async def _do(proxy):
        async with CurlAsyncSession(proxy=proxy) as s:
            r = await s.get(_SUGGEST, headers=_suggest_headers(), params=params,
                            impersonate="chrome", timeout=10)
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        return r.json()

    try:
        data = await with_proxy_fallback("agoda_suggest", _do)
        city_id = _pick_city_id(data, city_name)
        _CITY_CACHE[key] = city_id
        if city_id is not None:
            logger.info("[agoda] resolved %r → cityId %s", city_name, city_id)
        else:
            logger.info("[agoda] no cityId match for %r", city_name)
        return city_id
    except Exception as e:
        logger.warning("[agoda] suggest %r: %s", city_name, e)
        return None


def _pick_city_id(data, query: str) -> int | None:
    """
    Extract a city-type cityId from the unified-suggest payload. Defensive across
    the response's likely shapes/keys; prefers an exact name match and a
    city-type result (ObjectTypeId == 2) over hotels/areas.
    """
    items = data
    if isinstance(data, dict):
        for k in ("ViewModelList", "SuggestItems", "Suggestions", "suggestions",
                  "Results", "results", "data"):
            v = data.get(k)
            if isinstance(v, list):
                items = v
                break
    if not isinstance(items, list):
        return None

    q = query.lower().strip()
    best_id: int | None = None
    best_score = -1.0
    for it in items:
        if not isinstance(it, dict):
            continue
        # In the cronos payload the real city row is ObjectTypeId==1 with a
        # non-zero CityId; the first "echo" row has CityId==0. Prefer CityId.
        raw_id = it.get("CityId") or it.get("cityId") or it.get("ObjectId") or it.get("id")
        try:
            cid = int(raw_id)
        except (TypeError, ValueError):
            continue
        name = str(it.get("Name") or it.get("CityName") or it.get("ObjectName")
                   or it.get("name") or "").lower()
        otype = it.get("ObjectTypeId") or it.get("objectTypeId") or it.get("type")
        n_hotels = it.get("NoOfHotels") or 0
        score = 0.0
        if name == q:
            score += 3.0
        elif q in name or name.startswith(q):
            score += 1.5
        if otype in (1, "1", "city", "City"):      # ObjectTypeId 1 == city
            score += 2.0
        if it.get("CityId"):                        # non-zero CityId = real city row
            score += 1.0
        if isinstance(n_hotels, (int, float)) and n_hotels > 0:
            score += 0.5
        if score > best_score:
            best_score = score
            best_id = cid
    return best_id if best_score > 0 else None


def _iso_z(d: str) -> str:
    """'YYYY-MM-DD' → 'YYYY-MM-DDT00:00:00.000Z'."""
    return f"{d}T00:00:00.000Z"


def _build_variables(city_id: int, checkin: str, checkout: str, los: int,
                     rooms: int, adults: int, children: int, page_size: int,
                     sort: str | None = None, traveller_type: str = "Couple") -> dict:
    sort_field, sort_order = _SORTS.get(sort, ("Ranking", "Desc"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    user_id = str(uuid.uuid4())
    search_id = str(uuid.uuid4())
    occupancy = {"numberOfAdults": adults, "numberOfChildren": children,
                 "travelerType": 2, "checkIn": _iso_z(checkin)}
    return {
        "CitySearchRequest": {
            "cityId": city_id,
            "searchRequest": {
                "searchCriteria": {
                    "isAllowBookOnRequest": True,
                    "bookingDate": now,
                    "checkInDate": _iso_z(checkin),
                    "localCheckInDate": checkin,
                    "los": los,
                    "rooms": rooms,
                    "adults": adults,
                    "children": children,
                    "childAges": [],
                    "isUserLoggedIn": False,
                    "currency": "INR",
                    "travellerType": traveller_type,
                    "sorting": {"sortField": sort_field, "sortOrder": sort_order, "sortParams": None},
                    "requiredBasis": "PRPN",
                    "requiredPrice": "Exclusive",
                    "synchronous": False,
                },
                "searchContext": {
                    "userId": user_id, "memberId": 0, "locale": "en-in", "cid": int(_CID),
                    "origin": "IN", "platform": 1, "deviceTypeId": 1, "storeFrontId": 3,
                    "pageTypeId": 103, "endpointSearchType": "CitySearch", "searchId": search_id,
                },
                "filterRequest": {"idsFilters": [], "rangeFilters": [], "textFilters": []},
                "page": {"pageSize": page_size, "pageNumber": 1, "pageToken": ""},
            },
        },
        "ContentSummaryRequest": {
            "context": {
                "rawUserId": user_id, "memberId": 0, "userOrigin": "IN", "locale": "en-in",
                "forceExperimentsByIdNew": [], "apo": False,
                "searchCriteria": {"cityId": city_id}, "platform": {"id": 1},
                "storeFrontId": 3, "cid": _CID, "occupancy": occupancy, "deviceTypeId": 1,
                "whiteLabelKey": "", "correlationId": "",
            },
            "summary": {"highlightedFeaturesOrderPriority": None, "includeHotelCharacter": True},
            "reviews": {
                "commentary": None,
                "demographics": {"providerIds": None, "filter": {"defaultProviderOnly": True}},
                "summaries": {"providerIds": None, "apo": True, "limit": 1, "travellerType": 2},
                "cumulative": {"providerIds": None}, "filters": None,
            },
            "images": {"page": None, "maxWidth": 0, "maxHeight": 0, "imageSizes": None, "indexOffset": None},
            "rooms": {
                "images": None, "featureLimit": 0, "filterCriteria": None, "includeMissing": False,
                "includeSoldOut": False, "includeDmcRoomId": False, "soldOutRoomCriteria": None,
                "showRoomSize": True, "showRoomFacilities": True, "showRoomName": False,
            },
            "nonHotelAccommodation": True, "engagement": True,
            "highlights": {"maxNumberOfItems": 0,
                           "images": {"imageSizes": [{"key": "full", "size": {"width": 0, "height": 0}}]}},
            "personalizedInformation": True, "localInformation": {"images": None}, "features": None,
            "rateCategories": True, "contentRateCategories": {"escapeRateCategories": {}}, "synopsis": True,
        },
        "PricingSummaryRequest": {
            "cheapestOnly": True,
            "context": {
                "isAllowBookOnRequest": True,
                "abTests": [{"testId": t, "abUser": "B"} for t in (9021, 9023, 9024, 9025, 9027, 9029)],
                "clientInfo": {
                    "cid": int(_CID), "languageId": 1, "languageUse": 1, "origin": "IN", "platform": 1,
                    "searchId": search_id, "storefront": 3, "userId": user_id, "ipAddress": "",
                    "attributionInfos": [{"modelId": m, "cid": int(_CID)} for m in (19, 20, 99)],
                },
                "experiment": [], "sessionInfo": {"isLogin": False, "memberId": 0, "sessionId": 1},
            },
            "isSSR": True,
            "pricing": {
                "bookingDate": now, "checkIn": _iso_z(checkin), "checkout": _iso_z(checkout),
                "localCheckInDate": checkin, "localCheckoutDate": checkout, "currency": "INR",
                "details": {"cheapestPriceOnly": False, "itemBreakdown": False, "priceBreakdown": False},
                "featureFlag": [
                    "ClientDiscount", "PriceHistory", "VipPlatinum", "RatePlanPromosCumulative",
                    "PromosCumulative", "MixAndSave", "APSPeek", "StackChannelDiscount", "AutoApplyPromos",
                    "EnableAgencySupplyForPackages", "EnableCashback", "CreditCardPromotionPeek",
                    "EnableCofundedCashback", "DispatchGoLocalForInternational", "EnableGoToTravelCampaign",
                    "EnableCashbackMildlyAggressiveDisplay", "EnablePriceTrend",
                ],
                "features": {
                    "calculateRareRoomBadge": False, "crossOutRate": False, "isAPSPeek": False,
                    "isAllOcc": False, "isApsEnabled": False, "isIncludeUsdAndLocalCurrency": False,
                    "isMSE": True, "isRPM2Included": True, "maxSuggestions": 0,
                    "isEnableSupplierFinancialInfo": False, "isLoggingAuctionData": False,
                    "newRateModel": False, "overrideOccupancy": False,
                    "filterCheapestRoomEscapesPackage": False, "priusId": 0, "synchronous": False,
                    "enableRichContentOffer": True, "showCouponAmountInUserCurrency": False,
                    "disableEscapesPackage": False, "enablePushDayUseRates": False,
                    "enableDayUseCor": False, "ignoreRoomsCountForNha": False,
                    "enableSuggestPriceExclusiveWithFees": True,
                },
                "filters": {"cheapestRoomFilters": [], "filterAPO": False, "ratePlans": [1],
                            "secretDealOnly": False, "suppliers": [], "nosOfBedrooms": []},
                "includedPriceInfo": False,
                "occupancy": {"adults": adults, "children": children, "childAges": [],
                              "rooms": rooms, "childrenTypes": []},
                "supplierPullMetadata": {"requiredPrecheckAccuracyLevel": 0},
                "mseHotelIds": [], "mseClicked": "", "ppLandingHotelIds": [], "searchedHotelIds": [],
                "paymentId": -1, "requiredBasis": "PRPN",
            },
            "suggestedPrice": "Exclusive",
        },
    }


async def search_hotels(
    city_name: str,
    checkin: str,      # "YYYY-MM-DD"
    checkout: str,     # "YYYY-MM-DD"
    rooms: int = 1,
    adults: int = 2,
    children: int = 0,
    limit: int = 40,
    sort: str | None = None,
    property_types: list[str] | None = None,   # accepted for API uniformity; classifier-only
    travel_type: str = "couple",
) -> list[dict]:
    """
    Search hotels on Agoda for a city + date range. `sort` is a canonical key
    (price_asc|price_desc|recommended|rating). `travel_type` sets Agoda's
    travellerType. Property type is enforced via the shared classifier. Returns
    a normalized list.
    """
    city_id = await resolve_city_id(city_name)
    if city_id is None:
        return []

    try:
        los = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days or 1
    except Exception:
        los = 1

    traveller = _TRAVELLER.get((travel_type or "couple").lower(), "Couple")
    payload = {
        "query": _QUERY,
        "variables": _build_variables(city_id, checkin, checkout, los, rooms, adults, children,
                                      max(limit, 20), sort=sort, traveller_type=traveller),
    }

    async def _do(proxy):
        async with CurlAsyncSession(proxy=proxy) as s:
            r = await s.post(_BASE, headers=_headers(city_id), json=payload,
                             impersonate="chrome", timeout=25)
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        j = r.json()
        if j.get("errors") and not j.get("data"):
            raise ScraperBlocked(f"graphql errors: {[e.get('message') for e in j['errors'][:2]]}")
        return j

    try:
        data = await with_proxy_fallback("agoda", _do)
        props = (
            (((data.get("data") or {}).get("citySearch") or {}).get("properties")) or []
        )
        return [_normalise(p, checkin, checkout, los) for p in props]
    except Exception as e:
        logger.warning("[agoda] hotel search %r %s→%s: %s", city_name, checkin, checkout, e)
        return []


def _normalise(p: dict, checkin: str, checkout: str, nights: int) -> dict:
    from app.services.hotels_filters import classify_property_type
    content = p.get("content") or {}
    info = content.get("informationSummary") or {}
    addr = info.get("address") or {}
    geo = info.get("geoInfo") or {}
    cum = ((content.get("reviews") or {}).get("cumulative")) or {}

    # cheapest exclusive per-room-per-night + per-book across offers/rooms
    prpn: list[float] = []
    per_book: list[float] = []
    currency = "INR"
    for off in (p.get("pricing") or {}).get("offers") or []:
        for ro in off.get("roomOffers") or []:
            pricings = (ro.get("room") or {}).get("pricing") or []
            if isinstance(pricings, dict):          # tolerate either shape
                pricings = [pricings]
            for pr in pricings:
                if not isinstance(pr, dict):
                    continue
                currency = pr.get("currency") or currency
                price = pr.get("price") or {}
                n = ((price.get("perRoomPerNight") or {}).get("exclusive") or {}).get("display")
                b = ((price.get("perBook") or {}).get("exclusive") or {}).get("display")
                if isinstance(n, (int, float)):
                    prpn.append(n)
                if isinstance(b, (int, float)):
                    per_book.append(b)

    photos = []
    for img in ((content.get("images") or {}).get("hotelImages")) or []:
        for u in img.get("urls") or []:
            val = u.get("value")
            if val:
                photos.append("https:" + val if val.startswith("//") else val)
    photos = photos[:6]

    page = (info.get("propertyLinks") or {}).get("propertyPage") or ""
    booking_url = page if page.startswith("http") else (f"{_SITE}{page}" if page else None)

    return {
        "hotel_id": str(p.get("propertyId")) if p.get("propertyId") is not None else None,
        "name": info.get("displayName"),
        "address": (addr.get("area") or {}).get("name") or (addr.get("city") or {}).get("name"),
        "city": (addr.get("city") or {}).get("name"),
        "lat": _to_float(geo.get("latitude")),
        "lon": _to_float(geo.get("longitude")),
        "star_rating": _to_float(info.get("rating")),
        "review_score": _to_float(cum.get("score")),
        "review_count": cum.get("reviewCount"),
        "price_per_night": int(min(prpn)) if prpn else None,
        "total_price": int(min(per_book)) if per_book else None,
        "currency": currency,
        "nights": nights,
        "photos": photos,
        "booking_url": booking_url,
        "property_type": classify_property_type({"name": info.get("displayName")}),
        "source": "agoda",
    }


def _to_float(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None
