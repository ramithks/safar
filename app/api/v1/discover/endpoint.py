"""
GET /api/v1/discover/weekend-plans  →  personalized weekend trips from user_home_feed
GET /api/v1/discover/vibe-trips     →  personalized vibe trips from user_home_feed

Both return [] if the user hasn't completed onboarding yet or feed hasn't generated.
The frontend falls back to static curated data in that case.
"""
import logging

from fastapi import APIRouter, Depends

from app.deps.auth import require_user
from app.db.client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discover", tags=["discover"])


@router.get("/weekend-plans")
def get_weekend_plans(user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("user_home_feed")
            .select("items")
            .eq("user_id", str(user.id))
            .eq("feed_type", "weekend_plans")
            .maybe_single()
            .execute()
        )
        return res.data["items"] if (res and res.data) else []
    except Exception as exc:
        logger.warning("[discover] weekend-plans error: %s", exc)
        return []


@router.get("/vibe-trips")
def get_vibe_trips(user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("user_home_feed")
            .select("items")
            .eq("user_id", str(user.id))
            .eq("feed_type", "vibe_trips")
            .maybe_single()
            .execute()
        )
        return res.data["items"] if (res and res.data) else []
    except Exception as exc:
        logger.warning("[discover] vibe-trips error: %s", exc)
        return []
