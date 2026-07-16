"""
Public hotel catalog + SEO profile API.

  GET /api/v1/hotels            list SEO-ready hotels (for the index + sitemap)
  GET /api/v1/hotels/{slug}     one hotel's public profile (name, photos, the AI
                                one_liner/seo_summary/personality tags, best_for,
                                plus semantically-similar stays for internal links)

Reads the `hotels` master table joined with `hotel_profiles` (built by the reco
workers, docs/recommendation_engine.md Part 3/5). Served via the service-role
client so it works regardless of RLS — this is public SEO content, not user data,
so the routes are unauthenticated (like /hostels). Live prices are never here; the
workspace fetches those in real time.
"""
import logging
import re

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.limiter import limiter
from app.db.client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hotels", tags=["hotels"])

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# Card/list columns — light. Heavy blobs (seo_summary, all photos) only on detail.
# LEFT-joined to profiles: every slugged hotel is a live page (master-table data
# renders it; the LLM profile only enriches) — "launch ALL pages" means the
# list/sitemap must not hide the not-yet-profiled 4,400.
_LIST_COLUMNS = (
    "id,slug,name,city,destination,star_rating,review_score,review_count,"
    "property_type,photos,"
    "hotel_profiles(one_liner,personality_tags,best_for)"
)
_DETAIL_COLUMNS = (
    "id,slug,name,address,city,destination,lat,lon,star_rating,review_score,"
    "review_count,review_label,property_type,amenities,photos,booking_url,map_url,"
    "description,subscores,landmarks,"
    "hotel_profiles(one_liner,seo_summary,personality_tags,highlights,best_for,"
    "not_for,confidence,refreshed_at)"
)


def _flatten(row: dict) -> dict:
    """Lift the embedded hotel_profiles object up onto the hotel row."""
    prof = row.pop("hotel_profiles", None)
    if isinstance(prof, list):
        prof = prof[0] if prof else None
    if isinstance(prof, dict):
        row["profile"] = prof
    else:
        row["profile"] = None
    return row


@router.get("")
# High ceiling on purpose: the website's static build fetches every city hub +
# all sitemap shards from ONE IP in a burst — these are cheap indexed reads.
@limiter.limit("600/minute")
def list_hotels(
    request: Request,
    city: str | None = Query(None, description="Filter by city (case-insensitive)"),
    destination: str | None = Query(None, description="Filter by destination"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """SEO-ready hotels only (inner-joined to a profile, slug present)."""
    try:
        db = get_db()
        q = (
            db.table("hotels").select(_LIST_COLUMNS)
            .not_.is_("slug", "null")
        )
        if city:
            q = q.ilike("city", city)
        if destination:
            q = q.ilike("destination", destination)
        q = q.order("review_score", desc=True).range(offset, offset + limit - 1)
        rows = [_flatten(r) for r in (q.execute().data or [])]
        return {"count": len(rows), "hotels": rows}
    except Exception as exc:  # noqa: BLE001
        logger.exception("[hotels] list failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load hotels")


@router.get("/cities")
@limiter.limit("120/minute")
def list_cities(request: Request, min_hotels: int = Query(2, ge=1)):
    """Distinct cities with slugged hotels + counts — powers the /hotels/city
    hub pages and their sitemap entries. Cached upstream (ISR); the scan is a
    single-column read."""
    try:
        db = get_db()
        cities: dict[str, int] = {}
        offset = 0
        while True:
            rows = (db.table("hotels").select("city")
                    .not_.is_("slug", "null").not_.is_("city", "null")
                    .range(offset, offset + 999).execute().data or [])
            for r in rows:
                key = (r.get("city") or "").strip()
                if len(key) >= 2:
                    cities[key.title()] = cities.get(key.title(), 0) + 1
            if len(rows) < 1000:
                break
            offset += 1000
        out = sorted(
            ({"city": c, "count": n} for c, n in cities.items() if n >= min_hotels),
            key=lambda x: -x["count"],
        )
        return {"count": len(out), "cities": out}
    except Exception as exc:  # noqa: BLE001
        logger.exception("[hotels] cities failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load cities")


@router.get("/{slug}")
@limiter.limit("60/minute")
def get_hotel(request: Request, slug: str):
    try:
        db = get_db()
        col = "id" if _UUID_RE.match(slug) else "slug"
        res = (
            db.table("hotels").select(_DETAIL_COLUMNS)
            .eq(col, slug).limit(1).execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[hotels] detail failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load hotel")
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Hotel not found")
    hotel = _flatten(rows[0])
    hotel["similar"] = _similar_hotels(db, hotel["id"], hotel.get("destination"))
    return hotel


def _similar_hotels(db, hotel_id: str, destination: str | None) -> list[dict]:
    """Semantically-nearest stays (internal links / 'similar stays'). Best-effort:
    empty when the hotel isn't embedded yet or pgvector is unavailable."""
    try:
        emb_res = (
            db.table("hotel_embeddings").select("embedding")
            .eq("hotel_id", hotel_id).limit(1).execute()
        )
        if not emb_res.data:
            return []
        emb = emb_res.data[0]["embedding"]
        if isinstance(emb, str):
            import json
            emb = json.loads(emb)
        rows = db.rpc("match_hotels", {
            "query_embedding": emb, "match_destination": destination, "match_count": 7,
        }).execute().data or []
        return [r for r in rows if str(r.get("hotel_id")) != str(hotel_id)][:6]
    except Exception as exc:  # noqa: BLE001
        logger.debug("[hotels] similar lookup failed: %s", exc)
        return []
