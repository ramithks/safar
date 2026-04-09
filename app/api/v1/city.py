"""
POST /api/v1/city
Body: { "cityName": str, "action": "discovery" | "content", "offset": int, "limit": int }

Returns city discovery data (hero, must-visit, stories, itineraries) or paginated content.
No auth required.
"""
from fastapi import APIRouter
from pydantic import BaseModel
from app.db.client import get_db

router = APIRouter()

DISCOVERY_VIDEOS_LIMIT = 10
CONTENT_VIDEOS_PAGE_SIZE = 20
ITINERARIES_LIMIT = 15


class CityRequest(BaseModel):
    cityName: str
    action: str = "discovery"
    offset: int = 0
    limit: int = CONTENT_VIDEOS_PAGE_SIZE


def _place_emoji(type_: str) -> str:
    t = (type_ or "").lower()
    if any(w in t for w in ["temple", "shrine", "church", "mosque"]): return "⛩️"
    if any(w in t for w in ["beach", "coast", "island"]): return "🏖️"
    if any(w in t for w in ["food", "restaurant", "cafe", "market"]): return "🍜"
    if any(w in t for w in ["museum", "gallery", "art"]): return "🏛️"
    if any(w in t for w in ["park", "garden", "nature", "forest"]): return "🌿"
    if any(w in t for w in ["shop", "mall", "bazaar"]): return "🛍️"
    if any(w in t for w in ["bar", "club", "night", "lounge"]): return "🎉"
    if any(w in t for w in ["palace", "fort", "castle"]): return "🏯"
    return "📍"


def _activity_emoji(activity: str) -> str:
    a = activity.lower()
    if any(w in a for w in ["beach", "surf", "swim"]): return "🏖️"
    if any(w in a for w in ["food", "culinary", "dining"]): return "🍜"
    if any(w in a for w in ["night", "bar", "party"]): return "🎉"
    if any(w in a for w in ["temple", "culture", "heritage", "history"]): return "🎭"
    if any(w in a for w in ["nature", "hike", "trek", "trail"]): return "🌿"
    if any(w in a for w in ["shop", "market", "bazaar"]): return "🛍️"
    if any(w in a for w in ["adventure", "sport", "extreme"]): return "🧗"
    if any(w in a for w in ["wellness", "yoga", "spa", "relax"]): return "🧘"
    if any(w in a for w in ["city", "urban", "skyline"]): return "🌆"
    return "✈️"


DEFAULT_STORIES = [
    {"id": "s0", "label": "Culture", "emoji": "🎭"},
    {"id": "s1", "label": "Food", "emoji": "🍜"},
    {"id": "s2", "label": "Nature", "emoji": "🌿"},
    {"id": "s3", "label": "Nightlife", "emoji": "🎉"},
    {"id": "s4", "label": "Shopping", "emoji": "🛍️"},
    {"id": "s5", "label": "Adventure", "emoji": "🧗"},
]


