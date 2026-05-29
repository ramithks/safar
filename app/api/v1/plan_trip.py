"""
POST /api/v1/plan-trip
POST /api/v1/plan-journey

plan-trip:  Full AI trip itinerary — discovers places, calls Gemini, hydrates with
            real data, fetches YouTube, optimises routes, saves to DB.

plan-journey: Door-to-door multi-leg journey planner.
              Phase 1 — Gemini plans the full route (multi-stop, station codes).
              Phase 2 — Live data fetched in parallel for each bus/train/flight leg.
              Includes visa + document guidance for international routes.
"""
import asyncio
import math
import random
from datetime import datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db
from app.services.vertex import vertex_generate, extract_json
from app.services.places import search_places, discover_places
from app.services.transport import TransportService
from app.services.hotels import HotelService
from app.services.flights import FlightService
from app.services.railway import RailwayService, city_to_station_code
from app.services.booking.flights import search_flights as booking_search_flights
from app.services.easemytrip.flights import search_flights as emt_search_flights
from app.config import settings

_transport = TransportService()
_hotels    = HotelService()
_flights   = FlightService()
_railway   = RailwayService()

router = APIRouter()
MODEL = "gemini-2.5-flash"

# ── Currency config ───────────────────────────────────────────────────────────
CURRENCY_CONFIG: dict[str, dict] = {
    "usd": {"symbol": "$",   "hotel": {"budget": 60,   "mid-range": 160,   "luxury": 450  }, "level": {0: 30,   1: 60,   2: 150,  3: 320,  4: 550 }, "range": {"low": (40,   90  ), "mid": (90,   220 ), "high": (220,  600 )}, "ceiling": {"low": 200,   "mid": 550,   "high": 1600 }},
    "eur": {"symbol": "€",   "hotel": {"budget": 55,   "mid-range": 140,   "luxury": 400  }, "level": {0: 25,   1: 55,   2: 130,  3: 280,  4: 480 }, "range": {"low": (35,   80  ), "mid": (80,   190 ), "high": (190,  520 )}, "ceiling": {"low": 180,   "mid": 480,   "high": 1400 }},
    "gbp": {"symbol": "£",   "hotel": {"budget": 50,   "mid-range": 120,   "luxury": 350  }, "level": {0: 22,   1: 50,   2: 115,  3: 250,  4: 420 }, "range": {"low": (30,   70  ), "mid": (70,   170 ), "high": (170,  450 )}, "ceiling": {"low": 160,   "mid": 420,   "high": 1200 }},
    "inr": {"symbol": "₹",   "hotel": {"budget": 2000, "mid-range": 5500,  "luxury": 16000}, "level": {0: 800,  1: 1800, 2: 4500, 3: 9000, 4: 18000}, "range": {"low": (1000, 2500), "mid": (2500, 7000), "high": (7000, 20000)}, "ceiling": {"low": 5000,  "mid": 15000, "high": 50000}},
    "jpy": {"symbol": "¥",   "hotel": {"budget": 7000, "mid-range": 20000, "luxury": 60000}, "level": {0: 3000, 1: 7000, 2: 18000, 3: 40000, 4: 75000}, "range": {"low": (4000, 9000), "mid": (9000, 28000), "high": (28000, 80000)}, "ceiling": {"low": 15000, "mid": 55000, "high": 180000}},
    "aud": {"symbol": "A$",  "hotel": {"budget": 90,   "mid-range": 220,   "luxury": 580  }, "level": {0: 45,   1: 90,   2: 200,  3: 430,  4: 720 }, "range": {"low": (60,   110 ), "mid": (110,  280 ), "high": (280,  650 )}, "ceiling": {"low": 280,   "mid": 720,   "high": 2100 }},
    "aed": {"symbol": "AED", "hotel": {"budget": 200,  "mid-range": 600,   "luxury": 1800 }, "level": {0: 100,  1: 200,  2: 550,  3: 1300, 4: 2600}, "range": {"low": (150,  260 ), "mid": (260,  720 ), "high": (720,  2100)}, "ceiling": {"low": 700,   "mid": 2100,  "high": 6500 }},
    "sgd": {"symbol": "S$",  "hotel": {"budget": 80,   "mid-range": 200,   "luxury": 550  }, "level": {0: 40,   1: 80,   2: 190,  3: 400,  4: 680 }, "range": {"low": (55,   100 ), "mid": (100,  260 ), "high": (260,  680 )}, "ceiling": {"low": 250,   "mid": 680,   "high": 2000 }},
}

VIBE_QUERY_MAP: dict[str, str] = {
    "food": "{d} best street food restaurants must eat 2025",
    "foodie": "{d} food tour guide local eats 2025",
    "culture": "{d} heritage culture traditions guide 2025",
    "history": "{d} history main landmarks discovery 2025",
    "adventure": "{d} adventure hiking outdoor activities 2025",
    "nightlife": "{d} nightlife best bars clubs guide 2025",
    "nature": "{d} nature reserves parks scenic beauty 2025",
    "shopping": "{d} shopping markets malls guide 2025",
    "beaches": "{d} best beaches coast guide 2025",
    "art": "{d} modern art museums galleries 2025",
    "wellness": "{d} wellness yoga spa retreat 2025",
    "family": "{d} family friendly attractions guide 2025",
}


# ── Request models ────────────────────────────────────────────────────────────

class PlanTripRequest(BaseModel):
    destination: str
    days: int = 3
    budget: str = "mid"
    travelers: int = 2
    interests: list[str] = []
    start_date: str | None = None
    end_date: str | None = None
    is_public: bool = True
    skip_user_trips: bool = False


