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
import time
from datetime import datetime, timezone

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db
from app.services.vertex import muscle as vertex_muscle
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
        # TimeoutError et al. stringify to "" — log the type so the cause is visible.
        logger.warning("[onboarding] %s Gemini error: %s: %s", feed_type, type(raw).__name__, raw)
        sentry_sdk.capture_exception(raw)
        return
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
        if not (isinstance(items, list) and items):
            return
        t_img = time.perf_counter()
        items = await _attach_images(items, query_fn)
        logger.info(
            "[onboarding] %s image enrichment done: %d items in %.1fs",
            feed_type, len(items), time.perf_counter() - t_img,
        )
        db.table("user_home_feed").upsert(
            {"user_id": user_id, "feed_type": feed_type, "items": items, "generated_at": now},
            on_conflict="user_id,feed_type",
        ).execute()
        logger.info("[onboarding] Stored %d %s for %s", len(items), feed_type, user_id[:8])
    except Exception as exc:
        logger.warning("[onboarding] Parse %s failed: %s", feed_type, exc)
        sentry_sdk.capture_exception(exc)


async def _generate_with_retry(prompt: str, *, attempts: int = 2, per_attempt_timeout: float = 45.0):
    """Call Gemini, retrying if a single attempt doesn't finish in time.

    Gemini 2.5 Flash legitimately needs ~10-40s for a 10-12 item structured
    generation, so the wall-clock cap is generous — too tight a cap (the old
    12s) just killed requests that were about to succeed. vertex_generate now
    rotates regions internally on 429/503, so a single attempt absorbs rate
    limits; this layer only retries genuine timeouts / connection errors.

    Each attempt is capped at `per_attempt_timeout` seconds (wall-clock). On
    failure we retry up to `attempts` times before re-raising the last error.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await asyncio.wait_for(
                vertex_muscle(
                    prompt,
                    response_mime_type="application/json",
                    temperature=0.7,
                    timeout=per_attempt_timeout,
                ),
                # Slightly looser than the inner per-request timeout so httpx's
                # own timeout fires first with a cleaner error when it can.
                timeout=per_attempt_timeout + 5,
            )
        except Exception as exc:  # includes asyncio.TimeoutError — retry on any failure
            last_exc = exc
            logger.warning(
                "[onboarding] Gemini attempt %d/%d failed (%s) — retrying",
                attempt + 1, attempts, type(exc).__name__,
            )
    raise last_exc if last_exc else RuntimeError("generation failed")


async def generate_home_feed(user_id: str, city: str, country: str, vibes: list[str]) -> None:
    """Background: call Gemini for weekend plans + vibe trips, upsert into user_home_feed."""
    uid_short = user_id[:8]
    t0 = time.perf_counter()
    logger.info("[onboarding] START user=%s city=%s, %s vibes=%s", uid_short, city, country, vibes)
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

        logger.info("[onboarding] Calling Gemini (parallel) for user=%s", uid_short)
        t_gemini = time.perf_counter()
        weekend_raw, vibe_raw = await asyncio.gather(
            _generate_with_retry(weekend_prompt),
            _generate_with_retry(vibe_prompt),
            return_exceptions=True,
        )
        logger.info(
            "[onboarding] Gemini responded in %.1fs for user=%s | weekend_ok=%s vibe_ok=%s",
            time.perf_counter() - t_gemini, uid_short,
            not isinstance(weekend_raw, Exception),
            not isinstance(vibe_raw, Exception),
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

        logger.info(
            "[onboarding] DONE user=%s city=%s total=%.1fs",
            uid_short, city, time.perf_counter() - t0,
        )

    except Exception as exc:
        logger.error(
            "[onboarding] FAILED user=%s city=%s elapsed=%.1fs error=%s",
            uid_short, city, time.perf_counter() - t0, exc,
            exc_info=True,
        )
        sentry_sdk.capture_exception(exc)


@router.post("/generate-feed")
async def generate_feed(
    body: GenerateFeedRequest,
    user=Depends(require_user),
):
    """Generate, image-enrich, and store the home feed, returning 200 only
    after weekend_plans and vibe_trips have been upserted."""
    logger.info(
        "[onboarding] /generate-feed called user=%s city=%s, %s vibes=%s",
        str(user.id)[:8], body.city, body.country, body.vibes,
    )
    try:
        await generate_home_feed(
            str(user.id),
            body.city,
            body.country,
            body.vibes,
        )
    except Exception as exc:
        logger.error("[onboarding] generate_feed unhandled: %s", exc, exc_info=True)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(status_code=500, detail="Feed generation failed.") from exc
    return {"status": "ok", "generating": False}
