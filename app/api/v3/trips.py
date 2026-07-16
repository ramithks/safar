"""
TripDoc endpoints.

  POST   /api/v3/trips                → create + start generation
  GET    /api/v3/trips/{id}           → the doc (poll this while planning)
  PATCH  /api/v3/trips/{id}/select    → instant selection (one UPDATE, no AI)
  POST   /api/v3/trips/{id}/actions   → structural change → regenerate
  POST   /api/v3/trips/{id}/search-hotels → live hotel re-search for one stop

NOTE: no `from __future__ import annotations` here — stringified annotations
break FastAPI's body-model resolution under the slowapi limiter wrapper
(the pydantic body silently becomes a query param → 422).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.background import spawn_background
from app.core.limiter import limiter
from app.db.client import run_db_resilient
from app.deps.auth import require_user
from app.api.v1.plan_trip.models import PlanRequest, StopSpec, _validate_not_past_date
from app.trips_doc import generate, mytrips, select, store
from app.trips_doc.serialize import rerank_for_budget

logger = logging.getLogger(__name__)
router = APIRouter()


# ── auth (same visibility rules as the legacy surface) ──────────────────────

def _authorize_read(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id and row["visibility"] not in ("link", "public"):
        raise HTTPException(status_code=404, detail="Trip not found")


def _authorize_write(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your trip")


async def _get_row(trip_id: str) -> dict:
    row = await store.get_row(trip_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return row


def _doc_of(row: dict) -> dict:
    doc = row.get("doc")
    if not doc:
        raise HTTPException(status_code=404, detail="Trip has no content yet")
    return doc


def _envelope(row: dict, doc: dict) -> dict:
    return {
        "trip_id": str(row["id"]),
        "status": doc.get("status") or row.get("status"),
        "progress": doc.get("progress", row.get("progress") or 0),
        "visibility": row.get("visibility"),
        "doc": doc,
    }


# ── create ───────────────────────────────────────────────────────────────────

@router.post("")
@limiter.limit("10/hour")
async def create_trip(request: Request, body: PlanRequest, user=Depends(require_user)):
    spec = {**body.model_dump(), "budget_tier": body.budget, "version": 1}
    doc = generate.initial_doc(spec)
    trip_id = await store.create_trip(str(user.id), doc)
    doc["id"] = trip_id
    spawn_background(generate.run_generation(trip_id, spec, str(user.id)),
                     name=f"trip_doc_gen:{trip_id}")
    logger.info("[trip-doc] %s → %s queued as %s", body.origin, body.destination, trip_id)
    return {"trip_id": trip_id, "status": "planning"}


# ── read (the polling target) ────────────────────────────────────────────────

@router.get("/{trip_id}")
async def get_trip(trip_id: str, user=Depends(require_user)):
    row = await _get_row(trip_id)
    _authorize_read(row, str(user.id))
    return _envelope(row, _doc_of(row))


# ── instant selections (never invoke planning logic) ────────────────────────

class SelectBody(BaseModel):
    kind: str                      # transport | hotel
    leg_id: str | None = None      # transport
    hop_index: int = 0             # transport (multi-hop routes)
    option_id: str | None = None   # transport
    stop_id: str | None = None     # hotel (stop_id or city name)
    hotel_id: str | None = None    # hotel


@router.patch("/{trip_id}/select")
@limiter.limit("120/minute")
async def select_option(request: Request, trip_id: str, body: SelectBody,
                        user=Depends(require_user)):
    row = await _get_row(trip_id)
    _authorize_write(row, str(user.id))
    doc = _doc_of(row)
    try:
        if body.kind == "transport":
            if not (body.leg_id and body.option_id):
                raise ValueError("leg_id and option_id are required")
            doc, note = select.select_transport(doc, body.leg_id, body.option_id,
                                                hop_index=body.hop_index)
        elif body.kind == "hotel":
            if not (body.stop_id and body.hotel_id):
                raise ValueError("stop_id and hotel_id are required")
            doc, note = select.select_hotel(doc, body.stop_id, body.hotel_id)
        else:
            raise ValueError(f"Unknown selection kind '{body.kind}'")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await store.put_doc(trip_id, doc)
    spawn_background(mytrips.upsert_my_trip(trip_id, str(user.id), doc),
                     name=f"mytrips:{trip_id}")
    return {"ok": True, "note": note, "budget": doc.get("budget"),
            "updated_at": doc.get("updated_at")}


# ── structural actions (the expensive path — spec change → regenerate) ──────

class ActionBody(BaseModel):
    type: str                            # add_stop | remove_stop | change_dates | change_budget | regenerate
    city: str | None = None              # add_stop / remove_stop
    nights: int | None = None            # add_stop
    position: int | None = None          # add_stop: index into spec.stops (0 = right after origin)
    departure_date: str | None = None    # change_dates
    budget: str | None = None            # change_budget: low | mid | high


def _mutate_spec(spec: dict, body: ActionBody) -> str:
    """Apply one structural mutation in place. Returns a human note.
    (Same rules the legacy /actions endpoint enforced.)"""
    if body.type == "add_stop":
        stop = StopSpec(city=body.city or "", nights=body.nights)
        taken = {(spec.get("origin") or "").strip().lower(),
                 (spec.get("destination") or "").strip().lower(),
                 *((s.get("city") or "").strip().lower() for s in spec.get("stops") or [])}
        if stop.city.strip().lower() in taken:
            raise HTTPException(status_code=400, detail=f"{stop.city} is already on the route")
        stops = spec.setdefault("stops", [])
        if len(stops) >= 4:
            raise HTTPException(status_code=400, detail="At most 4 stops per trip")
        idx = len(stops) if body.position is None else max(0, min(int(body.position), len(stops)))
        stops.insert(idx, stop.model_dump())
        # A new stop EXTENDS the trip — its nights must never be stolen from the
        # destination (adding 3 nights in Chandigarh used to leave Manali with 1).
        spec["days"] = int(spec.get("days") or 3) + max(int(stop.nights or 1), 1)
        return f"added stop {stop.city} (+{max(int(stop.nights or 1), 1)} days)"

    if body.type == "remove_stop":
        key = (body.city or "").strip().lower()
        stops = spec.get("stops") or []
        removed = next((s for s in stops if (s.get("city") or "").strip().lower() == key), None)
        if removed is None:
            raise HTTPException(status_code=400, detail=f"{body.city} is not a stop on this trip")
        spec["stops"] = [s for s in stops if s is not removed]
        # Symmetric: give back the days the stop added (never below a 2-day trip).
        spec["days"] = max(int(spec.get("days") or 3) - max(int(removed.get("nights") or 1), 1), 2)
        return f"removed stop {body.city}"

    if body.type == "change_dates":
        try:
            spec["departure_date"] = _validate_not_past_date(body.departure_date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return f"moved departure to {body.departure_date}"

    if body.type == "change_budget":
        if body.budget not in ("low", "mid", "high"):
            raise HTTPException(status_code=400, detail="budget must be low | mid | high")
        spec["budget_tier"] = body.budget
        return f"budget set to {body.budget}"

    if body.type == "regenerate":
        return "regenerating from the current specification"

    raise HTTPException(status_code=400, detail=f"Unknown action '{body.type}'")


@router.post("/{trip_id}/actions")
@limiter.limit("30/hour")
async def trip_action(request: Request, trip_id: str, body: ActionBody,
                      user=Depends(require_user)):
    row = await _get_row(trip_id)
    _authorize_write(row, str(user.id))
    doc = _doc_of(row)

    if doc.get("status") == "planning":
        raise HTTPException(status_code=409, detail="A generation is already running for this trip")

    spec = dict(doc.get("spec") or {})
    note = _mutate_spec(spec, body)

    # change_budget: re-rank the already-inlined options — no re-search, no AI.
    if body.type == "change_budget":
        spec["version"] = int(spec.get("version") or 1) + 1
        doc["spec"] = spec
        doc = rerank_for_budget(doc, body.budget)
        await store.put_doc(trip_id, doc)
        return {"status": "applied", "applied": note, "regenerating": False,
                "budget": doc.get("budget")}

    # Structural actions: archive → bump spec → rebuild. Archive is awaited —
    # a rebuild must not start until the pre-change doc is durable.
    if body.type != "regenerate":
        spec["version"] = int(spec.get("version") or 1) + 1
    try:
        PlanRequest(
            origin=spec.get("origin") or "", destination=spec.get("destination") or "",
            departure_date=spec.get("departure_date"), days=int(spec.get("days") or 3),
            travelers=int(spec.get("travelers") or 1),
            budget=spec.get("budget_tier") or "mid",
            interests=spec.get("interests") or [], stops=spec.get("stops") or [],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Specification invalid: {exc}") from exc

    # Resilient: a transient transport blip must not 500 a structural action.
    # Replaying the INSERT at worst writes a duplicate snapshot row, which the
    # bounded per-trip retention prunes anyway.
    await run_db_resilient(
        lambda: store.archive_doc_sync(trip_id, doc, f"pre-{body.type}"),
        label=f"trip_doc.archive:{trip_id}", max_seconds=45)

    prior_doc = dict(doc)
    doc["spec"] = spec
    doc["status"] = "planning"
    doc["progress"] = 5
    await store.put_doc(trip_id, doc)

    spawn_background(generate.run_generation(trip_id, spec, str(user.id), prior_doc=prior_doc),
                     name=f"trip_doc_regen:{trip_id}")
    logger.info("[trip-doc] %s on %s → regenerating (spec v%s)",
                body.type, trip_id, spec.get("version"))
    return {"status": "regenerating", "applied": note,
            "spec_version": spec.get("version"), "regenerating": True}


# ── itinerary day editing (instant path — add/reorder/delete places) ────────

class ItineraryEditBody(BaseModel):
    stop_id: str                       # stop_id or city name
    day_index: int                     # 0-based index into that stop's days
    activities: list[dict]             # the day's FULL new activities list


@router.patch("/{trip_id}/itinerary")
@limiter.limit("120/minute")
async def edit_itinerary(request: Request, trip_id: str, body: ItineraryEditBody,
                         user=Depends(require_user)):
    from app.trips_doc.itinerary import edit_day_activities

    row = await _get_row(trip_id)
    _authorize_write(row, str(user.id))
    doc = _doc_of(row)
    try:
        doc, note = edit_day_activities(doc, body.stop_id, body.day_index, body.activities)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await store.put_doc(trip_id, doc)
    return {"ok": True, "note": note, "updated_at": doc.get("updated_at")}


@router.get("/{trip_id}/places")
@limiter.limit("60/hour")
async def search_trip_places(request: Request, trip_id: str, stop_id: str, q: str,
                             user=Depends(require_user)):
    """Google-Places-verified candidates for 'Add a place' — never free text."""
    from app.trips_doc.itinerary import search_places_for_stop

    row = await _get_row(trip_id)
    _authorize_read(row, str(user.id))
    query = (q or "").strip()[:80]
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Type at least 2 characters")
    places = await search_places_for_stop(_doc_of(row), stop_id, query)
    return {"places": places}


# ── explicit hotel re-search (expensive, user-invoked) ───────────────────────

class HotelSearchBody(BaseModel):
    stop_id: str                       # stop_id or city name
    query: str = ""                    # optional landmark/area ("near Mall Road")
    filters: dict | None = None        # canonical hotel filters


@router.post("/{trip_id}/search-hotels")
@limiter.limit("30/hour")
async def search_hotels(request: Request, trip_id: str, body: HotelSearchBody,
                        user=Depends(require_user)):
    from app.trips_doc.hotel_search import search_stop_hotels

    row = await _get_row(trip_id)
    _authorize_write(row, str(user.id))
    doc = _doc_of(row)
    try:
        doc, note = await search_stop_hotels(doc, body.stop_id,
                                             query=body.query.strip()[:120],
                                             raw_filters=body.filters)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await store.put_doc(trip_id, doc)
    stop = next((s for s in doc.get("itinerary") or []
                 if s.get("stop_id") == body.stop_id
                 or (s.get("city") or "").strip().lower() == body.stop_id.strip().lower()), None)
    return {"ok": True, "note": note, "budget": doc.get("budget"),
            "hotels": (stop or {}).get("hotels"), "updated_at": doc.get("updated_at")}
