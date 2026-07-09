"""
Hostels / Workstations API.

  GET  /api/v1/hostels                              list the master catalog (filterable)
  GET  /api/v1/hostels/{hostel_id}                  one hostel's full static detail
  GET  /api/v1/hostels/{hostel_id}/live             live rooms + pricing for a date range
  GET  /api/v1/hostels/{hostel_id}/availability     per-night beds-left calendar for one rate plan

The catalog (static: amenities, images, description, policies…) is served from
the `hostels` table, populated by scripts/scrape_gostops.py. Live prices are
never stored — the /live route proxies the source API in real time so quotes are
always fresh, mirroring how trip_journey keeps hotel prices out of the master
table.

Auth: read routes are public (catalog is not user data); flip to require_user if
the section should be gated.
"""
import logging
import re
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request

from app.core.limiter import limiter
from app.db.client import get_db
from app.services.gostops import client as gostops_client
from app.services.gostops import normalize as gostops_normalize

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hostels", tags=["hostels"])

# Live/availability params are forwarded into the upstream goSTOPS URL, so they
# must be validated to exact shapes here — a raw client string would otherwise
# let callers inject extra query params into our outbound request.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RATEPLAN_RE = re.compile(r"^\d{1,32}$")
_MAX_NIGHTS = 30
_MAX_DAYS_AHEAD = 365


def _validate_stay(check_in: str, check_out: str) -> None:
    """422 unless the dates are real, ordered, and a bookable-sized window."""
    if not _DATE_RE.match(check_in) or not _DATE_RE.match(check_out):
        raise HTTPException(status_code=422, detail="Dates must be YYYY-MM-DD")
    try:
        d_in, d_out = date.fromisoformat(check_in), date.fromisoformat(check_out)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid calendar date")
    if d_out <= d_in:
        raise HTTPException(status_code=422, detail="check_out must be after check_in")
    if (d_out - d_in).days > _MAX_NIGHTS:
        raise HTTPException(status_code=422, detail=f"Stay too long (max {_MAX_NIGHTS} nights)")
    today = date.today()
    if d_in < today - timedelta(days=1) or d_in > today + timedelta(days=_MAX_DAYS_AHEAD):
        raise HTTPException(status_code=422, detail="check_in out of bookable range")

# A hostel is addressable by either its UUID or its (human-readable) slug. We
# pick the lookup column by shape so a slug never hits the uuid column — that
# would raise a Postgres 22P02 syntax error instead of a clean 404.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _key_column(key: str) -> str:
    return "id" if _UUID_RE.match(key) else "slug"

# Live-pricing dispatch by source. Each entry maps a stored hostel row to its
# source's live fetch + normaliser. Adding Hosteller/Zostel later = one entry.
_LIVE_SOURCES = {
    "gostops": (gostops_client.live_rooms, gostops_normalize.normalize_rooms),
}

# Per-night availability-calendar dispatch, same pattern as _LIVE_SOURCES.
_AVAILABILITY_SOURCES = {
    "gostops": (gostops_client.availability, gostops_normalize.normalize_availability),
}


def _resolve_hostel(hostel_key: str) -> dict:
    """Load the source/slug for a hostel by uuid or slug; raises HTTP 404/500."""
    try:
        db = get_db()
        res = (
            db.table("hostels")
            .select("source,slug,name")
            .eq(_key_column(hostel_key), hostel_key)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        logger.exception("[hostels] lookup failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load hostel")
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="Hostel not found")
    return rows[0]

# Columns returned by the list endpoint — the card/list view. The heavy blobs
# (images, faqs, guidelines…) are only sent on the detail route.
_LIST_COLUMNS = (
    "id,source,source_hostel_id,slug,name,full_name,category,brand,city,state,"
    "destination,lat,lon,card_image,banner_image,tags,amenities,starting_price,"
    "pvtroom_starting_price,currency,check_in_time,check_out_time,is_active"
)


