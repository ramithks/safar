"""
Indian Railway search via ConfirmTKT API.
India-only — only applies when both origin and destination are Indian cities.

Station resolution: ConfirmTKT v2 auto-suggestion API only.
No hardcoded values — cache populated on first lookup.

Matching strategy for auto-suggestion results:
  1. Exact city match (station.city == query)
  2. majorStn: true (the "All stations" aggregate entry for a city)
  3. Partial city/name match
  Highest score wins.
"""
import logging
from datetime import datetime
import httpx

from app.config import settings
from app.services.scraper_proxy import with_proxy_fallback, ScraperBlocked

logger = logging.getLogger(__name__)

_BASE = "https://cttrainsapi.confirmtkt.com"


def _build_headers() -> dict:
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "ApiKey": settings.confirmtkt_api_key,
        "CT-Token": "",
        "CT-Userkey": "",
        "ClientId": "ct-web",
        "Content-Type": "application/json",
        "DeviceId": settings.confirmtkt_device_id,
        "Origin": "https://www.confirmtkt.com",
        "Referer": "https://www.confirmtkt.com/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
    }

# In-memory cache populated by API calls only — no hardcoded values
_STATION_CACHE: dict[str, str | None] = {}


def _pick_best_station(stations: list[dict], query: str) -> str | None:
    """
    Score each candidate station and return the best matching code.

    Scoring:
      +2.0  city exactly matches query (e.g. "Goa" for query "goa")
      +1.0  city contains query or query contains city
      +0.3  majorStn: true (the "All stations" aggregate entry)
      +0.2  stationName contains query
    Returns None if best score is 0 (no relevant match).
    """
    q = query.lower().strip()
    best_code  = None
    best_score = 0.0

    for stn in stations:
        city   = (stn.get("city")        or "").lower().strip()
        name   = (stn.get("stationName") or "").lower().strip()
        code   = stn.get("stationCode")  or ""
        major  = bool(stn.get("majorStn"))

        score = 0.0
        if city == q:
            score = 2.0
        elif q in city or city in q:
            score = 1.0
        elif q in name:
            score = 0.2

        if major:
            score += 0.3

        if score > best_score:
            best_score = score
            best_code  = code

    return best_code if best_score > 0 else None


async def resolve_station_async(city_name: str) -> str | None:
    """
    Resolve city name → Indian Railway station code via ConfirmTKT v2 autosuggest.
    Returns None if no matching station found (city has no rail connectivity).
    Results cached in-memory after first call.
    """
    key = city_name.lower().strip()
    if key in _STATION_CACHE:
        return _STATION_CACHE[key]

    async def _do(proxy):
        async with httpx.AsyncClient(timeout=8.0, proxy=proxy) as client:
            r = await client.get(
                f"{_BASE}/api/v2/trains/stations/auto-suggestion",
                headers=_build_headers(),
                params={
                    "searchString": city_name,
                    "sourceStnCode": "",
                    "popularStnListLimit": 15,
                    "preferredStnListLimit": 6,
                    "channel": "mwebd",
                    "language": "EN",
                },
            )
        if r.status_code != 200:
            raise ScraperBlocked(f"HTTP {r.status_code}")
        return r.json()

    try:
        data = await with_proxy_fallback("railway", _do)
        stations = (data.get("data") or {}).get("stationList") or []
        code = _pick_best_station(stations, city_name)
        _STATION_CACHE[key] = code
        if code:
            logger.info("[railway] autocomplete '%s' → %s", city_name, code)
        else:
            logger.info("[railway] autocomplete '%s' → no match (no rail connectivity)", city_name)
        return code
    except Exception as e:
        logger.warning("[railway] station autocomplete '%s': %s", city_name, e)

    _STATION_CACHE[key] = None
    return None


# Kept for callers that need a sync lookup — returns from cache only
def city_to_station_code(city_name: str) -> str | None:
    """Returns cached station code or None. Use resolve_station_async for fresh lookup."""
    return _STATION_CACHE.get(city_name.lower().strip())


async def search_trains(
    source_code: str,
    dest_code: str,
    date: str,           # "YYYY-MM-DD" or "DD-MM-YYYY"
) -> list[dict]:
    """
    Search trains between two station codes via ConfirmTKT.
    Returns a normalized list of train options.
    """
    try:
        parts = date.split("-")
        if len(parts) == 3 and len(parts[0]) == 4:
            doj = datetime.strptime(date, "%Y-%m-%d").strftime("%d-%m-%Y")
        else:
            doj = date
    except Exception:
        doj = date

    async def _do(proxy):
        async with httpx.AsyncClient(timeout=20.0, proxy=proxy) as client:
            r = await client.get(
                f"{_BASE}/api/v1/trains/search",
                headers=_build_headers(),
                params={
                    "sourceStationCode": source_code,
                    "destinationStationCode": dest_code,
                    "addAvailabilityCache": "true",
                    "excludeMultiTicketAlternates": "false",
                    "excludeBoostAlternates": "false",
                    "sortBy": "DEFAULT",
                    "dateOfJourney": doj,
                    "enableNearby": "true",
                    "enableTG": "true",
                    "tGPlan": "CTG-A42",
                    "showTGPrediction": "false",
                    "tgColor": "DEFAULT",
                    "showPredictionGlobal": "true",
                    "showNewAlternates": "false",
                    "showNewAltText": "true",
                },
            )
        r.raise_for_status()
        return r.json()

    try:
        data = await with_proxy_fallback("railway", _do)
        # New API: response is {"data": {"trainList": [...]}}
        # Old API: response is {"trainsList": [...]} or {"trains": [...]}
        trains_raw = (
            (data.get("data") or {}).get("trainList")
            or data.get("trainsList")
            or data.get("trains")
            or data.get("TrainList")
            or []
        )
        return [_normalise_train(t, doj) for t in trains_raw]
    except Exception as e:
        logger.warning("[railway] search %s→%s on %s: %s", source_code, dest_code, doj, e)
        return []


