"""
TravBuddy group-trip authoring.

Until now TravBuddy's own trips were hand-written JSON in the Supabase table
editor while partners got a real editor — the one remaining place where the
"same table, same template" promise wasn't matched by the tooling.

These endpoints create and edit rows with `partner_id = NULL`, which is what
makes a trip TravBuddy's. Everything else — the content contract, the public
template, the booking path — is identical to a partner's package, so the only
difference a traveller ever sees stays the "Organised by" line.

Same discipline as the rest of the admin API: passcode, validation, DB write,
audit row, event. No email is sent from here (nobody to notify — it's our own
trip), but the events still fire so an activity feed picks them up later.
"""
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.v1.admin.trip_schemas import (
    AdminTrip, PublishGap, TripDraft, TripSaved, TripValidation,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.admin import AdminIdentity, require_admin
from app.services import admin_audit
from app.services.events import emit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/trips", tags=["admin"])

RESERVED_SLUGS = {"new", "edit", "admin", "api", "trips"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:60].rstrip("-")
    return s or "trip"


async def _unique_slug(base: str) -> str:
    """First free slug. Immutable once set — it's a public URL."""
    root = _slugify(base)
    if root in RESERVED_SLUGS:
        root = f"{root}-trip"

    for n in range(1, 60):
        candidate = root if n == 1 else f"{root}-{n}"
        res = await run_db(
            lambda c=candidate: get_db().table("group_trips").select("id").eq("slug", c).execute()
        )
        if not (res.data if res else None):
            return candidate
    return f"{root}-{int(datetime.now(timezone.utc).timestamp())}"


def publish_gaps(row: dict) -> list[PublishGap]:
    """
    What's still missing before this can go live.

    Mirrors `tripPublishGaps()` on the website so a TravBuddy trip is held to
    exactly the bar a partner's is. A traveller pays upfront for a fixed
    departure, so the date, seats, day plan and inclusions are all required.
    """
    gaps: list[PublishGap] = []
    content = row.get("content") or {}

    def gap(field: str, label: str) -> None:
        gaps.append(PublishGap(field=field, label=label))

    if len((row.get("title") or "").strip()) < 4:
        gap("title", "Add a title")
    if not row.get("destination"):
        gap("destination", "Add the destination")
    if not row.get("hero_image"):
        gap("hero_image", "Add a cover photo")
    if len((row.get("description") or "").strip()) < 120:
        gap("description", "Write a description (a couple of sentences)")
    if not row.get("duration_days"):
        gap("duration_days", "Set how many days it runs")
    if not row.get("departure_date"):
        gap("departure_date", "Set the departure date")
    if not row.get("seats_total"):
        gap("seats_total", "Set how many seats")
    if not row.get("price_from"):
        gap("price_from", "Set a starting price")
    if not (content.get("days") or []):
        gap("content.days", "Add the day-by-day plan")
    if not (content.get("inclusions") or []):
        gap("content.inclusions", "List what's included")
    return gaps


def _normalise_content(draft: TripDraft) -> dict:
    """
    Write exactly the keys the public template reads.

    Pricing carries three representations that must agree: `value` (the
    template's sharing selector), `price` (the display string) and `amount`
    (the integer checkout and the AI matcher use). Deriving the first two from
    `amount` here means the page, Book Now and the recommendation can never
    quote different numbers.
    """
    content = dict(draft.content or {})

    tiers = []
    for tier in draft.pricing or []:
        if not tier.title or tier.amount <= 0:
            continue
        tiers.append({
            "title": tier.title,
            "value": tier.title,
            "price": f"₹{tier.amount:,}",
            "amount": tier.amount,
        })

    days = []
    for i, day in enumerate(draft.days or [], start=1):
        entry = {"n": day.n or str(i), "title": day.title}
        for slot in ("morning", "afternoon", "evening", "night"):
            value = getattr(day, slot, None)
            if value:
                entry[slot] = value
        if day.img:
            entry["img"] = day.img
        days.append(entry)

    # Every modelled key is written unconditionally, including when empty.
    # Writing them only "if truthy" would mean an admin who deletes the last
    # FAQ or clears the gallery sees the old values survive the save.
    content["days"] = days
    content["pricing"] = tiers
    content["inclusions"] = [
        {"emoji": i.emoji or "✅", "label": i.label} for i in (draft.inclusions or []) if i.label
    ]
    content["exclusions_note"] = draft.exclusions_note or None
    content["fine_print"] = [
        {"q": f.q, "a": f.a} for f in (draft.fine_print or []) if f.q and f.a
    ]
    content["images"] = {f"g{i + 1}": url for i, url in enumerate(draft.gallery or [])}

    # Unmodelled enrichment (reviews, stay_blurb, hotel_photos, highlights)
    # rides through untouched via `draft.content`, so hand-authored extras on
    # older rows survive an edit made through this editor.
    return content


def _row_patch(draft: TripDraft) -> dict:
    days = draft.duration_days
    nights = days - 1 if days and days > 1 else None
    return {
        "title": draft.title.strip(),
        "description": draft.description,
        "destination": draft.destination,
        "origin": draft.origin,
        "duration_days": days,
        # "3N/4D" is how every Indian operator writes it on their own material.
        "duration_label": (f"{nights}N/{days}D" if nights else (f"{days}D" if days else None)),
        "departure_date": draft.departure_date,
        "departure_label": draft.departure_label,
        "price_from": draft.price_from,
        "hero_image": draft.hero_image,
        "whatsapp_number": draft.whatsapp_number,
        "seo_title": draft.seo_title,
        # Included here (not just on create) so a trip can be switched between
        # WhatsApp leads and Razorpay checkout after it exists.
        "booking_mode": draft.booking_mode,
        "content": _normalise_content(draft),
    }


def _to_model(row: dict) -> AdminTrip:
    return AdminTrip(
        id=row["id"], slug=row["slug"], title=row["title"],
        description=row.get("description"), destination=row.get("destination"),
        origin=row.get("origin"), duration_days=row.get("duration_days"),
        departure_date=str(row["departure_date"]) if row.get("departure_date") else None,
        departure_label=row.get("departure_label"),
        seats_total=row.get("seats_total"), seats_left=row.get("seats_left"),
        price_from=row.get("price_from"), hero_image=row.get("hero_image"),
        whatsapp_number=row.get("whatsapp_number"), seo_title=row.get("seo_title"),
        content=row.get("content") or {}, status=row.get("status") or "draft",
        booking_mode=row.get("booking_mode") or "whatsapp",
        partner_id=row.get("partner_id"),
        is_travbuddy=row.get("partner_id") is None,
        views=row.get("views") or 0, leads=row.get("leads") or 0,
        updated_at=row.get("updated_at") or "",
        public_url=f"https://travbuddy.in/trips/{row['slug']}",
        gaps=publish_gaps(row),
    )


async def _load(trip_id: str) -> dict:
    res = await run_db(
        lambda: get_db().table("group_trips").select("*").eq("id", trip_id).maybe_single().execute()
    )
    if not res or not res.data:
        raise HTTPException(404, "Trip not found.")
    return res.data


# ── list / read ─────────────────────────────────────────────────────────────

@router.get("", response_model=list[AdminTrip])
@limiter.limit("120/minute")
async def list_trips(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
) -> list[AdminTrip]:
    """Every trip — TravBuddy's and partners' — since they share one table."""
    res = await run_db(
        lambda: get_db().table("group_trips").select("*")
        .order("updated_at", desc=True).limit(300).execute()
    )
    return [_to_model(r) for r in ((res.data if res else None) or [])]


@router.get("/{trip_id}", response_model=AdminTrip)
@limiter.limit("120/minute")
async def get_trip(
    request: Request,
    trip_id: str,
    _admin: AdminIdentity = Depends(require_admin),
) -> AdminTrip:
    return _to_model(await _load(trip_id))


# ── create / update ─────────────────────────────────────────────────────────

@router.post("", response_model=TripSaved)
@limiter.limit("60/minute")
async def create_trip(
    request: Request,
    body: TripDraft,
    admin: AdminIdentity = Depends(require_admin),
) -> TripSaved:
    """
    Create a TravBuddy trip.

    `partner_id` stays NULL — that's what makes it ours. Always starts as a
    draft: publishing is a separate, deliberate act.
    """
    slug = await _unique_slug(f"{body.title}-{body.destination or ''}")
    row = _row_patch(body) | {
        "slug": slug,
        "partner_id": None,
        "seats_total": body.seats_total,
        "seats_left": body.seats_total,
        "status": "draft",
        "is_active": False,
        "booking_mode": body.booking_mode,
        "commission_pct": 0,  # our own trip; there's no partner to take a cut from
    }

    try:
        # supabase-py's insert builder has no .select() — it already returns
        # the inserted rows (Prefer: return=representation).
        res = await run_db(lambda: get_db().table("group_trips").insert(row).execute())
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin/trips] create failed: %s", exc)
        raise HTTPException(502, "Could not create the trip.") from exc

    created = (res.data or [{}])[0] if res else {}
    trip_id = created.get("id")

    await admin_audit.record(
        actor=admin.actor, action="trip.created",
        entity_type="group_trips", entity_id=trip_id, entity_label=body.title,
        ip=admin.ip, metadata={"slug": slug, "destination": body.destination},
    )
    await emit("trip.created", {
        "actor": admin.actor, "entity_id": trip_id, "entity_label": body.title, "slug": slug,
    })

    return TripSaved(id=trip_id, slug=slug, status="draft", gaps=publish_gaps(created))


