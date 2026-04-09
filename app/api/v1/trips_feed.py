"""
GET /api/v1/trips-feed?page=1&pageSize=10

Returns paginated community trips from internal_trip_logs with author profiles.
No auth required.
"""
from fastapi import APIRouter, Query
from app.db.client import get_db
from app.services.places import extract_hero_image
from app.config import settings

router = APIRouter()

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


@router.get("/trips-feed")
async def trips_feed(
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, alias="pageSize"),
):
    db = get_db()
    maps_key = settings.google_maps_api_key or ""

    start = (page - 1) * page_size
    end = start + page_size - 1

    res = (
        db.table("internal_trip_logs")
        .select("id, destination, days, itinerary_data, user_id, created_at", count="exact")
        .not_.is_("itinerary_data", "null")
        .order("created_at", desc=True)
        .range(start, end)
        .execute()
    )
    rows = res.data or []
    total = res.count or 0

    # Batch-fetch author profiles
    user_ids = list({r["user_id"] for r in rows if r.get("user_id")})
    profile_map: dict = {}
    if user_ids:
        p_res = (
            db.table("profiles")
            .select("id, avatar_url, full_name")
            .in_("id", user_ids)
            .execute()
        )
        for p in p_res.data or []:
            profile_map[p["id"]] = {"avatar_url": p.get("avatar_url"), "full_name": p.get("full_name")}

    trips = []
    for t in rows:
        it = t.get("itinerary_data") or {}
        days = len(it.get("days", [])) or t.get("days") or 0
        hero = extract_hero_image(it, maps_key)
        profile = profile_map.get(t.get("user_id"), {})
        trips.append({
            "id": t["id"],
            "title": it.get("trip_name") or f"{days} Days in {t['destination']}",
            "city": t["destination"],
            "days": str(days),
            "thumbnailUrl": hero,
            "hero_image_url": hero,
            "imageUrl": hero,
            "authorId": t.get("user_id"),
            "authorName": profile.get("full_name"),
            "authorAvatar": profile.get("avatar_url"),
            "createdAt": t.get("created_at"),
            "itineraryData": it,
            "destination": t["destination"],
            "destination_description": it.get("destination_description"),
            "destination_video": it.get("destination_video"),
        })

    return {
        "trips": trips,
        "pagination": {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "hasMore": total > end + 1,
        },
    }
