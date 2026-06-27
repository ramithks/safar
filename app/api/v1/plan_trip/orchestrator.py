"""
plan_trip orchestration — all business logic, no HTTP/SSE concerns.

run_plan_trip() returns the full itinerary dict.
Caller: endpoint.py (POST /plan-trip).
"""
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

from fastapi import HTTPException

from app.db.client import get_db
from app.services.vertex import vertex_generate, extract_json
from app.services.places import discover_places
from app.core.timing import RequestTrace

from .models import PlanTripRequest
from .helpers import (
    CURRENCY_CONFIG,
    build_date_context,
    fetch_user_profile,
    fetch_memory_context,
    fill_deterministic_fields,
    assign_activity_times,
    nearest_neighbor_sort,
    compute_trip_intelligence,
    with_retry,
)
from .hydration import batch_hydrate_days
from .validation import (
    annotate_confidence, filter_low_confidence,
    trip_health_check, auto_fix_trip,
)

# ── Gemini prompt ─────────────────────────────────────────────────────────────
# Gemini outputs ONLY narrative fields. System fills time/duration/cost.

_PROMPT_TEMPLATE = """\
You are an expert travel planner.{personalization}{memory_ctx}{date_ctx}

CANDIDATE PLACES (from Google Places API — use these preferentially):
{candidates}

USER REQUEST:
Destination: {destination}
Days: {days}
Budget: {budget} ({symbol}{low}–{symbol}{high}/day)
Travelers: {travelers}
Interests: {interests}

TASK: Create a rich {days}-day itinerary for {destination}.

RULES:
- Each day covers ONE geographic area/neighborhood — ALL activities on a single day must be within 5 km of each other. No cross-city or cross-region days.
- Each day: at least 1 food + 1 attraction + 1 other. MAX 4 activities per day.
- Do NOT include hotel check-in/check-out as an activity.
- For candidates, use exact place_id and set source: "google_places".
- Up to 1 ai_suggested place per day if it adds real value (place_id: null, source: "ai_suggested").
- description: vivid and specific (what to see, why it matters, a hidden detail).
- must_try: actual dish/drink name for food activities, else null.
- Do NOT output: time, estimated_cost, duration, lat, lng, address, rating.
  The system fills these automatically.
- best_time must be precise — the system uses it to assign visit times:
  "Morning" = before noon | "Afternoon" = noon–5 PM | "Evening" = after 5 PM | "Anytime" = flexible.
  Sunset viewpoints MUST be "Evening". Dawn/sunrise viewpoints MUST be "Morning". Treks MUST be "Morning".
- Category diversity: spread across Nature (Viewpoint/Trek/Waterfall/Beach/Park), Culture (Museum/Temple/Landmark/Gallery), Food (Cafe/Restaurant/Street Food), Nightlife (Bar/Rooftop). NEVER use the same category more than twice across the whole trip.
- No revisit days. Every day must have genuinely different experiences. Day {days} should feel as fresh as Day 1.
- If it is a hill/village destination, do NOT suggest clubs, nightclubs, or urban nightlife. Use local cafes and stargazing instead.

Return ONLY valid JSON (no markdown):
{{
  "trip_name": "string",
  "destination": "{destination}",
  "destination_description": "4-5 vivid sentences: history, culture, must-see, best time to visit",
  "days": [
    {{
      "day": 1,
      "theme": "Area Name — Theme",
      "neighborhood_highlight": "1 sentence on why this area deserves a full day",
      "date_tip": "seasonal/crowd tip for this day",
      "activities": [
        {{
          "title": "exact place name",
          "type": "attraction|food|nightlife|experience",
          "category": "Cafe|Museum|Landmark|Park|Street Food|Viewpoint|Beach|Bar|Temple|Market|Restaurant|Rooftop|Gallery|Trek|Waterfall|Camping",
          "source": "google_places|ai_suggested",
          "place_id": "string or null",
          "best_time": "Morning|Afternoon|Evening|Anytime",
          "description": "2-3 vivid sentences: what makes it unmissable, a hidden detail",
          "tips": "1 highly practical tip",
          "must_try": "specific dish or null"
        }}
      ]
    }}
  ]
}}"""