@router.patch("/{trip_id}", response_model=TripSaved)
@limiter.limit("120/minute")
async def update_trip(
    request: Request,
    trip_id: str,
    body: TripDraft,
    admin: AdminIdentity = Depends(require_admin),
) -> TripSaved:
    """Save edits. Never moves `status` — that's /publish."""
    existing = await _load(trip_id)
    if existing.get("partner_id"):
        raise HTTPException(
            409, "That's a partner's trip. They edit it from their own dashboard."
        )

    patch = _row_patch(body)

    # Seats are also written by the booking path. Move `seats_left` by the
    # delta so an edit can never un-sell a seat someone already paid for.
    if body.seats_total is not None:
        sold = (existing.get("seats_total") or 0) - (existing.get("seats_left") or 0)
        patch["seats_total"] = body.seats_total
        patch["seats_left"] = max(0, body.seats_total - sold)

    try:
        await run_db(
            lambda: get_db().table("group_trips").update(patch).eq("id", trip_id).execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin/trips] update failed: %s", exc)
        raise HTTPException(502, "Could not save the trip.") from exc

    merged = existing | patch
    await admin_audit.record(
        actor=admin.actor, action="trip.updated",
        entity_type="group_trips", entity_id=trip_id, entity_label=body.title,
        ip=admin.ip, metadata={"slug": existing.get("slug")},
    )
    await emit("trip.updated", {
        "actor": admin.actor, "entity_id": trip_id, "entity_label": body.title,
    })

    return TripSaved(
        id=trip_id, slug=existing["slug"], status=existing.get("status") or "draft",
        gaps=publish_gaps(merged),
    )


