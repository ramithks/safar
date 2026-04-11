"""
POST /api/v1/bulk-generate-explore
X-API-Key: <BULK_EXPLORE_API_KEY>

Generates AI-planned trips for multiple city × vibe × budget combinations
and saves them all under a single demo account into `internal_trip_logs`.
This seeds the Explore / Trips Feed pages without requiring real user trips.

Request body:
{
  "trips": [
    {
      "destination": "Paris, France",
      "vibe": "culture",          // any VIBE_QUERY_MAP key
      "budget": "mid",            // "low" | "mid" | "high"
      "days": 3,
      "travelers": 2
    },
    ...
  ],
  "demo_user_email": "demo@travbuddy.app",   // optional override
  "demo_user_password": "TravBuddy_Demo_2025!" // optional override
}

Response:
{
  "generated": 5,
  "failed": 1,
  "results": [
    { "destination": "Paris", "status": "ok",    "trip_id": "uuid" },
    { "destination": "Bali",  "status": "error", "error": "..." }
  ]
}
"""
import asyncio
import math
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.db.client import get_db
from app.services.vertex import vertex_generate, extract_json
from app.services.places import search_places, discover_places
from app.config import settings

# ── Re-use helpers from plan_trip ─────────────────────────────────────────────
from app.api.v1.plan_trip import (
    CURRENCY_CONFIG,
    VIBE_QUERY_MAP,
    _haversine_km,
    _nearest_neighbor_sort,
    _fetch_hotel_options_raw,
    _price_hotels,
    _fetch_youtube_video,
    _build_vibe_query,
    _hydrate_activity,
    _hydrate_hotel,
    _compute_trip_intelligence,
)

router = APIRouter()

# ── All vibes & budgets used for auto-expansion ───────────────────────────────
ALL_VIBES   = ["culture", "food", "adventure", "nightlife", "nature", "beaches", "art", "wellness", "history", "shopping", "family"]
ALL_BUDGETS = ["low", "mid", "high"]

# ── Demo account defaults ─────────────────────────────────────────────────────
DEFAULT_DEMO_EMAIL    = "demo@travbuddy.app"
DEFAULT_DEMO_PASSWORD = "TravBuddy_Demo_2025!"

# ── API key guard (simple shared secret, loaded from .env) ───────────────────
BULK_API_KEY = settings.bulk_explore_api_key


def _require_bulk_key(x_api_key: str = Header(..., alias="X-API-Key")):
    if x_api_key != BULK_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


# ── Request / Response models ─────────────────────────────────────────────────

class TripSpec(BaseModel):
    destination: str
    vibe: str = "culture"        # must be a VIBE_QUERY_MAP key (falls back gracefully)
    budget: str = "mid"          # "low" | "mid" | "high"
    days: int = Field(default=3, ge=1, le=10)
    travelers: int = Field(default=2, ge=1, le=20)
    currency: str = "usd"        # iso currency code for pricing


class CitySpec(BaseModel):
    """
    Pass just a city name (and optional overrides) — the endpoint
    will auto-generate every combination of vibe × budget for it.

    Example:
        {"name": "Bali, Indonesia", "days": 4, "travelers": 2}
        → 11 vibes × 3 budgets = 33 TripSpecs auto-created
    """
    name: str                      # e.g. "Paris, France" or just "Paris"
    days: int = Field(default=3, ge=1, le=10)
    travelers: int = Field(default=2, ge=1, le=20)
    currency: str = "usd"
    vibes: list[str] | None = None   # subset of vibes; None = all
    budgets: list[str] | None = None # subset of budgets; None = all


class BulkExploreRequest(BaseModel):
    """
    You can use EITHER (or both) input styles:

    Style A — explicit specs (full control):
        { "trips": [{"destination": "Paris", "vibe": "culture", "budget": "mid"}] }

    Style B — just city names, auto-expand selected combos:
        { "cities": [{"name": "Paris", "vibes": ["culture","food","adventure","nature","nightlife"], "budgets": ["mid"]}] }

    Mix both freely. Duplicates are deduplicated automatically.
    Supports up to 50 cities / 300 total specs per call.
    """
    trips: list[TripSpec] = Field(default_factory=list, max_length=400)
    cities: list[CitySpec] = Field(
        default_factory=list,
        max_length=100,
        description="City names to auto-expand into vibe × budget combos"
    )
    demo_user_email: str = DEFAULT_DEMO_EMAIL
    demo_user_password: str = DEFAULT_DEMO_PASSWORD
    concurrency: int = Field(default=5, ge=1, le=15,
                              description="How many trips to generate in parallel")


class TripResult(BaseModel):
    destination: str
    vibe: str
    budget: str
    status: str          # "ok" | "error"
    trip_id: str | None = None
    error: str | None = None