class PlanJourneyRequest(BaseModel):
    origin: str
    destination: str
    transport_modes: list[str] = []   # ["bus", "train", "flight", "cab"] — empty = Gemini decides
    travelers: int = 1
    budget: str = "mid"
    departure_date: str | None = None
    stay_nights: int = 2              # nights at destination (used for hotel checkout date)
    include_itinerary: bool = False   # also generate destination day-plan


# ── Retry utility ─────────────────────────────────────────────────────────────

async def _with_retry(
    coro_fn,
    *args,
    max_retries: int = 2,
    base_delay: float = 1.5,
    fallback=None,
    **kwargs,
):
    """Call an async function with exponential backoff. Returns fallback after all retries fail."""
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                print(f"[retry] {getattr(coro_fn, '__name__', '?')} failed after {max_retries + 1}x: {e}")
                return fallback
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
    return fallback


# ── Helpers ───────────────────────────────────────────────────────────────────

def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _nearest_neighbor_sort(activities: list[dict]) -> list[dict]:
    if len(activities) <= 2:
        return activities
    hotel = next((a for a in activities if a.get("type") == "hotel"), None)
    rest = [a for a in activities if a.get("type") != "hotel"]
    with_coords = [a for a in rest if a.get("lat") and a.get("lng")]
    without_coords = [a for a in rest if not (a.get("lat") and a.get("lng"))]
    if len(with_coords) <= 1:
        return activities

    start_lat = hotel["lat"] if hotel and hotel.get("lat") else with_coords[0]["lat"]
    start_lng = hotel["lng"] if hotel and hotel.get("lng") else with_coords[0]["lng"]
    remaining = list(with_coords)
    sorted_acts: list[dict] = []
    while remaining:
        nearest_idx = min(range(len(remaining)), key=lambda i: _haversine_km(start_lat, start_lng, remaining[i]["lat"], remaining[i]["lng"]))
        nxt = remaining.pop(nearest_idx)
        sorted_acts.append(nxt)
        start_lat, start_lng = nxt["lat"], nxt["lng"]

    base_times = ["09:00 AM", "11:00 AM", "01:00 PM", "03:00 PM", "05:00 PM", "07:00 PM", "09:00 PM"]
    all_sorted = sorted_acts + without_coords
    for i, a in enumerate(all_sorted):
        if not a.get("time") or a["time"] == "TBD":
            a["time"] = base_times[i] if i < len(base_times) else "10:00 AM"

    return ([hotel] + all_sorted) if hotel else all_sorted


def _build_date_context(start_date: str | None, end_date: str | None, destination: str | None) -> str:
    if not start_date:
        return ""
    try:
        start = datetime.fromisoformat(start_date)
        month_name = start.strftime("%B")
        year = start.year
        day_name = start.strftime("%A")
        m = start.month
        season = "Spring" if 3 <= m <= 5 else "Summer" if 6 <= m <= 8 else "Autumn/Fall" if 9 <= m <= 11 else "Winter"
        ctx = f"\nTRAVEL DATES: {day_name} {start.day} {month_name} {year}"
        if end_date:
            end = datetime.fromisoformat(end_date)
            ctx += f" → {end.day} {end.strftime('%B')} {end.year}"
        ctx += f" ({season} season in the Northern Hemisphere)."
        ctx += (
            f"\nDATE-AWARE PLANNING NOTES:"
            f"\n- Consider {destination}'s typical weather in {month_name} ({season}): dress code, indoor vs outdoor activities"
            f"\n- Note major local festivals, holidays, or events in {destination} during {month_name}"
            f"\n- Mention peak/off-peak crowd levels for this time of year"
            f"\n- Include 1-2 date-specific tips per day"
        )
        return ctx
    except Exception:
        return ""


def _build_vibe_query(destination: str, primary_interest: str) -> str:
    key = primary_interest.lower().strip()
    tmpl = VIBE_QUERY_MAP.get(key, "{d} complete travel guide vlog itinerary 2025")
    return tmpl.replace("{d}", destination)