@router.post("/{trip_id}/validate", response_model=TripValidation)
@limiter.limit("120/minute")
async def validate_trip(
    request: Request,
    trip_id: str,
    _admin: AdminIdentity = Depends(require_admin),
) -> TripValidation:
    """What's still missing, without changing anything."""
    gaps = publish_gaps(await _load(trip_id))
    return TripValidation(ready=not gaps, gaps=gaps)


# ── publish ─────────────────────────────────────────────────────────────────

@router.post("/{trip_id}/publish", response_model=TripSaved)
@limiter.limit("60/minute")
async def publish_trip(
    request: Request,
    trip_id: str,
    live: bool = True,
    admin: AdminIdentity = Depends(require_admin),
) -> TripSaved:
    """
    Take a TravBuddy trip live, or pull it back to a draft.

    No review queue: it's our own trip and an admin is already the reviewer.
    The publish bar still applies — the same one partners face.
    """
    row = await _load(trip_id)
    if row.get("partner_id"):
        raise HTTPException(409, "Partner trips go through the review queue, not here.")

    if live:
        gaps = publish_gaps(row)
        if gaps:
            raise HTTPException(422, f"Not ready: {gaps[0].label}")
        if row.get("departure_date") and str(row["departure_date"]) < datetime.now(
            timezone.utc
        ).date().isoformat():
            raise HTTPException(422, "That departure date has already passed.")

    patch = (
        {"status": "published", "is_active": True, "approved_at": _now()}
        if live
        else {"status": "draft", "is_active": False}
    )

    try:
        await run_db(
            lambda: get_db().table("group_trips").update(patch).eq("id", trip_id).execute()
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, "Could not change the trip's status.") from exc

    action = "trip.published" if live else "trip.unpublished"
    await admin_audit.record(
        actor=admin.actor, action=action,
        entity_type="group_trips", entity_id=trip_id, entity_label=row.get("title"),
        ip=admin.ip, metadata={"slug": row.get("slug")},
    )
    await emit(action, {
        "actor": admin.actor, "entity_id": trip_id, "entity_label": row.get("title"),
        "public_url": f"https://travbuddy.in/trips/{row.get('slug')}",
    })

    return TripSaved(
        id=trip_id, slug=row["slug"],
        status="published" if live else "draft", gaps=[],
    )


@router.delete("/{trip_id}")
@limiter.limit("30/minute")
async def delete_trip(
    request: Request,
    trip_id: str,
    admin: AdminIdentity = Depends(require_admin),
) -> dict:
    """Refused once real seats have sold — a traveller who paid keeps their page."""
    row = await _load(trip_id)

    res = await run_db(
        lambda: get_db().table("bookings").select("id")
        .eq("item_id", trip_id).in_("status", ["paid", "confirmed", "completed"]).execute()
    )
    if (res.data if res else None):
        raise HTTPException(409, "This trip has bookings. Unpublish it instead of deleting.")

    try:
        await run_db(lambda: get_db().table("group_trips").delete().eq("id", trip_id).execute())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, "Could not delete the trip.") from exc

    await admin_audit.record(
        actor=admin.actor, action="trip.deleted",
        entity_type="group_trips", entity_id=trip_id, entity_label=row.get("title"),
        ip=admin.ip, metadata={"slug": row.get("slug")},
    )
    await emit("trip.deleted", {
        "actor": admin.actor, "entity_id": trip_id, "entity_label": row.get("title"),
    })
    return {"ok": True}