class BulkExploreResponse(BaseModel):
    generated: int
    failed: int
    total_specs: int
    expansion_count: int   # number of specs auto-created from `cities`
    demo_user_id: str
    results: list[TripResult]


# ── Demo-user helpers ─────────────────────────────────────────────────────────

def _get_or_create_demo_user(email: str, password: str) -> str:
    """
    Returns the Supabase user UUID for the demo account, creating it if needed.
    Uses the service-role client so no JWT is required.
    """
    db = get_db()
    # Try to sign in first (cheap path)
    try:
        from supabase import create_client
        anon = create_client(settings.supabase_url, settings.supabase_anon_key)
        res = anon.auth.sign_in_with_password({"email": email, "password": password})
        if res.user:
            print(f"[bulk_explore] demo user signed in: {res.user.id}")
            return res.user.id
    except Exception as e:
        print(f"[bulk_explore] sign-in failed ({e}), will try admin create")

    # Create via admin API (service role)
    try:
        from supabase import create_client
        admin = create_client(settings.supabase_url, settings.supabase_service_key)
        res = admin.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
        })
        if res.user:
            uid = res.user.id
            print(f"[bulk_explore] demo user created: {uid}")
            # Upsert a profile row so trips_feed shows a nice author name
            try:
                admin.table("profiles").upsert({
                    "id": uid,
                    "full_name": "TravBuddy Explorer",
                    "avatar_url": None,
                    "currency": "usd",
                }, on_conflict="id").execute()
            except Exception:
                pass
            return uid
    except Exception as e:
        raise RuntimeError(f"Cannot create/retrieve demo user: {e}")


# ── Core trip-generation logic (mirrors plan_trip but no HTTP request context) ─

async def _fetch_city_video(destination: str) -> dict | None:
    """
    Fetch ONE generic travel video for a city. Called once per unique city
    in bulk runs to avoid burning YouTube quota on every vibe variant.
    Query cost: 100 (search) + ~5 (videos.list) = ~105 units per city.
    """
    yt_key = settings.youtube_api_key
    if not yt_key:
        return None
    query = f"{destination} travel guide vlog 2025"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            search_r = await client.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "snippet", "q": query, "type": "video",
                    "maxResults": 5,           # only need 1 good result
                    "relevanceLanguage": "en", "videoDuration": "medium",
                    "videoCategoryId": "19",   # Travel & Events
                    "order": "viewCount", "key": yt_key,
                },
            )
            if search_r.status_code in (403, 429):
                print(f"[youtube] quota/auth error {search_r.status_code} for '{destination}' — skipping video")
                return None
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
            if details_r.status_code in (403, 429):
                return None
            details_r.raise_for_status()

            import re

            def _parse_dur(iso: str) -> int:
                m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
                if not m:
                    return 0
                return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)

            scored = []
            for it in (details_r.json().get("items") or []):
                dur = _parse_dur(it.get("contentDetails", {}).get("duration", ""))
                views = int(it.get("statistics", {}).get("viewCount", 0))
                in_range = 480 <= dur <= 1200
                dur_score = 1.0 if in_range else max(0, 1 - abs(dur - 840) / 1800)
                view_score = min(1, math.log10(views + 1) / 7)
                scored.append((it, view_score * 0.6 + dur_score * 0.4))

            best = max(scored, key=lambda x: x[1])[0] if scored else None
            if not best:
                return None

            snippet = snippet_map.get(best["id"], {})
            thumbs = snippet.get("thumbnails", {})
            thumb = (thumbs.get("maxres") or thumbs.get("standard") or thumbs.get("high") or thumbs.get("default") or {}).get("url", "")
            return {
                "video_id": best["id"],
                "title": snippet.get("title", ""),
                "thumbnail": thumb,
                "channel": snippet.get("channelTitle", ""),
                "views": int(best.get("statistics", {}).get("viewCount", 0)),
            }
    except Exception as e:
        print(f"[youtube] city fetch failed for '{destination}': {e}")
        return None


