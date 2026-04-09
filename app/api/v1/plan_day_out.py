"""
POST /api/v1/plan-day-out
Authorization: Bearer <supabase_jwt>
Body: { "location": str, "vibe": str, "companion": str, "budget": str, "date": str (ISO) }

Calls Vertex AI Gemini to suggest 5 venues for a day/night out,
enriches with Google Places, logs to DB, returns top 3.
"""
import asyncio
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any
import httpx

from app.deps.auth import require_user
from app.db.client import get_db
from app.services.vertex import vertex_generate, extract_json
from app.services.places import enrich_venue

# Shared client for weather API calls
_http_client = httpx.AsyncClient(timeout=8.0, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))

router = APIRouter()

MODEL = "gemini-2.5-flash"


class DayOutRequest(BaseModel):
    location: str
    vibe: str
    companion: str | None = None
    budget: str | None = None
    date: str | None = None  # ISO date string


async def _fetch_weather(location: str) -> dict | None:
    try:
        geo = await _http_client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        geo.raise_for_status()
        results = geo.json().get("results") or []
        if not results:
            return None
        lat, lng = results[0]["latitude"], results[0]["longitude"]

        wx = await _http_client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lng,
                "current": "temperature_2m,weather_code,wind_speed_10m,precipitation",
                "timezone": "auto",
            },
        )
        wx.raise_for_status()
        cur = wx.json().get("current", {})

        def _desc(code: int) -> str:
            if code == 0: return "Clear sky"
            if code <= 3: return "Partly cloudy"
            if code <= 49: return "Foggy"
            if code <= 59: return "Drizzle"
            if code <= 69: return "Rainy"
            if code <= 79: return "Snowy"
            if code <= 99: return "Thunderstorm"
            return "Clear"

        return {
            "temp_c": cur.get("temperature_2m"),
            "condition": _desc(cur.get("weather_code", 0)),
            "wind_kph": cur.get("wind_speed_10m"),
            "precip_mm": cur.get("precipitation", 0),
        }
    except Exception:
        return None


@router.post("/plan-day-out")
async def plan_day_out(body: DayOutRequest, user=Depends(require_user)):
    db = get_db()
    user_id = user.id

    plan_date = datetime.fromisoformat(body.date) if body.date else datetime.now()
    month_name = plan_date.strftime("%B")
    day_name = plan_date.strftime("%A")
    month_idx = plan_date.month
    season = (
        "Spring" if 3 <= month_idx <= 5
        else "Summer" if 6 <= month_idx <= 8
        else "Autumn" if 9 <= month_idx <= 11
        else "Winter"
    )
    date_context = (
        f"Date: {day_name}, {plan_date.day} {month_name} {plan_date.year} ({season}). "
        "Consider weekend vs weekday crowd levels, seasonal events, and current weather."
    )

    # Phase 1: parallel — log search + fetch weather
    search_insert = (
        db.table("tonight_searches")
        .insert({
            "user_id": user_id,
            "location": body.location,
            "vibe": body.vibe,
            "companion": body.companion,
            "budget": body.budget,
            "search_params": {"location": body.location, "vibe": body.vibe, "companion": body.companion, "budget": body.budget, "date": body.date},
        })
        .execute()
    )
    search_log = (search_insert.data or [{}])[0]

    weather = await _fetch_weather(body.location)

    weather_prompt = "Weather: unknown."
    if weather:
        weather_prompt = f"Current weather in {body.location}: {weather['condition']}, {weather['temp_c']}°C, wind {weather['wind_kph']} km/h."
        if (weather.get("precip_mm") or 0) > 0.5:
            weather_prompt += " It's raining — prefer indoor venues."

    # Phase 2: Gemini
    prompt = f"""You are an elite local day-out curator. Suggest exactly 5 REAL, VERIFIED venues for an exceptional day out, then rank them.

Location: {body.location}
Vibe: {body.vibe}
Companion: {body.companion or 'friends'}
Budget: {body.budget or 'medium'}
{weather_prompt}
{date_context}

STRICT RULES:
1. All venues MUST be real, well-known places that exist in {body.location} — use exact establishment names.
2. Vary venue types across the 5 (e.g. cafe + rooftop bar + park + restaurant + gallery).
3. Venues should be geographically clustered — walkable or a short ride between them.
4. match_score: vibe compatibility score 70-100. Be precise and varied (e.g. 94, 87, 82). Not all the same.
5. match: true for the single highest match_score venue only.
6. whyFit.desc MUST contain the exact whyFit.title phrase for UI highlighting.
7. seasonal_tip must be specific to {month_name} {plan_date.year}.

Return ONLY valid JSON with NO markdown:
{{
  "candidates": [
    {{
      "id": "venue_1",
      "name": "Exact Real Venue Name",
      "type": "Cafe|Restaurant|Park|Museum|Bar|Mall|Market|Gallery|Activity|Rooftop|Beach|Club",
      "rating": 4.5,
      "location": "Exact neighborhood or area, {body.location}",
      "price": "$ | $$ | $$$ | $$$$",
      "match": false,
      "match_score": 94,
      "seasonal_tip": "Specific tip for visiting in {month_name}",
      "whyFit": {{
        "title": "2-4 word hook",
        "desc": "One engaging sentence containing the exact title phrase explaining why this fits the {body.vibe} vibe."
      }}
    }}
  ],
  "message": "One punchy sentence overview of this perfect day out plan."
}}"""

    raw = await vertex_generate(prompt, response_mime_type="application/json")
    parsed = extract_json(raw)

    # Sort by match_score, keep top 3, enforce single match:true
    candidates: list[dict] = sorted(
        parsed.get("candidates") or [],
        key=lambda c: c.get("match_score") or 0,
        reverse=True,
    )[:3]
    for i, c in enumerate(candidates):
        c["match"] = i == 0

    # Phase 3: enrich with Google Places
    enriched = await asyncio.gather(*[enrich_venue(v, body.location) for v in candidates])
    enriched = list(enriched)

    parsed["candidates"] = enriched
    if weather:
        parsed["weather"] = weather
    parsed["search_params"] = {"location": body.location, "vibe": body.vibe, "companion": body.companion, "budget": body.budget, "date": body.date}

    # Phase 4: DB writes
    plan_res = (
        db.table("night_plans")
        .insert({
            "creator_id": user_id,
            "location": body.location,
            "vibe": body.vibe,
            "date": body.date or plan_date.date().isoformat(),
            "status": "voting",
        })
        .execute()
    )
    plan_id = ((plan_res.data or [{}])[0]).get("id")

    log_rows: list = [
        db.table("internal_tonight_logs").insert({
            "user_id": user_id,
            "search_id": (search_log or {}).get("id"),
            "location": body.location,
            "vibe": body.vibe,
            "candidates": enriched,
            "weather": weather,
        }).execute()
    ]
    if plan_id:
        log_rows += [
            db.table("night_plan_candidates").insert({
                "plan_id": plan_id,
                "place_name": c["name"],
                "place_details": c,
                "votes_count": 0,
            }).execute()
            for c in enriched
        ]

    return parsed
