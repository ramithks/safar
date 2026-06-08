"""
GET    /api/v1/users/stats                →  trip count, upcoming trips
GET    /api/v1/users/trips                →  user's saved trips list
GET    /api/v1/users/trips/{id}           →  single trip
PUT    /api/v1/users/trips/{id}           →  update trip itinerary
DELETE /api/v1/users/trips/{id}           →  delete trip
GET    /api/v1/users/trips/{id}/journey   →  transport/journey data for a trip

Profile read/update now happens directly from the client via the Supabase SDK
(RLS policies `profiles_read_all` / `profiles_update_own` cover it) — no
backend round-trip needed for plain CRUD on `profiles`.
"""
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


@router.get("/stats")
async def get_stats(user=Depends(require_user)):
    try:
        db = get_db()
        today = date.today().isoformat()

        total_res = (
            db.table("trips")
            .select("id", count="exact")
            .eq("user_id", str(user.id))
            .execute()
        )
        total = total_res.count or 0

        upcoming_res = (
            db.table("trips")
            .select("id")
            .eq("user_id", str(user.id))
            .gte("start_date", today)
            .execute()
        )
        upcoming = len(upcoming_res.data or [])

        return {
            "trips_planned": total,
            "upcoming_trips": upcoming,
        }
    except Exception as exc:
        logger.warning("[users] get_stats error: %s", exc)
        return {"trips_planned": 0, "upcoming_trips": 0}


@router.get("/trips")
async def get_saved_trips(user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("trips")
            .select("*")
            .eq("user_id", str(user.id))
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.warning("[users] get_trips error: %s", exc)
        return []


@router.get("/trips/{trip_id}")
async def get_trip_by_id(trip_id: str, user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("trips")
            .select("*")
            .eq("id", trip_id)
            .eq("user_id", str(user.id))
            .maybe_single()
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Trip not found")
        return res.data
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[users] get_trip_by_id error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch trip")


class UpdateTripItineraryRequest(BaseModel):
    itinerary_data: dict


@router.put("/trips/{trip_id}")
async def update_trip_itinerary(trip_id: str, body: UpdateTripItineraryRequest, user=Depends(require_user)):
    db = get_db()
    try:
        db.table("trips").update({"itinerary_data": body.itinerary_data}).eq("id", trip_id).eq("user_id", str(user.id)).execute()
        return {"status": "ok"}
    except Exception as exc:
        logger.error("[users] update_trip_itinerary error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to update trip")


@router.get("/trips/{trip_id}/journey")
async def get_trip_journey(trip_id: str, user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("trip_journey")
            .select("*")
            .eq("trip_id", trip_id)
            .eq("user_id", str(user.id))
            .maybe_single()
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Journey not found")
        return res.data
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[users] get_trip_journey error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch journey")


@router.delete("/trips/{trip_id}", status_code=204)
async def delete_trip(trip_id: str, user=Depends(require_user)):
    db = get_db()
    try:
        db.table("trips").delete().eq("id", trip_id).eq("user_id", str(user.id)).execute()
    except Exception as exc:
        logger.error("[users] delete_trip error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete trip")
