"""
POST /api/v1/onboarding/personalize
  Saves user profile to Supabase and fires background Gemini generation
  for personalized weekend plans + vibe trips stored in user_home_feed.

POST /api/v1/onboarding/skip
  Marks onboarding completed without generating feed content.

Required Supabase table (create once in dashboard):

  CREATE TABLE user_home_feed (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users,
    feed_type text NOT NULL,  -- 'weekend_plans' | 'vibe_trips'
    items jsonb NOT NULL DEFAULT '[]',
    generated_at timestamptz DEFAULT now(),
    UNIQUE (user_id, feed_type)
  );

  Also ensure profiles has: city text, vibes text[], social_links jsonb DEFAULT '{}'
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db
from app.services.vertex import vertex_generate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class PersonalizeRequest(BaseModel):
    city: str | None = None
    country: str | None = None
    vibes: list[str] = []
    styles: list[str] = []
    interests: list[str] = []
    name: str | None = None
    phone_number: str | None = None
    bio: str | None = None
    age: int | None = None
    gender: str | None = None
    budget: str | None = None


async def generate_home_feed(user_id: str, city: str, country: str, vibes: list[str]) -> None:
    """Background: call Gemini for weekend plans + vibe trips, upsert into user_home_feed."""
    try:
        db = get_db()
        vibe_str = ", ".join(vibes) if vibes else "Adventure, Nature, Culture"

        weekend_prompt = (
            f"You are an expert travel planner. Generate exactly 12 weekend getaway trips "
            f"from {city}, {country} that can be done in 2-3 days. "
            f"Include a diverse mix: hill stations, beaches, heritage sites, nature escapes, "
            f"adventure spots, spiritual places. "
            f"Return a JSON array of exactly 12 objects with these keys: "
            f"id (kebab-case slug), title (short destination name), duration (\"2 Days\" or \"3 Days\"), "
            f"cost (budget range per person, e.g. \"₹2,500–4,500\"), rating (float 4.0-4.9), "
            f"distance (travel time from {city}, e.g. \"5h drive\"), "
            f"category (one of: Mountains|Beaches|Heritage|Nature|Adventure|Spiritual|Lakes|Desert|City). "
            f"Return ONLY the JSON array, no explanation."
        )

        vibe_prompt = (
            f"You are an expert global travel curator. Generate exactly 10 personalized trip ideas "
            f"based on these travel vibes: {vibe_str}. The traveler is from {country}. "
            f"Exactly 5 must be national trips within {country}. "
            f"Exactly 5 must be international trips across different regions (Asia, Europe, Americas, Africa/Oceania). "
            f"Return a JSON array of exactly 10 objects with these keys: "
            f"id (kebab-case slug), title (evocative trip title), destination (city/region), country, "
            f"is_international (boolean), duration (\"3 Days\"/\"5 Days\"/\"7 Days\"), "
            f"budget (\"Budget-Friendly\"|\"Mid Budget\"|\"Premium\"|\"Luxury\"), "
            f"vibes (array of 2-3 strings from the input list), "
            f"description (one sentence, max 80 chars). "
            f"Return ONLY the JSON array, no explanation."
        )

        weekend_raw, vibe_raw = await asyncio.gather(
            vertex_generate(weekend_prompt, response_mime_type="application/json", temperature=0.7, timeout=60.0),
            vertex_generate(vibe_prompt, response_mime_type="application/json", temperature=0.7, timeout=60.0),
            return_exceptions=True,
        )

        now = datetime.now(timezone.utc).isoformat()

        if not isinstance(weekend_raw, Exception):
            try:
                plans = json.loads(weekend_raw) if isinstance(weekend_raw, str) else weekend_raw
                if isinstance(plans, list) and plans:
                    db.table("user_home_feed").upsert(
                        {"user_id": user_id, "feed_type": "weekend_plans", "items": plans, "generated_at": now},
                        on_conflict="user_id,feed_type",
                    ).execute()
                    logger.info("[onboarding] Stored %d weekend plans for %s", len(plans), user_id[:8])
            except Exception as exc:
                logger.warning("[onboarding] Parse weekend plans failed: %s", exc)
        else:
            logger.warning("[onboarding] Weekend plans Gemini error: %s", weekend_raw)

        if not isinstance(vibe_raw, Exception):
            try:
                trips = json.loads(vibe_raw) if isinstance(vibe_raw, str) else vibe_raw
                if isinstance(trips, list) and trips:
                    db.table("user_home_feed").upsert(
                        {"user_id": user_id, "feed_type": "vibe_trips", "items": trips, "generated_at": now},
                        on_conflict="user_id,feed_type",
                    ).execute()
                    logger.info("[onboarding] Stored %d vibe trips for %s", len(trips), user_id[:8])
            except Exception as exc:
                logger.warning("[onboarding] Parse vibe trips failed: %s", exc)
        else:
            logger.warning("[onboarding] Vibe trips Gemini error: %s", vibe_raw)

    except Exception as exc:
        logger.error("[onboarding] _generate_home_feed crashed: %s", exc)


@router.post("/personalize")
async def personalize(
    body: PersonalizeRequest,
    background_tasks: BackgroundTasks,
    user=Depends(require_user),
):
    db = get_db()

    updates: dict = {
        "onboarding_completed": True,
        "preferences": {
            "styles": body.styles,
            "interests": body.interests,
            **({"budget": body.budget} if body.budget else {}),
        },
    }
    if body.name:
        updates["full_name"] = body.name
    if body.phone_number:
        updates["phone_number"] = body.phone_number
    if body.bio:
        updates["bio"] = body.bio
    if body.age is not None:
        updates["age"] = body.age
    if body.gender:
        updates["gender"] = body.gender
    if body.country:
        updates["country"] = body.country
    if body.city:
        updates["city"] = body.city
    if body.vibes:
        updates["vibes"] = body.vibes

    try:
        db.table("profiles").upsert({"id": str(user.id), **updates}, on_conflict="id").execute()
    except Exception as exc:
        logger.error("[onboarding] Profile save failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save profile")

    # Fire background generation if we have location data
    if body.city and body.country:
        all_vibes = list(set((body.vibes or []) + (body.interests or []) + (body.styles or [])))
        background_tasks.add_task(
            generate_home_feed,
            str(user.id),
            body.city,
            body.country,
            all_vibes,
        )

    return {"status": "ok", "generating": bool(body.city and body.country)}


@router.post("/skip")
async def skip_onboarding(user=Depends(require_user)):
    db = get_db()
    try:
        db.table("profiles").update({"onboarding_completed": True}).eq("id", str(user.id)).execute()
    except Exception as exc:
        logger.warning("[onboarding] Skip update failed: %s", exc)
    return {"status": "ok"}