def _normalise_train(t: dict, doj: str = "") -> dict:
    """
    Normalize a ConfirmTKT train entry.
    doj: journey date in "DD-MM-YYYY" format (used for booking URL).

    Each class entry in available_classes includes:
      fare, availability, prediction (ConfirmTKT seat prediction),
      prediction_status ("Confirm" / "RAC" / "WL"), prediction_pct (0-100).
    """
    avail_cache = t.get("availabilityCache") or {}
    avl_classes = t.get("avlClasses") or t.get("avlClassesSorted") or []

    cheapest_fare:  int | None = None
    cheapest_class: str | None = None
    available_classes = []

    for cls_code in avl_classes:
        ce = avail_cache.get(cls_code) or {}
        fare_str     = ce.get("fare")
        display      = ce.get("availabilityDisplayName") or ""   # e.g. "AVL 4", "WL 12"
        availability = ce.get("availability") or display          # "AVAILABLE-0004"
        prediction   = ce.get("prediction") or ce.get("predictionDisplayName") or ""
        pred_status  = ce.get("confirmTktStatus") or ""           # "Confirm" / "RAC" / "WL"
        pred_pct     = ce.get("predictionPercentage")             # 0-100

        try:
            fare = int(float(fare_str)) if fare_str else None
        except Exception:
            fare = None

        seats = None
        if "AVL" in display:
            try:
                seats = int(display.split()[-1])
            except Exception:
                pass

        available_classes.append({
            "code":              cls_code,
            "fare":              fare,
            "availability":      availability,
            "display":           display,
            "available_seats":   seats,
            "prediction":        prediction,
            "prediction_status": pred_status,
            "prediction_pct":    pred_pct,
        })
        if fare and (cheapest_fare is None or fare < cheapest_fare):
            cheapest_fare  = fare
            cheapest_class = cls_code

    # Fallback: old classesWithFare format
    if not available_classes:
        for c in (t.get("classesWithFare") or t.get("availableClasses") or []):
            code = c.get("classCode") or c.get("code")
            fare = c.get("fare") or c.get("totalFare")
            try:
                fare = int(fare) if fare else None
            except Exception:
                fare = None
            available_classes.append({
                "code": code, "fare": fare, "availability": None,
                "display": None, "available_seats": None,
                "prediction": None, "prediction_status": None, "prediction_pct": None,
            })
            if fare and (cheapest_fare is None or fare < cheapest_fare):
                cheapest_fare  = fare
                cheapest_class = code

    _dur_raw = t.get("duration") or t.get("journeyDuration") or ""
    duration_str = str(_dur_raw)
    duration_min = 0
    if isinstance(_dur_raw, int):
        duration_min = _dur_raw
    elif ":" in duration_str:
        try:
            parts = duration_str.split(":")
            duration_min = int(parts[0]) * 60 + int(parts[1])
        except Exception:
            pass

    from_code = t.get("fromStnCode") or t.get("sourceStationCode") or t.get("srcStnCode")
    to_code   = t.get("toStnCode")   or t.get("destinationStationCode") or t.get("dstStnCode")
    dep_time  = t.get("departureTime") or t.get("departure")
    arr_time  = t.get("arrivalTime")   or t.get("arrival")

    # ConfirmTKT booking URL: /rbooking/trains/from/{FROM}/to/{TO}/{DD-MM-YYYY}
    booking_url = None
    if from_code and to_code and doj:
        booking_url = (
            f"https://www.confirmtkt.com/rbooking/trains/"
            f"from/{from_code}/to/{to_code}/{doj}"
        )

    return {
        "train_number":    t.get("trainNumber") or t.get("trainNo"),
        "train_name":      t.get("trainName")   or t.get("name"),
        "train_type":      t.get("trainType")   or t.get("type"),
        "from_station_code": from_code,
        "from_station_name": t.get("fromStnName") or t.get("sourceStationName"),
        "to_station_code":   to_code,
        "to_station_name":   t.get("toStnName") or t.get("destinationStationName"),
        "departure_time": dep_time,
        "arrival_time":   arr_time,
        "duration":       duration_str,
        "duration_min":   duration_min,
        "distance_km":    t.get("distance") or t.get("distanceKm"),
        "cheapest_fare":  cheapest_fare,
        "cheapest_class": cheapest_class,
        "available_classes": available_classes,
        "running_days":   t.get("runningDays") or t.get("daysOfRun") or [],
        "pantry":         t.get("hasPantry") or t.get("pantryAvailable", False),
        "train_rating":   t.get("trainRating"),
        "booking_url":    booking_url,
        "source": "confirmtkt",
    }


class RailwayService:
    """Indian Railway search via ConfirmTKT — India-only domestic routes."""

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        from_code: str | None = None,
        to_code: str | None = None,
    ) -> dict:
        """
        Search trains between two cities. Caller can provide station codes directly
        (from Gemini) to avoid the city→code lookup.
        Returns {"trains": [...], "from_code": str, "to_code": str, "resolved": bool}
        """
        fc = from_code or city_to_station_code(origin)
        tc = to_code or city_to_station_code(destination)

        if not fc or not to_code and not tc:
            return {
                "trains": [], "from_code": fc, "to_code": tc,
                "resolved": False,
                "note": "No railway station found — this area may not have rail connectivity",
            }

        trains = await search_trains(fc, tc or "", date)
        return {
            "trains": trains,
            "from_code": fc,
            "to_code": tc,
            "resolved": True,
        }
