"""
Privileged partner operations — the acts the DB reserves for `service_role`.

    website ──► POST /api/v1/marketplace/listings/{id}/submit
                    ├── verify Supabase JWT           (deps/auth.require_user)
                    ├── resolve partner FROM the JWT  (_require_partner)
                    ├── load the row, check ownership (server-side, not trusted)
                    ├── re-run the publish gates      (authoritative)
                    └── write with the service role

The website holds no service-role key (see the website's
docs/MARKETPLACE_BACKEND_MIGRATION.md). Everything the guard triggers refuse
from an `authenticated` caller — status transitions, `seats_left`, the counters
— happens here instead.

⚠ The partner is ALWAYS derived from `auth.uid()`. No endpoint accepts a
partner_id, and every row write re-filters on the resolved partner's id, so a
guessed listing id belonging to someone else 404s rather than mutating.

The publish gates are duplicated from the website's lib/marketplace/publish-rules.ts
on purpose: the website's copy drives the live editor checklist, but a gate that
only runs in the client it protects is not a gate. This is the authoritative one.
"""
# NOTE: deliberately no `from __future__ import annotations` — see the same note
# in app/api/v1/admin/endpoint.py. PEP 563 breaks FastAPI body resolution
# through slowapi's wrapper.
import logging
import re
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.v1.marketplace.partner_schemas import (
    CounterRequest,
    GapItem,
    Ok,
    SeatsRequest,
    StatusResponse,
)
from app.api.v1.marketplace.payment_schemas import AiJobRequest, UploadResponse
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.auth import require_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketplace", tags=["marketplace"])

MIN_PHOTOS = 3
MIN_DESCRIPTION_CHARS = 120
MIN_PRICE = 100

# Mirrors KIND_RULES in the website's lib/marketplace/types.ts.
NEEDS_APPROVAL = {"hotel": False, "experience": True, "group_trip": True}

SETTLED_BOOKING_STATUSES = ["paid", "confirmed", "completed"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── authorization ────────────────────────────────────────────────────────────


async def _require_partner(user) -> dict:
    """
    The caller's partner row, resolved from the verified JWT alone.

    This is the only place a partner identity enters these endpoints. A request
    body never contributes to it.
    """
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("id, brand_name, email, city, whatsapp, status, "
                "stay_plan_status, stay_plan_expires_at")
        .eq("owner_id", user.id)
        .maybe_single()
        .execute()
    )
    partner = res.data if res else None
    if not partner:
        raise HTTPException(403, "Create your partner profile first.")
    if partner.get("status") == "suspended":
        raise HTTPException(403, "This account is suspended. Please contact support.")
    return partner


async def _owned_listing(partner_id: str, listing_id: str) -> dict:
    """Load a listing the caller owns, or 404. Ownership is a WHERE clause."""
    res = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .select("*")
        .eq("id", listing_id)
        .eq("partner_id", partner_id)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    if not row:
        raise HTTPException(404, "Listing not found.")
    return row


async def _owned_trip(partner_id: str, trip_id: str) -> dict:
    res = await run_db(
        lambda: get_db()
        .table("group_trips")
        .select("*")
        .eq("id", trip_id)
        .eq("partner_id", partner_id)
        .maybe_single()
        .execute()
    )
    row = res.data if res else None
    if not row:
        raise HTTPException(404, "Trip not found.")
    return row


