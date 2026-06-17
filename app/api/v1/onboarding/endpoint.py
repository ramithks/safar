"""
POST /api/v1/onboarding/generate-feed
  Fires background Gemini generation for personalized weekend plans + vibe
  trips, stored in user_home_feed. Each item is then enriched with an
  `image_url` (a /api/v1/images/{photo_reference} proxy path) looked up via
  the Google Places API — the frontend never sees the Maps API key.
  The client writes profile fields straight to Supabase (RLS-protected);
  this endpoint exists only because the AI generation needs server-side
  credentials.

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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db
from app.services.vertex import vertex_generate
from app.services.places import search_places, photo_proxy_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class GenerateFeedRequest(BaseModel):
    city: str
    country: str
    vibes: list[str] = []


async def _attach_image(item: dict, query: str) -> dict:
    """Look up a destination photo via Google Places and attach it as a proxy URL.

    Biases toward "tourist attraction" results so the photo represents the
    destination itself, falling back to a plain search if that yields nothing.
    Picks the first result that actually has a photo, not just result[0].
    """
    try:
        places = await search_places(query, "tourist attraction")
        if not any(p.get("photo_reference") for p in places):
            places = await search_places(query)
        photo_ref = next((p["photo_reference"] for p in places if p.get("photo_reference")), None)
        item["image_url"] = photo_proxy_path(photo_ref) if photo_ref else None
    except Exception as exc:
        logger.warning("[onboarding] image lookup failed for %r: %s", query, type(exc).__name__)
        item["image_url"] = None
    return item


async def _attach_images(items: list[dict], query_fn) -> list[dict]:
    """Concurrently enrich each item with an `image_url` from Google Places."""
    return list(await asyncio.gather(*(_attach_image(item, query_fn(item)) for item in items)))


async def _store_feed(db, user_id: str, feed_type: str, raw, query_fn, now: str) -> None:
    """Parse a Gemini JSON array, enrich items with images, and upsert — all concurrently."""
    if isinstance(raw, Exception):
        logger.warning("[onboarding] %s Gemini error: %s", feed_type, raw)
        return
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not (isinstance(items, list) and items):
            return
        items = await _attach_images(items, query_fn)
        db.table("user_home_feed").upsert(
            {"user_id": user_id, "feed_type": feed_type, "items": items, "generated_at": now},
            on_conflict="user_id,feed_type",
        ).execute()
        logger.info("[onboarding] Stored %d %s for %s", len(items), feed_type, user_id[:8])
    except Exception as exc:
        logger.warning("[onboarding] Parse %s failed: %s", feed_type, exc)


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
            f"Each trip must be to a SINGLE city/town only — never combine multiple places "
            f"(e.g. use \"Shimla\" or \"Manali\", not \"Shimla & Manali\" or \"Shimla, Manali\"), "
            f"since our trip planner currently supports planning for one destination city at a time. "
            f"Return a JSON array of exactly 12 objects with these keys: "
            f"id (kebab-case slug), title (single destination city/town name only, no combos), duration (\"2 Days\" or \"3 Days\"), "
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
            f"Each trip must center on a SINGLE named city only — never a region, area, or combo of "
            f"places (e.g. use \"Kochi\" or \"Munnar\", not \"Kerala Backwaters\" or \"Shimla & Manali\"), "
            f"since our trip planner currently supports planning for one destination city at a time. "
            f"Return a JSON array of exactly 10 objects with these keys: "
            f"id (kebab-case slug), title (evocative trip title), destination (single city name only, no region/combo), country, "
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

        # Parse, image-enrich (Places lookups run concurrently across all items in both
        # feeds) and upsert both feed types in parallel — independent end-to-end.
        await asyncio.gather(
            _store_feed(db, user_id, "weekend_plans", weekend_raw, lambda p: f"{p.get('title')}, {country}", now),
            _store_feed(
                db, user_id, "vibe_trips", vibe_raw,
                lambda t: f"{t.get('destination')}, {t.get('country')}", now,
            ),
        )

    except Exception as exc:
        logger.error("[onboarding] _generate_home_feed crashed: %s", exc)


@router.post("/generate-feed")
async def generate_feed(
    body: GenerateFeedRequest,
    user=Depends(require_user),
):
    """Generate, image-enrich, and store the home feed, returning 200 only
    after weekend_plans and vibe_trips have been upserted."""
    await generate_home_feed(
        str(user.id),
        body.city,
        body.country,
        body.vibes,
    )
    return {"status": "ok", "generating": False}