async def _fetch_youtube_video(query: str) -> dict | None:
    yt_key = settings.youtube_api_key or settings.google_maps_api_key
    if not yt_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            search_r = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"part": "snippet", "q": query, "type": "video", "maxResults": 20,
                        "relevanceLanguage": "en", "videoDuration": "medium",
                        "videoCategoryId": "19", "order": "viewCount", "key": yt_key},
            )
            search_r.raise_for_status()
            items = [it for it in (search_r.json().get("items") or [])
                     if "#shorts" not in (it.get("snippet", {}).get("title", "") or "").lower()]
            if not items:
                return None

            video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
            snippet_map = {it["id"]["videoId"]: it["snippet"] for it in items if it.get("id", {}).get("videoId")}

            details_r = await client.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "contentDetails,statistics", "id": ",".join(video_ids), "key": yt_key},
            )
            details_r.raise_for_status()
            detail_items = details_r.json().get("items") or []

            def _parse_dur(iso: str) -> int:
                import re
                m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
                if not m:
                    return 0
                return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)

            dest_word = query.lower().split()[0]
            scored = []
            for it in detail_items:
                dur = _parse_dur(it.get("contentDetails", {}).get("duration", ""))
                views = int(it.get("statistics", {}).get("viewCount", 0))
                title = (snippet_map.get(it["id"], {}).get("title", "") or "").lower()
                in_range = 480 <= dur <= 1200
                dur_score = 1.0 if in_range else max(0, 1 - abs(dur - 840) / 1800)
                view_score = min(1, math.log10(views + 1) / 7)
                kw_score = 1.0 if dest_word in title else 0.2
                scored.append((it, view_score * 0.5 + dur_score * 0.3 + kw_score * 0.2))

            best_it = max(scored, key=lambda x: x[1])[0] if scored else None
            if not best_it:
                return None

            snippet = snippet_map.get(best_it["id"], {})
            thumbs = snippet.get("thumbnails", {})
            thumb = (thumbs.get("maxres") or thumbs.get("standard") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
            return {
                "video_id": best_it["id"],
                "title": snippet.get("title", ""),
                "thumbnail": thumb,
                "channel": snippet.get("channelTitle", ""),
                "views": int(best_it.get("statistics", {}).get("viewCount", 0)),
            }
    except Exception as e:
        print(f"[youtube] fetch failed for '{query}': {e}")
        return None


async def _fetch_hotel_options(destination: str, days: int, budget_cat: str, base_price: int, price_level_map: dict) -> list[dict]:
    return _price_hotels(
        await _fetch_hotel_options_raw(destination, days, budget_cat),
        budget_cat, base_price, price_level_map, days,
    )


async def _fetch_hotel_options_raw(destination: str, days: int, safe_budget: str) -> list[dict]:
    budget_cat = "budget" if safe_budget == "low" else "luxury" if safe_budget == "high" else "mid-range"
    tier_query = {
        "budget": f"budget affordable hotels hostels in {destination}",
        "mid-range": f"mid-range 3-star hotels in {destination}",
        "luxury": f"luxury 5-star hotels resorts in {destination}",
    }
    hotels = await search_places(tier_query[budget_cat])
    return [
        {
            "name": h["name"],
            "place_id": h["place_id"],
            "lat": (h.get("geometry") or {}).get("location", {}).get("lat"),
            "lng": (h.get("geometry") or {}).get("location", {}).get("lng"),
            "address": h.get("formatted_address"),
            "rating": h.get("rating"),
            "user_ratings_total": h.get("user_ratings_total"),
            "price_level": h.get("price_level"),
            "photos": [h["photo_reference"]] if h.get("photo_reference") else [],
            "budget_category": budget_cat,
            "_days": days,
        }
        for h in hotels[:5]
    ]


def _price_hotels(raw_hotels: list[dict], budget_cat: str, base_price: int, price_level_map: dict, days: int) -> list[dict]:
    nights = max(1, days - 1)
    result = []
    for h in raw_hotels:
        h = {**h}
        h.pop("_days", None)
        h["budget_category"] = budget_cat
        h["estimated_price_per_night"] = price_level_map.get(h.get("price_level"), base_price)
        h["total_price"] = price_level_map.get(h.get("price_level"), base_price) * nights
        result.append(h)
    return result


def _fetch_user_profile(db, user_id: str) -> dict:
    try:
        r = db.table("profiles").select("country, age, language, currency").eq("id", user_id).maybe_single().execute()
        return r.data or {}
    except Exception:
        return {}


def _fetch_memory_context(db, user_id: str, destination: str | None) -> str:
    try:
        trips_r = db.table("trips").select("destination").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
        graph_r = db.table("user_preference_graph").select("*").eq("user_id", user_id).maybe_single().execute()
        past = list({t["destination"] for t in (trips_r.data or []) if t.get("destination")})[:3]
        graph = graph_r.data
        if not graph and not past:
            return ""
        parts = []
        if past:
            parts.append(f"Previously visited: {', '.join(past)}")
        if graph:
            vw = graph.get("vibe_weights") or {}
            top_vibe = max(vw, key=lambda k: vw[k]) if vw else "balanced"
            parts.append(f"Dominant travel vibe: {top_vibe}")
            if graph.get("budget_behavior"):
                parts.append(f"Budget behavior: {graph['budget_behavior']}")
            if graph.get("pace_preference"):
                parts.append(f"Pace preference: {graph['pace_preference']}")
            fp = graph.get("food_preference") or {}
            top_food = sorted(fp, key=lambda k: -fp[k])[:2]
            if top_food:
                parts.append(f"Prefers: {' and '.join(top_food)} food experiences")
            if destination and graph.get("destination_affinity", {}).get(destination):
                parts.append(f"Affinity for {destination}: {graph['destination_affinity'][destination]}/100")
        return f"\n\nUSER MEMORY & PREFERENCES:\n{'. '.join(parts)}.\nPersonalize accordingly."
    except Exception:
        return ""


def _compute_trip_intelligence(itinerary: dict, budget: str, days: int, ceiling_per_day: dict) -> dict:
    day_plans = itinerary.get("days") or []
    acts_per_day = [len([a for a in (d.get("activities") or []) if a.get("type") != "hotel"]) for d in day_plans]
    avg_acts = sum(acts_per_day) / max(len(day_plans), 1)
    pace = "Relaxed" if avg_acts <= 2.5 else "Moderate" if avg_acts <= 4.5 else "Packed"

    total_cost = sum(a.get("estimated_cost") or 0 for d in day_plans for a in (d.get("activities") or []))
    ceiling = (ceiling_per_day.get(budget) or ceiling_per_day.get("mid") or 500) * days
    budget_fit = max(0, min(100, round((1 - abs(total_cost - ceiling) / max(ceiling, 1)) * 100)))

    total_dist, dist_count = 0.0, 0
    for d in day_plans:
        acts = [a for a in (d.get("activities") or []) if a.get("lat") and a.get("lng")]
        for i in range(1, len(acts)):
            total_dist += _haversine_km(acts[i-1]["lat"], acts[i-1]["lng"], acts[i]["lat"], acts[i]["lng"])
            dist_count += 1
    avg_walking = round(total_dist / max(dist_count, 1) * 10) / 10
    travel_eff = max(0, min(100, round(100 - (avg_walking - 1) * 10)))

    cat_counts: dict[str, int] = {}
    for d in day_plans:
        for a in (d.get("activities") or []):
            if a.get("type") and a["type"] != "hotel":
                cat_counts[a["type"]] = cat_counts.get(a["type"], 0) + 1
    top_cat = max(cat_counts, key=lambda k: cat_counts[k]) if cat_counts else "attraction"

    summary_map = {
        "Relaxed": f"A relaxed {top_cat}-focused journey with room to breathe",
        "Moderate": f"A well-balanced trip mixing {top_cat} and exploration",
        "Packed": "An action-packed adventure — every moment is maximized",
    }
    return {
        "pace": pace,
        "budget_fit_score": budget_fit,
        "travel_efficiency_score": travel_eff,
        "avg_walking_distance_km": avg_walking,
        "top_category": top_cat,
        "total_estimated_cost": total_cost,
        "ai_summary": summary_map.get(pace, "A perfectly curated journey awaits"),
    }


# ── plan-trip route ───────────────────────────────────────────────────────────

@router.post("/plan-trip")
async def plan_trip(body: PlanTripRequest, user=Depends(require_user)):
    db = get_db()
    user_id = user.id

    safe_budget = body.budget if body.budget in ("low", "mid", "high") else "mid"
    nights = max(1, body.days - 1)
    date_ctx = _build_date_context(body.start_date, body.end_date, body.destination)

    # Phase 1: everything in parallel — profile + places + hotels + memory
    (profile,), (candidates, hotel_options, memory_ctx) = await asyncio.gather(
        asyncio.gather(
            asyncio.get_event_loop().run_in_executor(None, _fetch_user_profile, db, user_id),
        ),
        asyncio.gather(
            _with_retry(discover_places, body.destination, fallback={}),
            _with_retry(_fetch_hotel_options_raw, body.destination, body.days, safe_budget, fallback=[]),
            asyncio.get_event_loop().run_in_executor(None, _fetch_memory_context, db, user_id, body.destination),
        ),
    )

    currency_code = (profile.get("currency") or "usd").lower()
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["usd"]
    symbol = ci["symbol"]
    budget_cat = "budget" if safe_budget == "low" else "luxury" if safe_budget == "high" else "mid-range"
    brange = ci["range"][safe_budget]

    hotel_options = _price_hotels(hotel_options, budget_cat, ci["hotel"][budget_cat], ci["level"], body.days)

    place_lookup: dict[str, dict] = {}
    for cat, places in (candidates or {}).items():
        for p in places:
            pid = p.get("place_id")
            if pid:
                place_lookup[pid] = p

    all_candidates = [
        {"name": p["name"], "cat": cat, "rating": p.get("rating"), "place_id": p["place_id"],
         "lat": (p.get("geometry") or {}).get("location", {}).get("lat"),
         "lng": (p.get("geometry") or {}).get("location", {}).get("lng")}
        for cat, places in (candidates or {}).items()
        for p in places
    ]
    dest_photo_ref = (
        (candidates.get("attractions") or [{}])[0].get("photo_reference")
        or (candidates.get("hidden_gems") or [{}])[0].get("photo_reference")
    ) if candidates else None
    recommended_hotel = max(hotel_options, key=lambda h: h.get("rating") or 0) if hotel_options else None

    personalization = ""
    if profile.get("country"):
        personalization += f" Traveler is from {profile['country']}."
    if profile.get("age"):
        personalization += f" Age: {profile['age']}."

    # Phase 2: Gemini
    hotel_json = (
        f'{{"name": "{recommended_hotel["name"]}", "rating": {recommended_hotel.get("rating")}, '
        f'"place_id": "{recommended_hotel["place_id"]}", "lat": {recommended_hotel.get("lat")}, "lng": {recommended_hotel.get("lng")}}}'
        if recommended_hotel else "No hotel data — suggest a well-known hotel in the area"
    )

    prompt = f"""You are an expert travel planner.{personalization}{memory_ctx}{date_ctx}

CANDIDATE PLACES (from Google Places API):
{all_candidates}

HOTEL (recommended, place on Day 1):
{hotel_json}

USER REQUEST:
Destination: {body.destination}
Days: {body.days}
Budget: {safe_budget} ({symbol}{brange[0]}–{symbol}{brange[1]}/night)
Travelers: {body.travelers}
Interests: {', '.join(body.interests) or 'general sightseeing'}

TASK: Create a {body.days}-day itinerary for {body.destination}.

RULES:
- Each day covers one geographic area/neighborhood to minimise travel.
- Each day: at least 1 food + 1 attraction + 1 other. 4-5 activities max per day.
- Hotel: Day 1 first activity only. estimated_cost = total for {nights} nights.
- For candidates above, use exact place_id and set source: "google_places".
- Up to 1 ai_suggested place per day if it fits (set place_id: null, source: "ai_suggested").

Return ONLY valid JSON (no markdown):
{{
  "trip_name": "string",
  "destination": "{body.destination}",
  "destination_description": "4-5 sentences about {body.destination}",
  "days": [
    {{
      "day": 1,
      "theme": "Area Name — Theme",
      "date_tip": "seasonal tip for this day",
      "activities": [
        {{
          "title": "exact place name",
          "type": "attraction|food|nightlife|experience|hotel",
          "category": "Cafe|Museum|Landmark|Park|Street Food|Viewpoint|Beach|Bar",
          "source": "google_places|ai_suggested",
          "place_id": "string or null",
          "time": "09:00 AM",
          "description": "2-3 sentences",
          "tips": "1 practical tip",
          "estimated_cost": 0
        }}
      ]
    }}
  ]
}}"""

    raw = await _with_retry(vertex_generate, prompt, response_mime_type="application/json", timeout=180.0)
    if not raw:
        raise HTTPException(502, "AI trip generation failed after retries")
    ai_json = extract_json(raw)

    ai_json["destination_photo_ref"] = dest_photo_ref
    if dest_photo_ref:
        ai_json["image"] = dest_photo_ref

    # Phase 3: hydrate activities + YouTube in parallel
    primary_interest = (body.interests[0] if body.interests else "").lower()
    yt_query = _build_vibe_query(body.destination, primary_interest)

    hotel_processed = False
    hydration_tasks: list[Any] = []

    for day in (ai_json.get("days") or []):
        if not day.get("activities"):
            continue
        new_acts = []
        for act in day["activities"]:
            if act.get("type") == "hotel":
                if hotel_processed:
                    continue
                hotel_processed = True
            new_acts.append(act)
        day["activities"] = new_acts

        for act in day["activities"]:
            if act.get("type") == "hotel" and recommended_hotel:
                hydration_tasks.append(_hydrate_hotel(act, recommended_hotel))
            else:
                hydration_tasks.append(_hydrate_activity(act, body.destination, place_lookup))

    dest_video, *_ = await asyncio.gather(
        _fetch_youtube_video(yt_query),
        *hydration_tasks,
    )
    ai_json["destination_video"] = dest_video

    # Route optimisation per day
    for day in (ai_json.get("days") or []):
        if day.get("activities") and len(day["activities"]) >= 2:
            day["activities"] = _nearest_neighbor_sort(day["activities"])

    ai_json["currency"] = symbol
    ai_json["hotel_options"] = hotel_options
    ai_json["selected_hotel"] = recommended_hotel
    ai_json["trip_intelligence"] = _compute_trip_intelligence(ai_json, safe_budget, body.days, ci["ceiling"])

    # Phase 4: DB writes (fire-and-forget)
    asyncio.create_task(_db_writes(db, user_id, body, ai_json))

    return ai_json


async def _hydrate_hotel(act: dict, hotel: dict) -> None:
    act["title"] = hotel["name"]
    act["place_id"] = hotel["place_id"]
    act["lat"] = hotel.get("lat")
    act["lng"] = hotel.get("lng")
    act["address"] = hotel.get("address")
    act["rating"] = hotel.get("rating")
    act["user_ratings_total"] = hotel.get("user_ratings_total")
    act["photos"] = hotel.get("photos") or []
    act["source"] = "google_places"
    act["estimated_cost"] = hotel.get("total_price") or act.get("estimated_cost") or 0
    act["youtube_shorts"] = []


async def _hydrate_activity(act: dict, destination: str, place_lookup: dict | None = None) -> None:
    place_data = None
    pid = act.get("place_id") or ""

    if pid and place_lookup and pid in place_lookup:
        place_data = place_lookup[pid]
    elif pid and len(pid) > 20 and act.get("source") == "google_places":
        title = act.get("title", "")
        places = await _with_retry(search_places, f"{title} {destination}", fallback=[])
        place_data = (places or [None])[0]
    elif not pid or act.get("source") == "ai_suggested":
        title = act.get("title", "")
        for prefix in ["Check-in at", "Check-out from", "Lunch at", "Dinner at", "Breakfast at", "Visit", "Explore"]:
            title = title.replace(prefix, "").strip()
        places = await _with_retry(search_places, f"{title} {destination}", fallback=[])
        place_data = (places or [None])[0]

    if place_data:
        geo = (place_data.get("geometry") or {}).get("location", {})
        photos = place_data.get("photos") or []
        photo_ref = (photos[0].get("photo_reference") if photos and isinstance(photos[0], dict)
                     else place_data.get("photo_reference"))
        act["place_id"] = place_data.get("place_id") or act.get("place_id")
        act["lat"] = geo.get("lat")
        act["lng"] = geo.get("lng")
        act["address"] = place_data.get("formatted_address")
        act["rating"] = place_data.get("rating")
        act["user_ratings_total"] = place_data.get("user_ratings_total")
        act["source"] = "google_places"
        act["photos"] = [photo_ref] if photo_ref else []
    else:
        act.setdefault("place_id", None)
        act.setdefault("lat", None)
        act.setdefault("lng", None)
        act.setdefault("address", None)
        act.setdefault("rating", None)
        act.setdefault("photos", [])
        act.setdefault("source", "ai_suggested")

    if not act.get("estimated_cost"):
        defaults = {"food": 800, "attraction": 200, "nightlife": 1500, "experience": 1000}
        act["estimated_cost"] = defaults.get(act.get("type", ""), 500)

    act["youtube_shorts"] = []


# ── Flight alternatives ───────────────────────────────────────────────────────

async def _search_flight_alternatives(
    from_iata: str, to_iata: str, date: str, travelers: int, limit: int = 4,
) -> list[dict]:
    """Search Booking.com + EaseMyTrip in parallel, deduplicate, return up to limit flights."""
    if not from_iata or not to_iata or not date:
        return []
    booking_r, emt_r = await asyncio.gather(
        _with_retry(booking_search_flights, from_iata, to_iata, date, adults=travelers, limit=limit + 2, fallback=[]),
        _with_retry(emt_search_flights,     from_iata, to_iata, date, adults=travelers, limit=limit + 2, fallback=[]),
    )
    seen: set[str] = set()
    merged: list[dict] = []
    for f in (booking_r or []) + (emt_r or []):
        key = (f.get("flight_number") or "") + str(f.get("departure_time") or "")
        if key and key in seen:
            continue
        seen.add(key)
        merged.append(f)
    merged.sort(key=lambda f: f.get("price_total") or 999_999)
    return merged[:limit]


# ── Destination itinerary ─────────────────────────────────────────────────────

async def _gemini_destination_itinerary(
    destination: str, budget: str, symbol: str, travelers: int,
) -> list[dict]:
    """Generate a 2-day arrival itinerary for the destination."""
    prompt = f"""You are a travel expert. Create a practical 2-day arrival itinerary for {destination}.
Budget: {budget} ({symbol}/person). Travelers: {travelers}.
Day 1 = Arrival & orientation. Day 2 = Main highlights.

Return ONLY valid JSON (no markdown):
[
  {{
    "day": 1,
    "theme": "Arrival & Settle In",
    "activities": [
      {{
        "time": "09:00 AM",
        "title": "place name",
        "type": "attraction|food|experience|rest|hotel",
        "category": "Cafe|Landmark|Market|Viewpoint|Restaurant|Park",
        "description": "2 sentences about what to do here",
        "tips": "one practical tip",
        "estimated_cost": 0,
        "duration": "1h 30m"
      }}
    ]
  }}
]"""
    raw = await _with_retry(vertex_generate, prompt, response_mime_type="application/json", timeout=90.0)
    if not raw:
        return []
    try:
        import json as _json
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text[: text.rfind("```")] if text.endswith("```") else text
        text = text.strip()
        # Gemini returns a JSON array — find [ ... ] not { ... }
        start = text.find("[")
        end   = text.rfind("]")
        if start >= 0 and end > start:
            result = _json.loads(text[start : end + 1])
        else:
            result = extract_json(text)
        return result if isinstance(result, list) else [result]
    except Exception as e:
        print(f"[itinerary] parse error: {e} | raw[:200]={raw[:200]}")
        return []


# ── plan-journey: Gemini route planning ──────────────────────────────────────

async def _gemini_plan_route(body: PlanJourneyRequest, budget: str, symbol: str, currency: str) -> dict:
    """
    Ask Gemini to plan the full multi-leg route.
    Returns structured JSON with legs, modes, station codes, and visa info for international routes.
    """
    modes_hint = (
        f"Preferred transport modes: {', '.join(body.transport_modes)}."
        if body.transport_modes
        else "Choose the best transport combination for this route."
    )
    date_hint = f"Departure date: {body.departure_date}." if body.departure_date else "Departure date not specified."

    prompt = f"""You are an expert travel routing specialist with deep knowledge of Indian transport
networks, international routes, and last-mile connectivity for remote destinations.

JOURNEY REQUEST:
Origin: {body.origin}
Destination: {body.destination}
Travelers: {body.travelers} | Budget: {budget} ({symbol})
{modes_hint}
{date_hint}

TASK: Plan the complete, practical door-to-door journey from {body.origin} to {body.destination}.

CRITICAL RULES:
1. ALWAYS break into logical legs — for remote or hill destinations include intermediate hubs.
   Example: Delhi → Aut = [Delhi→Bhuntar by overnight bus, Bhuntar→Aut by shared cab]
   Example: Delhi → Shimla = [Delhi→Kalka by train, Kalka→Shimla by toy train or cab]
2. For TRAIN legs: include exact Indian Railway station codes (NDLS, PNBE, CDG, etc.)
   Only suggest train where rail actually exists — Manali, Leh, Spiti, Aut have NO railway.
3. For BUS legs: mention operator (HRTC, RSRTC, Volvo, etc.) and boarding point if known.
4. For FLIGHT legs: include airport IATA codes (DEL, BOM, IXL, etc.)
5. Cab/shared_cab/auto legs: realistic local fares, no live search needed.
6. If crossing national borders: set is_international=true, provide complete visa guidance.
7. Cost estimates in {currency.upper()} ({symbol}).
8. needs_live_search=true ONLY for bus/train/flight (real schedules fetchable).
   needs_live_search=false for: cab, shared_cab, auto, walk, ferry, metro (no live API).
9. ALWAYS include search_params for searchable legs with the most search-friendly city names.

INTERNATIONAL CHECK: Are {body.origin} and {body.destination} in different countries?
If YES → is_international=true, fill visa_info and documents_needed completely.

Return ONLY valid JSON (no markdown):
{{
  "is_international": false,
  "origin_country": "India",
  "destination_country": "India",
  "visa_required": false,
  "visa_info": null,
  "documents_needed": [],
  "health_requirements": [],
  "currency_info": null,
  "journey_summary": "2-3 sentence narrative of the full journey",
  "total_distance_km": 0,
  "total_duration_approx": "Xh Ym",
  "total_estimated_cost": 0,
  "best_time_to_depart": "specific departure advice",
  "packing_tips": ["tip 1", "tip 2"],
  "hotel_search_location": "nearest town with hotels (use destination if well-known, else nearest hub e.g. Kullu for Aut, Bhuntar for Kasol)",
  "flight_search": {{
    "from_iata": "DEL",
    "to_iata": "KUU",
    "route_note": "Delhi to Bhuntar/Kullu airport — nearest airport to Aut"
  }},
  "legs": [
    {{
      "leg": 1,
      "from": "exact place name",
      "to": "exact place name",
      "mode": "bus|train|flight|cab|shared_cab|auto|metro|walk|ferry",
      "operator_hint": "HRTC / Volvo / IndiGo / local taxi",
      "departure_time_approx": "22:00 or Flexible or Overnight",
      "duration_approx": "Xh Ym",
      "distance_km": 0,
      "estimated_cost_per_person": 0,
      "booking_channel": "RedBus / IRCTC / counter / local stand",
      "tips": "one practical tip",
      "notes": "important detail about this leg",
      "needs_live_search": true,
      "search_params": {{
        "origin": "search-friendly city name",
        "destination": "search-friendly city name",
        "mode_type": "bus|train|flight",
        "from_station_code": "NDLS",
        "to_station_code": "CDG",
        "from_iata": null,
        "to_iata": null
      }}
    }}
  ]
}}

For visa_info (international only):
{{
  "visa_type": "...",
  "max_stay_days": 0,
  "fee": "...",
  "documents": ["..."],
  "how_to_apply": "...",
  "processing_time": "...",
  "important_notes": "..."
}}"""

    raw = await _with_retry(
        vertex_generate, prompt,
        response_mime_type="application/json",
        timeout=120.0,
        max_retries=2,
    )
    if not raw:
        return {}
    try:
        return extract_json(raw)
    except Exception as e:
        print(f"[plan_journey] Gemini route parse error: {e}")
        return {}


# ── plan-journey route ────────────────────────────────────────────────────────

@router.post("/plan-journey")
async def plan_journey(body: PlanJourneyRequest, user=Depends(require_user)):
    """
    Door-to-door multi-leg journey planner.
    Phase 1 — Gemini plans the full route with intermediate stops and station codes.
    Phase 2 — Live transport data fetched in parallel for every bus/train/flight leg.
    Includes complete visa + document guidance for international trips.
    """
    db = get_db()
    user_id = user.id
    safe_budget = body.budget if body.budget in ("low", "mid", "high") else "mid"

    # Fetch profile (quick, needed for currency)
    profile = await asyncio.get_event_loop().run_in_executor(None, _fetch_user_profile, db, user_id)
    currency_code = (profile.get("currency") or "inr").lower()
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]

    # Phase 1 — Gemini plans the route
    route_plan = await _gemini_plan_route(body, safe_budget, symbol, currency_code)
    if not route_plan:
        raise HTTPException(502, "Route planning failed — AI could not plan this journey")

    legs: list[dict] = route_plan.get("legs") or []
    departure_date = body.departure_date or ""

    # Phase 2 — Build live-search tasks for each searchable leg
    live_meta: list[tuple[int, str]] = []   # (leg_index, mode_type)
    live_coros: list = []

    for i, leg in enumerate(legs):
        if not leg.get("needs_live_search") or not departure_date:
            leg["has_live_data"] = False
            continue

        sp = leg.get("search_params") or {}
        mode = leg.get("mode", "").lower()
        origin_s = sp.get("origin") or leg.get("from", "")
        dest_s   = sp.get("destination") or leg.get("to", "")

        if mode == "bus":
            live_meta.append((i, "bus"))
            live_coros.append(_with_retry(
                _transport.search_buses,
                origin_s, dest_s, departure_date, body.travelers,
                fallback={"buses": [], "resolved": False},
            ))

        elif mode in ("train", "railway"):
            from_code = sp.get("from_station_code") or city_to_station_code(origin_s)
            to_code   = sp.get("to_station_code")   or city_to_station_code(dest_s)
            if from_code and to_code:
                live_meta.append((i, "train"))
                live_coros.append(_with_retry(
                    _railway.search,
                    origin_s, dest_s, departure_date,
                    from_code=from_code, to_code=to_code,
                    fallback={"trains": [], "resolved": False},
                ))
            else:
                leg["has_live_data"] = False
                leg["live_search_note"] = "No railway station found for this route segment"

        elif mode == "flight":
            live_meta.append((i, "flight"))
            live_coros.append(_with_retry(
                _flights.search,
                origin_s, dest_s, departure_date,
                adults=body.travelers,
                fallback={"flights": [], "resolved": False},
            ))

    # Phase 2b — flight alternatives (always, using Gemini's airport codes)
    fs = route_plan.get("flight_search") or {}
    flight_from_iata = fs.get("from_iata") or _flights.resolve_iata(body.origin) or ""
    flight_to_iata   = fs.get("to_iata")   or _flights.resolve_iata(body.destination) or ""
    flight_alt_coro = _search_flight_alternatives(
        flight_from_iata, flight_to_iata, departure_date, body.travelers
    ) if departure_date and flight_from_iata and flight_to_iata else None

    # Phase 2c — destination itinerary when include_itinerary=True
    itinerary_coro = _gemini_destination_itinerary(
        body.destination, safe_budget, symbol, body.travelers
    ) if body.include_itinerary else None

    # Phase 2d — hotels at destination (always when departure_date given)
    # Use hotel_search_location from Gemini — it picks the nearest bookable hub
    # (e.g. "Kullu" for "Aut", "Bhuntar" for "Kasol") to avoid Booking.com 400s.
    hotel_search_loc = (
        route_plan.get("hotel_search_location") or body.destination
    ).strip()
    hotel_coro = None
    if departure_date:
        try:
            checkin  = (datetime.fromisoformat(departure_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            checkout = (datetime.fromisoformat(departure_date) + timedelta(days=1 + body.stay_nights)).strftime("%Y-%m-%d")
            hotel_coro = _with_retry(
                _hotels.search,
                hotel_search_loc, checkin, checkout,
                adults=body.travelers, budget=safe_budget,
                fallback={"hotels": [], "source": "none"},
            )
        except Exception:
            hotel_coro = None

    # Run all live tasks in parallel
    extra_coros: list = []
    if flight_alt_coro:  extra_coros.append(flight_alt_coro)
    if itinerary_coro:   extra_coros.append(itinerary_coro)
    if hotel_coro:       extra_coros.append(hotel_coro)

    all_coros = live_coros + extra_coros
    all_results = await asyncio.gather(*all_coros, return_exceptions=True) if all_coros else []

    transport_results = all_results[: len(live_coros)]
    extras = all_results[len(live_coros):]
    ei = 0
    flight_alt_result = extras[ei] if flight_alt_coro and ei < len(extras) else None; ei += bool(flight_alt_coro)
    itinerary_result  = extras[ei] if itinerary_coro  and ei < len(extras) else None; ei += bool(itinerary_coro)
    hotel_result      = extras[ei] if hotel_coro       and ei < len(extras) else None

    # Inject live data + booking links into legs
    for task_idx, (leg_idx, mode_type) in enumerate(live_meta):
        if task_idx >= len(transport_results):
            break
        result = transport_results[task_idx]
        if isinstance(result, Exception):
            legs[leg_idx]["has_live_data"] = False
            continue

        legs[leg_idx]["live_data"] = result
        has = bool(
            result.get("buses")   if mode_type == "bus"    else
            result.get("trains")  if mode_type == "train"  else
            result.get("flights") if mode_type == "flight" else False
        )
        legs[leg_idx]["has_live_data"] = has
        legs[leg_idx]["booking_url"] = _leg_booking_url(legs[leg_idx], result, departure_date)

    route_plan["legs"] = legs
    route_plan["currency"] = symbol
    route_plan["has_real_data"] = any(leg.get("has_live_data") for leg in legs)

    # Flight alternatives
    if flight_alt_result and not isinstance(flight_alt_result, Exception) and flight_alt_result:
        route_plan["flight_alternatives"] = {
            "from_iata": flight_from_iata,
            "to_iata": flight_to_iata,
            "route_note": fs.get("route_note", ""),
            "flights": flight_alt_result,
            "best_pick": min(flight_alt_result, key=lambda f: f.get("price_total") or 999_999),
        }
    else:
        route_plan["flight_alternatives"] = {
            "from_iata": flight_from_iata,
            "to_iata": flight_to_iata,
            "route_note": fs.get("route_note", ""),
            "flights": [],
            "best_pick": None,
        }

    # Destination itinerary
    if itinerary_result and not isinstance(itinerary_result, Exception):
        route_plan["destination_itinerary"] = itinerary_result

    if hotel_result and not isinstance(hotel_result, Exception):
        hotels_list = hotel_result.get("hotels") or []
        ranked = _rank_hotels(hotels_list)
        route_plan["destination_hotels"] = {
            "search_location": hotel_search_loc,
            "source": hotel_result.get("source"),
            "checkin": (datetime.fromisoformat(departure_date) + timedelta(days=1)).strftime("%Y-%m-%d") if departure_date else None,
            "checkout": (datetime.fromisoformat(departure_date) + timedelta(days=1 + body.stay_nights)).strftime("%Y-%m-%d") if departure_date else None,
            "nights": body.stay_nights,
            "hotels": ranked[:8],
            "top_pick": ranked[0] if ranked else None,
        }

    return route_plan


def _leg_booking_url(leg: dict, live_result: dict, departure_date: str) -> str | None:
    """Build a direct booking/search URL for a transport leg."""
    mode = leg.get("mode", "")
    sp = leg.get("search_params") or {}

    if mode == "bus":
        # Use the booking_url already embedded in the first live bus result
        buses = live_result.get("buses") or []
        if buses and buses[0].get("booking_url"):
            return buses[0]["booking_url"]
        # Fallback: generic RedBus search
        origin = sp.get("origin") or leg.get("from", "")
        dest   = sp.get("destination") or leg.get("to", "")
        slug_o = origin.lower().replace(" ", "-")
        slug_d = dest.lower().replace(" ", "-")
        return f"https://www.redbus.in/bus-tickets/{slug_o}-to-{slug_d}"

    if mode in ("train", "railway"):
        fc = sp.get("from_station_code") or ""
        tc = sp.get("to_station_code") or ""
        if fc and tc and departure_date:
            try:
                from datetime import datetime as _dt
                d = _dt.fromisoformat(departure_date).strftime("%Y%m%d")
                return f"https://www.irctc.co.in/nget/train-search?fromStation={fc}&toStation={tc}&journeyDate={d}"
            except Exception:
                pass
        return f"https://www.confirmtkt.com/train-between-stations/{fc}/{tc}"

    if mode == "flight":
        flights = live_result.get("flights") or []
        # EaseMyTrip/Booking.com results may carry a booking_url
        for f in flights:
            if f.get("booking_url"):
                return f["booking_url"]
        # Fallback: MakeMyTrip search
        fi = sp.get("from_iata") or ""
        ti = sp.get("to_iata") or ""
        date_fmt = departure_date.replace("-", "") if departure_date else ""
        if fi and ti and date_fmt:
            return f"https://www.makemytrip.com/flights/domestic/results/oneway/{fi}/{ti}/{date_fmt}/1/0/0/E/MYB"

    return None


def _rank_hotels(hotels: list[dict]) -> list[dict]:
    """Sort hotels by a value score: review_score * 0.6 + price_efficiency * 0.4."""
    def _score(h: dict) -> float:
        review = float(h.get("review_score") or h.get("ta_rating") or h.get("star_rating") or 0)
        price  = float(h.get("price_per_night") or h.get("estimated_price_per_night") or 0)
        # Higher review → better. Lower price → more efficient (invert: 10k / price, capped at 1)
        price_eff = min(1.0, 10000 / price) if price > 0 else 0.5
        return review * 0.6 + price_eff * 10 * 0.4

    return sorted(hotels, key=_score, reverse=True)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _pick_city_id(cities: list[dict]) -> int | None:
    if not cities:
        return None
    city_types = [c for c in cities if c.get("type") == "CITY"]
    area_types = [c for c in cities if c.get("type") == "AREA"]
    if city_types:
        return city_types[0]["id"]
    if area_types:
        return area_types[0]["id"]
    return cities[0]["id"]


async def _db_writes(db, user_id: str, body: PlanTripRequest, itinerary: dict) -> None:
    try:
        trip_start = datetime.fromisoformat(body.start_date) if body.start_date else datetime.now()
        trip_end = datetime.fromisoformat(body.end_date) if body.end_date else trip_start + timedelta(days=body.days)

        await asyncio.gather(
            asyncio.get_event_loop().run_in_executor(None, lambda: (
                db.table("user_searches").insert({
                    "user_id": user_id,
                    "destination": body.destination,
                    "days": body.days,
                    "num_travelers": body.travelers,
                    "budget": body.budget,
                    "interests": body.interests,
                    "search_params": body.model_dump(),
                }).execute()
            )),
            asyncio.get_event_loop().run_in_executor(None, lambda: (
                db.table("internal_trip_logs").insert({
                    "user_id": user_id,
                    "destination": body.destination,
                    "days": body.days,
                    "itinerary_data": itinerary,
                }).execute()
            )) if not body.skip_user_trips else asyncio.sleep(0),
        )
    except Exception as e:
        print(f"[plan_trip] DB write error (non-fatal): {e}")
