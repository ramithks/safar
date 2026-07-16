"""
My Trips mirror from a TripDoc — same `trips` row contract as the legacy
mirror (trips.id == trip_states.id, itinerary_data.planner == "workspace")
so existing cards and deep-links keep working unchanged.
"""
from __future__ import annotations

import logging

from app.db.client import get_db, run_db_resilient

logger = logging.getLogger(__name__)


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) else None


def _flat_days(doc: dict) -> list[dict]:
    """Flatten per-stop days into one list — the preview shape the cards read."""
    days: list[dict] = []
    for stop in doc.get("itinerary") or []:
        days.extend(d for d in (stop.get("days") or []) if isinstance(d, dict))
    return days


def _itinerary_data(doc: dict) -> dict:
    spec = doc.get("spec") or {}
    dest = doc.get("destination") or {}
    budget = doc.get("budget") or {}
    transport = doc.get("transport") or []
    return_date = transport[-1].get("date") if transport else None
    return {
        "planner": "workspace",
        "trip_name": doc.get("title") or spec.get("destination"),
        "destination": spec.get("destination"),
        "destination_description": dest.get("description"),
        "cover_photo_ref": dest.get("cover_photo_ref"),
        "days": _flat_days(doc),
        "currency": budget.get("currency") or "INR",
        "travelers": spec.get("travelers"),
        "departure_date": spec.get("departure_date"),
        "return_date": return_date,
        "journey_summary": " → ".join(doc.get("route") or []) or None,
        "total_estimated_cost": _num(budget.get("total")),
        "cost_per_person": _num(budget.get("per_person")),
    }


async def upsert_my_trip(trip_id: str, user_id: str, doc: dict) -> None:
    """Best-effort upsert of the My Trips row. Never raises."""
    if not user_id:
        return
    try:
        spec = doc.get("spec") or {}
        transport = doc.get("transport") or []
        payload = {
            "id": trip_id,
            "user_id": user_id,
            "title": doc.get("title") or spec.get("destination") or "Trip",
            "destination": spec.get("destination"),
            "start_date": spec.get("departure_date") or None,
            "end_date": (transport[-1].get("date") if transport else None),
            "currency": (doc.get("budget") or {}).get("currency") or "INR",
            "itinerary_data": _itinerary_data(doc),
            "is_public": False,
        }
        await run_db_resilient(
            lambda: get_db().table("trips").upsert(payload, on_conflict="id").execute(),
            label="trip_doc.my_trips",
        )
        logger.info("[trip-doc] mirrored %s into My Trips", trip_id)
    except Exception as exc:  # noqa: BLE001 — advisory mirror only
        logger.warning("[trip-doc] My Trips mirror failed for %s: %s", trip_id, exc)