@router.get("")
@limiter.limit("60/minute")
def list_hostels(
    request: Request,
    city: str | None = Query(None, description="Filter by city name (case-insensitive)"),
    category: str | None = Query(None, description="workstation | coliving | group"),
    source: str | None = Query(None, description="gostops | hosteller | zostel"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    try:
        db = get_db()
        q = db.table("hostels").select(_LIST_COLUMNS).eq("is_active", True)
        if city:
            q = q.ilike("city", city)
        if category:
            q = q.eq("category", category)
        if source:
            q = q.eq("source", source)
        q = q.order("starting_price", desc=False).range(offset, offset + limit - 1)
        res = q.execute()
        rows = res.data or []
        return {"count": len(rows), "hostels": rows}
    except Exception as exc:
        logger.exception("[hostels] list failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load hostels")


@router.get("/{hostel_key}")
@limiter.limit("60/minute")
def get_hostel(request: Request, hostel_key: str):
    try:
        db = get_db()
        # NB: use limit(1) rather than maybe_single() — postgrest-py's
        # maybe_single() raises APIError(204) on zero rows instead of returning
        # empty, which would 500 on any unknown id. Resolve by uuid or slug.
        col = _key_column(hostel_key)
        res = db.table("hostels").select("*").eq(col, hostel_key).limit(1).execute()
        rows = res.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Hostel not found")
        return rows[0]
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[hostels] detail failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load hostel")


@router.get("/{hostel_key}/live")
@limiter.limit("20/minute")
async def get_hostel_live(
    request: Request,
    hostel_key: str,
    check_in: str = Query(..., description="YYYY-MM-DD"),
    check_out: str = Query(..., description="YYYY-MM-DD"),
):
    """Live room inventory + pricing for a stored hostel over a date range."""
    _validate_stay(check_in, check_out)
    row = _resolve_hostel(hostel_key)
    source, slug = row.get("source"), row.get("slug")
    dispatch = _LIVE_SOURCES.get(source)
    if not dispatch or not slug:
        raise HTTPException(
            status_code=422,
            detail=f"Live pricing not available for source '{source}'",
        )

    fetch, normalise = dispatch
    try:
        data = await fetch(slug, check_in, check_out)
    except gostops_client.GostopsError as exc:
        logger.warning("[hostels] live fetch failed for %s: %s", slug, exc)
        raise HTTPException(status_code=502, detail="Live pricing temporarily unavailable")

    return normalise(data, slug)


@router.get("/{hostel_key}/availability")
@limiter.limit("20/minute")
async def get_hostel_availability(
    request: Request,
    hostel_key: str,
    check_in: str = Query(..., description="YYYY-MM-DD"),
    check_out: str = Query(..., description="YYYY-MM-DD"),
    rateplan_id: str = Query(..., description="rateplan_id from a /live variant"),
):
    """Per-night beds-left + price calendar for one rate plan of a hostel.

    The rateplan_id comes from the `variants` of the /live response; call this
    when the user picks a specific room/plan to confirm night-by-night stock.
    """
    _validate_stay(check_in, check_out)
    if not _RATEPLAN_RE.match(rateplan_id):
        raise HTTPException(status_code=422, detail="Invalid rateplan_id")
    row = _resolve_hostel(hostel_key)
    source, slug = row.get("source"), row.get("slug")
    dispatch = _AVAILABILITY_SOURCES.get(source)
    if not dispatch or not slug:
        raise HTTPException(
            status_code=422,
            detail=f"Availability not supported for source '{source}'",
        )

    fetch, normalise = dispatch
    try:
        data = await fetch(slug, check_in, check_out, rateplan_id)
    except gostops_client.GostopsError as exc:
        logger.warning("[hostels] availability fetch failed for %s: %s", slug, exc)
        raise HTTPException(status_code=502, detail="Availability temporarily unavailable")

    return normalise(data, slug, rateplan_id)
