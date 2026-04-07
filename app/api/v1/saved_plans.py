"""
GET /api/v1/saved-plans
Authorization: Bearer <supabase_jwt>

Returns all saved plans for the authenticated user:
trips, tonight_plans, night_plans (legacy), reel_scans.
"""
from fastapi import APIRouter, Depends
from app.deps.auth import require_user
from app.db.client import get_db

router = APIRouter()


@router.get("/saved-plans")
async def saved_plans(user=Depends(require_user)):
    db = get_db()
    user_id = user.id

    trips_res = (
        db.table("trips")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    tonight_res = (
        db.table("tonight_plans")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    legacy_night_res = (
        db.table("night_plans")
        .select("*, night_plan_candidates(count)")
        .eq("creator_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    reel_res = (
        db.table("reel_scans")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )

    return {
        "trips": trips_res.data or [],
        "tonightPlans": tonight_res.data or [],
        "nightPlans": legacy_night_res.data or [],
        "reelScans": reel_res.data or [],
    }