@router.post("/city")
async def city(body: CityRequest):
    db = get_db()
    city_name = body.cityName

    if body.action == "content":
        offset = max(0, body.offset)
        limit = min(50, max(1, body.limit))

        videos_res, city_res = await _parallel(
            lambda: db.table("videos")
            .select("id, video_id, title, thumbnail_url, channel_name, vibes, activity_types, ai_summary, view_count, city_name")
            .ilike("city_name", f"%{city_name}%")
            .eq("processing_state", "completed")
            .not_.is_("thumbnail_url", "null")
            .order("view_count", desc=True)
            .range(offset, offset + limit - 1)
            .execute(),
            lambda: db.table("cities").select("id, name, country").ilike("name", f"%{city_name}%").limit(1).execute(),
        )

        city_videos = videos_res.data or []
        city_record = (city_res.data or [None])[0]

        videos = []
        for v in city_videos:
            vibes = v.get("vibes") or []
            acts = v.get("activity_types") or []
            category = (vibes[0] if vibes else (acts[0] if acts else "travel")).replace("_", " ")
            videos.append({
                "id": v["id"],
                "videoId": v.get("video_id") or v["id"],
                "title": v.get("title") or "Travel Video",
                "thumbnailUrl": v.get("thumbnail_url"),
                "author": f"@{v['channel_name'].replace(' ', '')}" if v.get("channel_name") else "@TravBuddy",
                "category": category[0].upper() + category[1:],
                "viewCount": v.get("view_count") or 0,
                "cityName": v.get("city_name"),
            })

        itineraries = await _fetch_itineraries(db, city_record, city_videos, city_name, ITINERARIES_LIMIT, filter_thumb=True)

        return {
            "videos": videos,
            "itineraries": itineraries,
            "pagination": {"offset": offset, "limit": limit, "hasMore": len(videos) >= limit},
        }

    # ── DISCOVERY ─────────────────────────────────────────────────────────────
    videos_res, city_res = await _parallel(
        lambda: db.table("videos")
        .select("id, title, thumbnail_url, places_mentioned, activity_types, vibes, ai_summary, view_count")
        .ilike("city_name", f"%{city_name}%")
        .eq("processing_state", "completed")
        .not_.is_("thumbnail_url", "null")
        .order("view_count", desc=True)
        .limit(DISCOVERY_VIDEOS_LIMIT)
        .execute(),
        lambda: db.table("cities").select("id, name, country").ilike("name", f"%{city_name}%").limit(1).execute(),
    )

    city_videos = videos_res.data or []
    city_record = (city_res.data or [None])[0]

    # Aggregate must-visit places
    place_map: dict[str, dict] = {}
    for v in city_videos:
        for p in (v.get("places_mentioned") or []):
            if not p.get("name") or p.get("sentiment") == "negative":
                continue
            key = p["name"].lower()
            if key in place_map:
                place_map[key]["count"] += 1
            else:
                place_map[key] = {"name": p["name"], "type": p.get("type") or "Attraction", "count": 1}

    must_visit = [
        {"id": f"p{i}", "name": p["name"], "desc": p["type"], "icon": _place_emoji(p["type"])}
        for i, p in enumerate(sorted(place_map.values(), key=lambda x: -x["count"])[:6])
    ]

    # Stories from activity types + vibes
    label_set: set[str] = set()
    for v in city_videos:
        for label in list(v.get("activity_types") or []) + list(v.get("vibes") or []):
            label_set.add(label)
    stories = [
        {"id": f"s{i}", "label": l[0].upper() + l[1:], "emoji": _activity_emoji(l)}
        for i, l in enumerate(list(label_set)[:8])
    ] or DEFAULT_STORIES

    itineraries = await _fetch_itineraries(db, city_record, city_videos, city_name, 5, filter_thumb=False)

    return {
        "cityName": city_name,
        "description": (city_videos[0].get("ai_summary") if city_videos else None)
            or f"Explore the best of {city_name} with our curated travel guides.",
        "heroImage": city_videos[0].get("thumbnail_url") if city_videos else None,
        "mustVisit": must_visit,
        "stories": stories,
        "itineraries": itineraries,
    }


async def _parallel(fn1, fn2):
    """Run two sync Supabase calls concurrently in a thread pool."""
    import asyncio
    loop = asyncio.get_event_loop()
    r1, r2 = await asyncio.gather(
        loop.run_in_executor(None, fn1),
        loop.run_in_executor(None, fn2),
    )
    return r1, r2


async def _fetch_itineraries(db, city_record, city_videos, city_name, limit, filter_thumb: bool) -> list:
    if city_record:
        it_res = (
            db.table("video_itineraries")
            .select("id, duration_days, trip_style, structured_plan, video_id")
            .eq("city_id", city_record["id"])
            .not_.is_("structured_plan", "null")
            .limit(limit)
            .execute()
        )
        it_rows = it_res.data or []
        video_ids = [r["video_id"] for r in it_rows if r.get("video_id")]
        it_video_map: dict = {}
        if video_ids:
            v_res = db.table("videos").select("id, thumbnail_url").in_("id", video_ids).execute()
            it_video_map = {v["id"]: v for v in (v_res.data or [])}
    else:
        video_ids = [v["id"] for v in city_videos if v.get("id")]
        if video_ids:
            it_res = (
                db.table("video_itineraries")
                .select("id, duration_days, trip_style, structured_plan, video_id")
                .in_("video_id", video_ids)
                .not_.is_("structured_plan", "null")
                .limit(limit)
                .execute()
            )
            it_rows = it_res.data or []
            it_video_map = {v["id"]: v for v in city_videos}
        else:
            return []

    result = []
    for i, it in enumerate(it_rows):
        if not it.get("structured_plan"):
            continue
        plan = it["structured_plan"]
        thumb = it_video_map.get(it.get("video_id"), {}).get("thumbnail_url")
        if filter_thumb and not thumb:
            continue
        style = it.get("trip_style") or ""
        style_label = style[0].upper() + style[1:] if style else "Curated"
        result.append({
            "id": it.get("id") or f"i{i}",
            "title": f"{style_label} {city_name} – {it['duration_days']} Days",
            "desc": (plan.get("general_tips") or [None])[0] or f"A curated {it['duration_days']}-day itinerary for {city_name}.",
            "days": str(it["duration_days"]),
            "tripStyle": it.get("trip_style"),
            "thumbnailUrl": thumb,
            "videoId": it.get("video_id"),
            "rating": "4.8",
        })
    return result
