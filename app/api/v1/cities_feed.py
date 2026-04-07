"""
GET /api/v1/cities-feed?page=1

Returns paginated city cards from internal_trip_logs.
Falls back to seed cities when DB data is sparse.
No auth required.
"""
from fastapi import APIRouter, Query
from app.db.client import get_db
from app.services.places import extract_hero_image
from app.config import settings

router = APIRouter()

PAGE_SIZE = 10

SEED_CITIES = [
    {"name": "Paris", "country": "France", "image": "https://images.unsplash.com/photo-1502602898657-3e91760cbb34?w=800&q=80"},
    {"name": "New York", "country": "USA", "image": "https://images.unsplash.com/photo-1496442226666-8d4d0e62e6e9?w=800&q=80"},
    {"name": "Barcelona", "country": "Spain", "image": "https://images.unsplash.com/photo-1539037116277-4db20889f2d4?w=800&q=80"},
    {"name": "Santorini", "country": "Greece", "image": "https://images.unsplash.com/photo-1570077188670-e3a8d69ac5ff?w=800&q=80"},
    {"name": "Dubai", "country": "UAE", "image": "https://images.unsplash.com/photo-1512453979798-5ea266f8880c?w=800&q=80"},
    {"name": "Kyoto", "country": "Japan", "image": "https://images.unsplash.com/photo-1528360983277-13d401cdc186?w=800&q=80"},
    {"name": "Bali", "country": "Indonesia", "image": "https://images.unsplash.com/photo-1537996194471-e657df975ab4?w=800&q=80"},
    {"name": "Amsterdam", "country": "Netherlands", "image": "https://images.unsplash.com/photo-1534351590666-13e3e96b5017?w=800&q=80"},
    {"name": "Rome", "country": "Italy", "image": "https://images.unsplash.com/photo-1529260830199-42c24126f198?w=800&q=80"},
    {"name": "Tokyo", "country": "Japan", "image": "https://images.unsplash.com/photo-1540959733332-eab4deabeeaf?w=800&q=80"},
]


@router.get("/cities-feed")
async def cities_feed(page: int = Query(1, ge=1)):
    db = get_db()
    maps_key = settings.google_maps_api_key or ""

    rows_res = (
        db.table("internal_trip_logs")
        .select("destination, itinerary_data, created_at")
        .not_.is_("destination", "null")
        .not_.is_("itinerary_data", "null")
        .order("created_at", desc=True)
        .limit(300)
        .execute()
    )
    rows = rows_res.data or []

    # Deduplicate by destination (case-insensitive), keep latest
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = row["destination"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(row)

    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    paginated = unique[start:end]
    total = len(unique)

    cities = []
    for row in paginated:
        dest: str = row["destination"]
        image = extract_hero_image(row.get("itinerary_data") or {}, maps_key)
        name, country = dest, ""
        if "," in dest:
            idx = dest.rfind(",")
            name = dest[:idx].strip()
            country = dest[idx + 1:].strip()
        cities.append({"name": name, "country": country, "image": image})

    # Pad first page with seed cities if sparse
    if page == 1 and len(cities) < PAGE_SIZE:
        present = {c["name"].lower() for c in cities}
        seeds = [s for s in SEED_CITIES if s["name"].lower() not in present]
        cities += seeds[: PAGE_SIZE - len(cities)]

    return {
        "cities": cities,
        "pagination": {
            "page": page,
            "pageSize": PAGE_SIZE,
            "total": total,
            "hasMore": total > end,
        },
    }
