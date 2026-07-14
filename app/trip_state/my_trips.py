"""
Mirror a workspace trip into the shared `trips` table so it shows up in
My Trips (website + app) alongside classic /api/v1/plan trips.

trips.id == trip_states.id, so a My Trips card can deep-link straight back to
the live workspace at /plan/{id}. itinerary_data carries planner="workspace" —
the marker the frontend uses to route the card to the workspace instead of the
static itinerary view. The workspace snapshot in `trip_states` stays the source
of truth; this row is a listing/preview mirror, refreshed on generation
complete and after copilot edits.
"""
import logging

from app.db.client import get_db, run_db_resilient

logger = logging.getLogger(__name__)


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) else None


def _itinerary_data(state: dict) -> dict:
    """Slim TripData-shaped preview from a TripState — same fields the My Trips
    cards and the static itinerary view read on classic trips."""
    req = state.get("request") or {}
    meta = state.get("meta") or {}
    dest = state.get("destination") or {}
    costs = state.get("costs") or {}
    transport = state.get("transport") or {}
    return {
        "planner": "workspace",
        "trip_name": meta.get("title") or req.get("destination"),
        "destination": req.get("destination"),
        "destination_description": dest.get("description"),
        "cover_photo_ref": dest.get("cover_photo_ref"),
        "days": (state.get("itinerary") or {}).get("days") or [],
        "currency": costs.get("currency") or "INR",
        "travelers": req.get("travelers"),
        "departure_date": req.get("departure_date"),
        "return_date": req.get("return_date"),
        "journey_summary": transport.get("journey_summary"),
        "total_estimated_cost": _num(costs.get("total")),
        "cost_per_person": _num(costs.get("per_person")),
    }


async def upsert_my_trip(trip_id: str, user_id: str, state: dict) -> None:
    """Best-effort upsert of the My Trips mirror row. Never raises — a listing
    miss must not touch the workspace trip itself."""
    if not user_id:
        return
    try:
        req = state.get("request") or {}
        meta = state.get("meta") or {}
        payload = {
            "id": trip_id,
            "user_id": user_id,
            "title": meta.get("title") or req.get("destination") or "Trip",
            "destination": req.get("destination"),
            "start_date": req.get("departure_date") or None,
            "end_date": req.get("return_date") or req.get("end_date") or None,
            "currency": (state.get("costs") or {}).get("currency") or "INR",
            "itinerary_data": _itinerary_data(state),
            "is_public": False,
        }
        await run_db_resilient(
            lambda: get_db().table("trips").upsert(payload, on_conflict="id").execute(),
            label="trip_state.my_trips",
        )
        logger.info("[trip-state] mirrored %s into My Trips", trip_id)
    except Exception as exc:  # noqa: BLE001 — advisory mirror only
        logger.warning("[trip-state] My Trips mirror failed for %s: %s", trip_id, exc)