async def _generate_one_trip(
    spec: TripSpec,
    demo_user_id: str,
    dest_video: dict | None = None,   # pre-fetched by caller — skips YouTube call
) -> TripResult:
    """Generate a single AI trip for `spec` and save to DB. Returns TripResult."""
    db = get_db()
    destination = spec.destination
    safe_budget = spec.budget if spec.budget in ("low", "mid", "high") else "mid"
    days = spec.days
    nights = max(1, days - 1)
    currency_code = spec.currency.lower()
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["usd"]
    symbol = ci["symbol"]
    budget_cat = "budget" if safe_budget == "low" else "luxury" if safe_budget == "high" else "mid-range"
    brange = ci["range"][safe_budget]

    try:
        # Phase 1: Places + hotels (parallel)
        candidates, hotel_options_raw = await asyncio.gather(
            discover_places(destination),
            _fetch_hotel_options_raw(destination, days, safe_budget),
        )

        hotel_options = _price_hotels(
            hotel_options_raw, budget_cat, ci["hotel"][budget_cat], ci["level"], days
        )

        place_lookup: dict[str, dict] = {}
        for cat, places in candidates.items():
            for p in places:
                pid = p.get("place_id")
                if pid:
                    place_lookup[pid] = p

        all_candidates = [
            {
                "name": p["name"], "cat": cat, "rating": p.get("rating"),
                "place_id": p["place_id"],
                "lat": (p.get("geometry") or {}).get("location", {}).get("lat"),
                "lng": (p.get("geometry") or {}).get("location", {}).get("lng"),
            }
            for cat, places in candidates.items()
            for p in places
        ]

        dest_photo_ref = (
            (candidates.get("attractions") or [{}])[0].get("photo_reference")
            or (candidates.get("hidden_gems") or [{}])[0].get("photo_reference")
        )
        recommended_hotel = max(hotel_options, key=lambda h: h.get("rating") or 0) if hotel_options else None

        # Vibe-aware description for the interest field
        vibe_key = spec.vibe.lower().strip()
        interests_label = vibe_key.replace("_", " ").title()

        hotel_json = (
            f'{{"name": "{recommended_hotel["name"]}", "rating": {recommended_hotel.get("rating")}, '
            f'"place_id": "{recommended_hotel["place_id"]}", "lat": {recommended_hotel.get("lat")}, "lng": {recommended_hotel.get("lng")}}}'
            if recommended_hotel else "No hotel data — suggest a well-known hotel in the area"
        )

        # Phase 2: Gemini
        prompt = f"""You are an expert travel planner with a {vibe_key} travel style focus.

CANDIDATE PLACES (from Google Places API):
{all_candidates}

HOTEL (recommended, place on Day 1):
{hotel_json}

USER REQUEST:
Destination: {destination}
Days: {days}
Budget: {safe_budget} ({symbol}{brange[0]}–{symbol}{brange[1]}/night)
Travelers: {spec.travelers}
Interests: {interests_label} (primary vibe: {vibe_key})

TASK: Create a {days}-day itinerary for {destination} themed around {vibe_key}.

RULES:
- Each day covers one geographic area/neighborhood to minimise travel.
- Each day: at least 1 food + 1 attraction + 1 other. 4-5 activities max per day.
- Hotel: Day 1 first activity only. estimated_cost = total for {nights} nights.
- Lean heavily into the '{vibe_key}' theme — pick places that embody this vibe.
- For candidates above, use exact place_id and set source: "google_places".
- Up to 1 ai_suggested place per day if it fits (set place_id: null, source: "ai_suggested").

Return ONLY valid JSON (no markdown):
{{
  "trip_name": "string",
  "destination": "{destination}",
  "destination_description": "4-5 sentences about {destination}",
  "vibe": "{vibe_key}",
  "days": [
    {{
      "day": 1,
      "theme": "Area Name — Theme",
      "date_tip": "seasonal/vibe tip for this day",
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

        raw = await vertex_generate(prompt, response_mime_type="application/json", timeout=180.0)
        ai_json = extract_json(raw)

        ai_json["destination_photo_ref"] = dest_photo_ref
        if dest_photo_ref:
            ai_json["image"] = dest_photo_ref
        ai_json["vibe"] = vibe_key
        ai_json["budget"] = safe_budget

        # Phase 3: Hydrate activities (parallel). YouTube video is pre-fetched
        # at the city level by the caller — no per-trip YouTube call needed.
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
                    hydration_tasks.append(_hydrate_activity(act, destination, place_lookup))

        if hydration_tasks:
            await asyncio.gather(*hydration_tasks)
        ai_json["destination_video"] = dest_video  # passed in from city-level prefetch

        # Route optimisation per day
        for day in (ai_json.get("days") or []):
            if day.get("activities") and len(day["activities"]) >= 2:
                day["activities"] = _nearest_neighbor_sort(day["activities"])

        ai_json["currency"] = symbol
        ai_json["hotel_options"] = hotel_options
        ai_json["selected_hotel"] = recommended_hotel
        ai_json["trip_intelligence"] = _compute_trip_intelligence(
            ai_json, safe_budget, days, ci["ceiling"]
        )

        # Phase 4: Save to DB (blocking here since we need the ID)
        db_result = await asyncio.get_event_loop().run_in_executor(None, lambda: (
            db.table("internal_trip_logs").insert({
                "user_id": demo_user_id,
                "destination": destination,
                "days": days,
                "itinerary_data": ai_json,
            }).execute()
        ))
        inserted = (db_result.data or [{}])[0]
        trip_id = inserted.get("id") or inserted.get("trip_id")

        return TripResult(
            destination=destination,
            vibe=vibe_key,
            budget=safe_budget,
            status="ok",
            trip_id=str(trip_id) if trip_id else None,
        )

    except Exception as e:
        print(f"[bulk_explore] FAILED for {destination} ({spec.vibe}/{spec.budget}): {e}")
        return TripResult(
            destination=destination,
            vibe=spec.vibe,
            budget=safe_budget,
            status="error",
            error=str(e)[:300],
        )


# ── City auto-expansion helper ────────────────────────────────────────────────

def _expand_cities(cities: list[CitySpec]) -> tuple[list[TripSpec], int]:
    """
    Expand each CitySpec into TripSpec × (vibes) × (budgets).
    Returns (expanded_specs, total_expansion_count).
    """
    expanded: list[TripSpec] = []
    for city in cities:
        vibes   = city.vibes   if city.vibes   else ALL_VIBES
        budgets = city.budgets if city.budgets else ALL_BUDGETS
        for vibe in vibes:
            for budget in budgets:
                expanded.append(TripSpec(
                    destination=city.name,
                    vibe=vibe,
                    budget=budget,
                    days=city.days,
                    travelers=city.travelers,
                    currency=city.currency,
                ))
    return expanded, len(expanded)


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/bulk-generate-explore", response_model=BulkExploreResponse)
async def bulk_generate_explore(
    body: BulkExploreRequest,
    _: None = Depends(_require_bulk_key),
):
    """
    Bulk-generate AI trips for multiple city × vibe × budget combinations.

    • Pass `trips` for explicit control (destination + vibe + budget per entry).
    • Pass `cities` to auto-expand: every city gets ALL vibes × ALL budgets.
    • Both can be mixed freely.

    All trips are saved under the demo account so the Explore/Trips-Feed pages
    are richly populated.
    Protected by X-API-Key header (set BULK_EXPLORE_API_KEY in .env).
    """
    # 1. Expand city shortcuts into full TripSpecs
    city_expanded, expansion_count = _expand_cities(body.cities)

    # 2. Merge explicit + expanded, deduplicate by (destination, vibe, budget)
    all_specs: list[TripSpec] = []
    seen: set[tuple] = set()
    for spec in list(body.trips) + city_expanded:
        key = (spec.destination.lower().strip(), spec.vibe.lower(), spec.budget.lower())
        if key not in seen:
            seen.add(key)
            all_specs.append(spec)

    if not all_specs:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one entry in `trips` or `cities`."
        )

    print(f"[bulk_explore] {len(all_specs)} specs total ({expansion_count} from city expansion)")

    # 3. Ensure demo user exists
    demo_user_id = await asyncio.get_event_loop().run_in_executor(
        None, _get_or_create_demo_user, body.demo_user_email, body.demo_user_password
    )

    # 4. Pre-fetch ONE YouTube video per unique city (saves ~80% of YouTube quota).
    #    All vibe variants for the same city share the same video.
    unique_destinations = list({s.destination for s in all_specs})
    print(f"[bulk_explore] pre-fetching YouTube videos for {len(unique_destinations)} cities")
    yt_semaphore = asyncio.Semaphore(3)  # limit concurrent YouTube calls

    async def _fetch_with_limit(dest: str) -> tuple[str, dict | None]:
        async with yt_semaphore:
            return dest, await _fetch_city_video(dest)

    yt_results = await asyncio.gather(*[_fetch_with_limit(d) for d in unique_destinations])
    city_video_cache: dict[str, dict | None] = dict(yt_results)
    print(f"[bulk_explore] YouTube prefetch done — {sum(1 for v in city_video_cache.values() if v)} videos fetched")

    # 5. Generate trips with bounded concurrency
    semaphore = asyncio.Semaphore(body.concurrency)

    async def _guarded(spec: TripSpec) -> TripResult:
        async with semaphore:
            return await _generate_one_trip(
                spec,
                demo_user_id,
                dest_video=city_video_cache.get(spec.destination),
            )

    results: list[TripResult] = await asyncio.gather(*[_guarded(s) for s in all_specs])

    generated = sum(1 for r in results if r.status == "ok")
    failed = len(results) - generated

    return BulkExploreResponse(
        generated=generated,
        failed=failed,
        total_specs=len(all_specs),
        expansion_count=expansion_count,
        demo_user_id=demo_user_id,
        results=results,
    )