async def run_plan_trip(body: PlanTripRequest, user_id: str, trace: RequestTrace | None = None, profile: dict | None = None) -> dict:
    db = get_db()
    safe_budget = body.budget if body.budget in ("low", "mid", "high") else "mid"
    date_ctx = build_date_context(body.start_date, body.end_date, body.destination)

    # ── Phase 1: profile + places + memory (parallel) ─────────────────────────
    async with (trace.track("phase1_parallel") if trace else _noop()):
        if profile is None:
            (profile,), (candidates, memory_ctx) = await asyncio.gather(
                asyncio.gather(
                    asyncio.get_event_loop().run_in_executor(None, fetch_user_profile, db, user_id),
                ),
                asyncio.gather(
                    with_retry(discover_places, body.destination, fallback={}),
                    asyncio.get_event_loop().run_in_executor(None, fetch_memory_context, db, user_id, body.destination),
                ),
            )
        else:
            candidates, memory_ctx = await asyncio.gather(
                with_retry(discover_places, body.destination, fallback={}),
                asyncio.get_event_loop().run_in_executor(None, fetch_memory_context, db, user_id, body.destination),
            )

    # Default to INR (not USD) so the itinerary's currency matches the journey
    # planner and the live scraper prices (EaseMyTrip/Booking/ConfirmTKT all
    # return INR). A mismatched default makes the budget engine sum INR transport
    # with USD food and compare against a USD ceiling — producing nonsense totals.
    currency_code = (profile.get("currency") or "inr").lower()
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["usd"]
    symbol = ci["symbol"]
    brange = ci["range"][safe_budget]

    place_lookup: dict[str, dict] = {
        p["place_id"]: p
        for cat, places in (candidates or {}).items()
        for p in places
        if p.get("place_id")
    }
    all_candidates = [
        {"name": p["name"], "cat": cat, "place_id": p["place_id"],
         "lat": (p.get("geometry") or {}).get("location", {}).get("lat"),
         "lng": (p.get("geometry") or {}).get("location", {}).get("lng")}
        for cat, places in (candidates or {}).items()
        for p in places
    ]
    dest_photo_ref = (
        (candidates.get("attractions") or [{}])[0].get("photo_reference")
        or (candidates.get("hidden_gems") or [{}])[0].get("photo_reference")
    ) if candidates else None

    personalization = ""
    if profile.get("country"):
        personalization += f" Traveler is from {profile['country']}."
    if profile.get("age"):
        personalization += f" Age: {profile['age']}."

    # ── Hotels-only path (no Gemini, returns empty itinerary) ────────────────
    if not body.include_itinerary:
        result = {
            "trip_name": f"{body.days} Days in {body.destination}",
            "destination": body.destination,
            "cover_photo_ref": dest_photo_ref,
            "currency": symbol,
            "days": [],
            "include_itinerary": False,
        }
        asyncio.create_task(_db_writes(db, user_id, body, result))
        return result

    # ── Phase 3: Gemini itinerary ─────────────────────────────────────────────
    prompt = _PROMPT_TEMPLATE.format(
        personalization=personalization,
        memory_ctx=memory_ctx,
        date_ctx=date_ctx,
        candidates=all_candidates,
        destination=body.destination,
        days=body.days,
        budget=safe_budget,
        symbol=symbol,
        low=brange[0],
        high=brange[1],
        travelers=body.travelers,
        interests=", ".join(body.interests) or "general sightseeing",
    )

    async with (trace.track("gemini") if trace else _noop()):
        raw = await with_retry(vertex_generate, prompt, response_mime_type="application/json", timeout=180.0)
    if not raw:
        raise HTTPException(502, "AI trip generation failed after retries")
    ai_json = extract_json(raw)

    ai_json["cover_photo_ref"] = dest_photo_ref

    # ── Phase 4: fill deterministic fields BEFORE hydration ───────────────────
    fill_deterministic_fields(ai_json.get("days") or [], currency_code, safe_budget)

    # ── Phase 5: batch hydration ──────────────────────────────────────────────
    async with (trace.track("phase5_enrichment") if trace else _noop()):
        await batch_hydrate_days(ai_json.get("days") or [], body.destination, place_lookup)

    # ── Confidence scoring ────────────────────────────────────────────────────
    days = ai_json.get("days") or []
    annotate_confidence(days)
    dropped = filter_low_confidence(days)
    if dropped:
        logger.debug("[plan_trip] Dropped %d low-confidence activities: %s", len(dropped), dropped)

    # ── Trip health check + auto-fix ─────────────────────────────────────────
    health = trip_health_check(days)
    fixed  = auto_fix_trip(days)
    ai_json["trip_quality"] = {**health, "auto_fixed": fixed}
    if health["issues"]:
        logger.debug("[plan_trip] Health issues fixed: %s", health["issues"])

    # ── Route optimisation per day ─────────────────────────────────────────────
    for day in days:
        if day.get("activities") and len(day["activities"]) >= 2:
            day["activities"] = nearest_neighbor_sort(day["activities"])

    # ── Deterministic time assignment — must run AFTER route sort ─────────────
    # Sorts activities by category+best_time window: viewpoints at dawn/dusk,
    # cafes at lunch, treks at early morning. Time-correctness > route order.
    assign_activity_times(days)

    ai_json["currency"] = symbol
    ai_json["trip_intelligence"] = compute_trip_intelligence(ai_json, safe_budget, body.days, ci["ceiling"])

    # ── Summary log ───────────────────────────────────────────────────────────
    _ti = ai_json.get("trip_intelligence") or {}
    acts_total = sum(len(d.get("activities") or []) for d in (ai_json.get("days") or []))
    logger.info(
        "[plan_trip] %s (%dd, %s) | %d activities | Pace: %s | Efficiency: %s%%",
        body.destination, body.days, safe_budget,
        acts_total, _ti.get("pace"), _ti.get("travel_efficiency_score"),
    )
    if trace:
        logger.debug(trace.summary())

    # ── Phase 6: DB writes (fire-and-forget) ──────────────────────────────────
    asyncio.create_task(_db_writes(db, user_id, body, ai_json))

    return ai_json


async def _db_writes(db, user_id: str, body: PlanTripRequest, itinerary: dict) -> None:
    try:
        trip_start = datetime.fromisoformat(body.start_date) if body.start_date else datetime.now()
        trip_end = datetime.fromisoformat(body.end_date) if body.end_date else trip_start + timedelta(days=body.days)
        if not body.skip_user_trips:
            await asyncio.get_event_loop().run_in_executor(None, lambda: (
                db.table("internal_trip_logs").insert({
                    "user_id": user_id,
                    "destination": body.destination,
                    "days": body.days,
                    "itinerary_data": itinerary,
                }).execute()
            ))
    except Exception as e:
        logger.warning("[plan_trip] DB write error (non-fatal): %s", e)


from contextlib import asynccontextmanager

@asynccontextmanager
async def _noop():
    yield
