"""
POST /api/v1/save-trip
Authorization: Bearer <supabase_jwt>
Body: { "tripData": {...}, "destinationName": str, "is_public": bool }

Saves an AI-generated trip to the DB (trips + itinerary_items tables)
and updates the user preference graph.
"""
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any
from app.deps.auth import require_user
from app.db.client import get_db

router = APIRouter()


class SaveTripRequest(BaseModel):
    tripData: Any
    destinationName: str
    is_public: bool = True


@router.post("/save-trip")
async def save_trip(body: SaveTripRequest, user=Depends(require_user)):
    db = get_db()
    trip_data = body.tripData or {}
    destination_name = body.destinationName
    user_id = user.id

    days = trip_data.get("days") or []
    total_days = len(days) or 3
    today = date.today()
    end_dt = today + timedelta(days=total_days)

    total_cost = sum(
        act.get("estimated_cost") or 0
        for day in days
        for act in (day.get("activities") or [])
    )

    # Insert trip row
    trip_res = (
        db.table("trips")
        .insert({
            "user_id": user_id,
            "title": trip_data.get("trip_name") or f"Trip to {destination_name}",
            "destination": trip_data.get("destination") or destination_name,
            "start_date": str(today),
            "end_date": str(end_dt),
            "budget": total_cost or None,
            "currency": trip_data.get("currency") or "USD",
            "description": trip_data.get("destination_description"),
            "image_url": trip_data.get("hero_image_url") or trip_data.get("image"),
            "itinerary_data": trip_data,
            "is_public": body.is_public,
        })
        .execute()
    )
    if not trip_res.data:
        raise HTTPException(status_code=500, detail="Failed to save trip")

    trip_id = trip_res.data[0]["id"]

    # Insert itinerary_items for map querying
    item_rows = []
    for day in days:
        for act in (day.get("activities") or []):
            if not act.get("lat") or not act.get("lng"):
                continue
            item_rows.append({
                "trip_id": trip_id,
                "day_number": day.get("day"),
                "time": act.get("time"),
                "title": act.get("title"),
                "description": act.get("description"),
                "location_lat": act["lat"],
                "location_lng": act["lng"],
                "cost": act.get("estimated_cost") or 0,
                "type": (act.get("type") or "activity").lower(),
                "metadata": {
                    "location": act.get("location"),
                    "duration_minutes": act.get("duration_minutes") or 60,
                    "tags": act.get("tags") or [],
                },
            })
    if item_rows:
        db.table("itinerary_items").insert(item_rows).execute()

    # Update preference graph
    existing_res = (
        db.table("user_preference_graph")
        .select("total_trips, destination_affinity")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    existing = existing_res.data or {}
    current_trips = int(existing.get("total_trips") or 0)
    affinity: dict = existing.get("destination_affinity") or {}
    affinity[destination_name] = int(affinity.get(destination_name) or 0) + 10

    db.table("user_preference_graph").upsert(
        {
            "user_id": user_id,
            "total_trips": current_trips + 1,
            "destination_affinity": affinity,
            "last_updated": date.today().isoformat(),
        },
        on_conflict="user_id",
    ).execute()

    return {"tripId": trip_id, "ok": True}