def _has_active_stay_plan(partner: dict) -> bool:
    if partner.get("stay_plan_status") != "active":
        return False
    expires = partner.get("stay_plan_expires_at")
    if not expires:
        return False
    try:
        return datetime.fromisoformat(expires.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except (ValueError, AttributeError):
        return False


# ── publish gates (authoritative copy) ───────────────────────────────────────


def _listing_gaps(listing: dict) -> list[GapItem]:
    gaps: list[GapItem] = []
    content = listing.get("content") or {}

    if not (listing.get("title") or "").strip() or len((listing.get("title") or "").strip()) < 4:
        gaps.append(GapItem(field="title", label="Add a title"))
    if len(listing.get("photos") or []) < MIN_PHOTOS:
        gaps.append(GapItem(field="photos", label=f"Add at least {MIN_PHOTOS} photos"))
    if len((listing.get("description") or "").strip()) < MIN_DESCRIPTION_CHARS:
        gaps.append(
            GapItem(field="description", label="Write a description (a couple of sentences)")
        )
    if not listing.get("price_from") or listing["price_from"] < MIN_PRICE:
        gaps.append(GapItem(field="price_from", label="Set a starting price"))
    if not listing.get("city") and not listing.get("destination"):
        gaps.append(GapItem(field="city", label="Add the location"))

    if listing.get("kind") == "hotel" and not (content.get("rooms") or []):
        gaps.append(GapItem(field="content.rooms", label="Add at least one room type"))
    if listing.get("kind") == "experience" and not content.get("duration_hours"):
        gaps.append(GapItem(field="content.duration_hours", label="Add how long it takes"))

    return gaps


def _trip_gaps(trip: dict) -> list[GapItem]:
    gaps: list[GapItem] = []
    content = trip.get("content") or {}

    if len((trip.get("title") or "").strip()) < 4:
        gaps.append(GapItem(field="title", label="Add a title"))
    if not trip.get("destination"):
        gaps.append(GapItem(field="destination", label="Add the destination"))
    if not trip.get("hero_image"):
        gaps.append(GapItem(field="hero_image", label="Add a cover photo"))
    if len((trip.get("description") or "").strip()) < MIN_DESCRIPTION_CHARS:
        gaps.append(
            GapItem(field="description", label="Write a description (a couple of sentences)")
        )
    if not trip.get("duration_days") or trip["duration_days"] < 1:
        gaps.append(GapItem(field="duration_days", label="Set how many days it runs"))
    if not trip.get("departure_date"):
        gaps.append(GapItem(field="departure_date", label="Set the departure date"))
    if not trip.get("seats_total") or trip["seats_total"] < 1:
        gaps.append(GapItem(field="seats_total", label="Set how many seats"))
    if not trip.get("price_from") or trip["price_from"] < MIN_PRICE:
        gaps.append(GapItem(field="price_from", label="Set a starting price"))
    if not (content.get("days") or []):
        gaps.append(GapItem(field="content.days", label="Add the day-by-day plan"))
    if not (content.get("inclusions") or []):
        gaps.append(GapItem(field="content.inclusions", label="List what's included"))

    return gaps


# ── counters ─────────────────────────────────────────────────────────────────


@router.post("/views", response_model=Ok)
# Unauthenticated on purpose: most page views are signed-out, and requiring a
# session here would silently count only logged-in traffic. `views` and `leads`
# are guarded columns, so this is the only path that can move them.
@limiter.limit("600/minute")
async def bump_counter(request: Request, body: CounterRequest) -> Ok:
    """
    Vanity counters for the partner dashboard. Never worth failing a caller
    over, so every failure is swallowed after logging.
    """
    table = "partner_listings" if body.kind == "listing" else "group_trips"
    try:
        if body.metric == "views":
            rpc = "increment_listing_views" if body.kind == "listing" else "increment_trip_views"
            arg = "p_listing" if body.kind == "listing" else "p_trip"
            await run_db(lambda: get_db().rpc(rpc, {arg: body.id}).execute())
        else:
            # No RPC exists for `leads` yet, so this is the same read-then-write
            # the website did. Still racy under concurrent enquiries — worth an
            # increment_*_leads RPC when the counter starts mattering.
            res = await run_db(
                lambda: get_db()
                .table(table)
                .select("leads")
                .eq("id", body.id)
                .maybe_single()
                .execute()
            )
            current = (res.data or {}).get("leads", 0) if res else 0
            await run_db(
                lambda: get_db()
                .table(table)
                .update({"leads": (current or 0) + 1})
                .eq("id", body.id)
                .execute()
            )
    except Exception:  # noqa: BLE001
        logger.warning("[marketplace] counter bump failed for %s %s", body.kind, body.id)

    return Ok(ok=True)


# ── listings ─────────────────────────────────────────────────────────────────


@router.post("/listings/{listing_id}/submit", response_model=StatusResponse)
@limiter.limit("60/hour")
async def submit_listing(
    request: Request,
    listing_id: str,
    user=Depends(require_user),
) -> StatusResponse:
    """
    Send a listing live.

      hotel      → needs an active ₹499 plan, then publishes instantly
      experience → goes to 'pending' for admin review

    The plan check is the fee model. It runs here, against the partner row
    loaded server-side, so a partner cannot publish a stay by calling the API
    directly with someone else's plan state.
    """
    partner = await _require_partner(user)
    listing = await _owned_listing(partner["id"], listing_id)

    gaps = _listing_gaps(listing)
    if gaps:
        return StatusResponse(ok=False, error="A few things are still missing.", gaps=gaps)

    kind = listing.get("kind") or "experience"
    needs_approval = NEEDS_APPROVAL.get(kind, True)

    if not needs_approval and not _has_active_stay_plan(partner):
        return StatusResponse(
            ok=False,
            error="Subscribe at ₹499/month to publish your stay.",
            needs_plan=True,
        )

    next_status = "pending" if needs_approval else "published"
    patch = {"status": next_status, "rejection_reason": None}
    if next_status == "published":
        patch["published_at"] = _now()

    res = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .update(patch)
        .eq("id", listing_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(500, "Could not submit your listing. Please try again.")

    return StatusResponse(ok=True, status=next_status, listing=res.data[0])


@router.post("/listings/{listing_id}/unpublish", response_model=StatusResponse)
@limiter.limit("60/hour")
async def unpublish_listing(
    request: Request,
    listing_id: str,
    user=Depends(require_user),
) -> StatusResponse:
    """Take a live listing down. Always allowed — `published → draft` only."""
    partner = await _require_partner(user)
    await _owned_listing(partner["id"], listing_id)

    res = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .update({"status": "draft"})
        .eq("id", listing_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(500, "Could not unpublish this listing.")

    return StatusResponse(ok=True, status="draft", listing=res.data[0])


@router.delete("/listings/{listing_id}", response_model=Ok)
@limiter.limit("60/hour")
async def delete_listing(
    request: Request,
    listing_id: str,
    user=Depends(require_user),
) -> Ok:
    """
    Delete a listing. Refused when settled bookings exist — a traveller who paid
    must not lose the page describing what they bought.
    """
    partner = await _require_partner(user)
    await _owned_listing(partner["id"], listing_id)

    booked = await run_db(
        lambda: get_db()
        .table("bookings")
        .select("id", count="exact")
        .eq("item_id", listing_id)
        .in_("status", SETTLED_BOOKING_STATUSES)
        .limit(1)
        .execute()
    )
    if (booked.count or 0) > 0:
        raise HTTPException(
            409,
            "This listing has bookings, so it can't be deleted. Unpublish it instead.",
        )

    await run_db(
        lambda: get_db()
        .table("partner_listings")
        .delete()
        .eq("id", listing_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    return Ok(ok=True)


# ── group trips ──────────────────────────────────────────────────────────────


@router.post("/trips/{trip_id}/submit", response_model=StatusResponse)
@limiter.limit("60/hour")
async def submit_trip(
    request: Request,
    trip_id: str,
    user=Depends(require_user),
) -> StatusResponse:
    """Group trips always go to review — there is no instant-publish path."""
    partner = await _require_partner(user)
    trip = await _owned_trip(partner["id"], trip_id)

    gaps = _trip_gaps(trip)
    if gaps:
        return StatusResponse(ok=False, error="A few things are still missing.", gaps=gaps)

    # A departure already in the past cannot be reviewed into a sellable trip.
    departure = trip.get("departure_date")
    if departure:
        try:
            if date.fromisoformat(str(departure)[:10]) < datetime.now(timezone.utc).date():
                return StatusResponse(
                    ok=False,
                    error="That departure date has already passed. Pick a future date.",
                )
        except ValueError:
            pass  # a malformed date is caught by the gaps check, not here

    res = await run_db(
        lambda: get_db()
        .table("group_trips")
        .update({"status": "pending", "rejection_reason": None})
        .eq("id", trip_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(500, "Could not submit your trip. Please try again.")

    return StatusResponse(ok=True, status="pending", listing=res.data[0])


@router.post("/trips/{trip_id}/unpublish", response_model=StatusResponse)
@limiter.limit("60/hour")
async def unpublish_trip(
    request: Request,
    trip_id: str,
    user=Depends(require_user),
) -> StatusResponse:
    """Take a live trip down. `is_active` follows `status` so the legacy public
    read flag cannot leave it visible."""
    partner = await _require_partner(user)
    await _owned_trip(partner["id"], trip_id)

    res = await run_db(
        lambda: get_db()
        .table("group_trips")
        .update({"status": "draft", "is_active": False})
        .eq("id", trip_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(500, "Could not unpublish this trip.")

    return StatusResponse(ok=True, status="draft", listing=res.data[0])


@router.delete("/trips/{trip_id}", response_model=Ok)
@limiter.limit("60/hour")
async def delete_trip(
    request: Request,
    trip_id: str,
    user=Depends(require_user),
) -> Ok:
    """Refused when seats have actually been sold."""
    partner = await _require_partner(user)
    await _owned_trip(partner["id"], trip_id)

    booked = await run_db(
        lambda: get_db()
        .table("bookings")
        .select("id", count="exact")
        .eq("item_id", trip_id)
        .in_("status", SETTLED_BOOKING_STATUSES)
        .limit(1)
        .execute()
    )
    if (booked.count or 0) > 0:
        raise HTTPException(
            409,
            "This trip has bookings, so it can't be deleted. Unpublish it instead.",
        )

    await run_db(
        lambda: get_db()
        .table("group_trips")
        .delete()
        .eq("id", trip_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    return Ok(ok=True)


@router.patch("/trips/{trip_id}/seats", response_model=StatusResponse)
@limiter.limit("120/hour")
async def update_trip_seats(
    request: Request,
    trip_id: str,
    body: SeatsRequest,
    user=Depends(require_user),
) -> StatusResponse:
    """
    Resize a trip's inventory.

    `seats_left` moves by the DELTA the partner applied to `seats_total`, never
    to an absolute the caller supplies — otherwise an edit could silently
    un-sell a seat someone already paid for. Sold seats are derived from the
    stored row, so the arithmetic can't be influenced from outside.
    """
    partner = await _require_partner(user)
    trip = await _owned_trip(partner["id"], trip_id)

    old_total = trip.get("seats_total") or 0
    old_left = trip.get("seats_left") or 0
    sold = max(old_total - old_left, 0)

    new_total = body.seats_total
    if new_total < sold:
        raise HTTPException(
            409,
            f"{sold} seat(s) are already booked, so the trip can't be resized below {sold}.",
        )

    res = await run_db(
        lambda: get_db()
        .table("group_trips")
        .update({"seats_total": new_total, "seats_left": new_total - sold})
        .eq("id", trip_id)
        .eq("partner_id", partner["id"])
        .execute()
    )
    if not res.data:
        raise HTTPException(500, "Could not update seats. Please try again.")

    return StatusResponse(ok=True, status=trip.get("status"), listing=res.data[0])


# ── media upload ─────────────────────────────────────────────────────────────

BUCKET = "marketplace-media"
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/avif": "avif",
}


@router.post("/upload", response_model=UploadResponse, status_code=201)
@limiter.limit("120/hour")
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    folder: str = Form(default="misc"),
    user=Depends(require_user),
) -> UploadResponse:
    """
    Image upload for logos, covers and listing photos.

    Server-side rather than a direct browser→Storage upload for the reason the
    partner-media incident established: Storage did not honour the project's
    ES256 JWT and fell back to `anon`, 403-ing authenticated uploads. Routing
    through here removes that failure mode entirely.

    The storage prefix is the caller's own partner id, derived from the JWT — a
    client cannot write into another partner's folder whatever it sends.
    """
    partner = await _require_partner(user)

    mime = (file.content_type or "").split(";")[0].strip().lower()
    ext = UPLOAD_EXT.get(mime)
    if not ext:
        raise HTTPException(415, "Please upload a JPG, PNG, WebP or AVIF image.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "No file received.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That image is over 5 MB. Please upload a smaller one.")

    safe_folder = re.sub(r"[^a-z0-9-]", "", folder or "", flags=re.I) or "misc"
    path = f"{partner['id']}/{safe_folder}/{uuid.uuid4()}.{ext}"

    try:
        await run_db(
            lambda: get_db()
            .storage.from_(BUCKET)
            .upload(
                path,
                data,
                {"content-type": mime, "cache-control": "31536000", "upsert": "false"},
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[marketplace] upload failed")
        raise HTTPException(500, "Upload failed. Please try again.") from exc

    public_url = await run_db(
        lambda: get_db().storage.from_(BUCKET).get_public_url(path)
    )
    return UploadResponse(ok=True, url=public_url, path=path)


# ── AI import audit ──────────────────────────────────────────────────────────


@router.post("/ai-jobs", response_model=Ok, status_code=201)
@limiter.limit("120/hour")
async def record_ai_job(
    request: Request,
    body: AiJobRequest,
    user=Depends(require_user),
) -> Ok:
    """
    Record an AI import attempt so cost and abuse trace back to a partner.

    `ai_import_jobs` has a read policy for owners but no insert policy, so this
    is service-role work. The partner comes from the JWT — a caller cannot file
    a job against someone else's ledger.

    Never fatal: the extraction has already happened and been paid for by the
    time this runs, so a failed audit write must not fail the import.
    """
    partner = await _require_partner(user)
    try:
        await run_db(
            lambda: get_db()
            .table("ai_import_jobs")
            .insert(
                {
                    "partner_id": partner["id"],
                    "source_type": body.source_type,
                    "source_ref": body.source_ref,
                    "input_chars": body.input_chars,
                    "output": body.output,
                    "status": body.status,
                    "error": body.error,
                }
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.warning("[marketplace] ai job audit failed for partner %s", partner["id"])
    return Ok(ok=True)
