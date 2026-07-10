"""
TripState persistence — snapshot in `trip_states`, append-only patch log in
`trip_state_events`. All writes go through the service-role client; the
orchestrator is the single writer.

Supabase calls are sync (postgrest client), so every public function here is
sync and callers hop through run_in_executor — same pattern as supervisor.py.
"""
import logging
from datetime import datetime, timezone

from app.db.client import get_db
from .models import SCHEMA_VERSION, TripState
from .patch import PatchOp, apply_patches

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_trip_state(user_id: str | None, state: TripState) -> str:
    """Insert a new trip_states row; returns trip_id."""
    db = get_db()
    row = db.table("trip_states").insert({
        "user_id": user_id,
        "title": state.meta.title or state.request.destination,
        "origin": state.request.origin,
        "destination": state.request.destination,
        "status": state.meta.status,
        "visibility": state.meta.visibility,
        "schema_version": SCHEMA_VERSION,
        "state": state.model_dump(mode="json"),
    }).execute()
    trip_id = row.data[0]["id"]
    logger.info("[trip-state] created %s (%s → %s)", trip_id,
                state.request.origin, state.request.destination)
    return trip_id


def get_trip_state(trip_id: str) -> dict | None:
    """Returns the full trip_states row (state + meta columns) or None."""
    db = get_db()
    res = db.table("trip_states").select("*").eq("id", trip_id).limit(1).execute()
    return res.data[0] if res.data else None


def append_events(
    trip_id: str,
    actor: str,
    patches: list[PatchOp],
    phase: str | None = None,
    reasoning: str | None = None,
) -> dict:
    """
    Apply patches to the stored snapshot and append one event row.
    Returns the updated state dict.
    """
    db = get_db()
    row = get_trip_state(trip_id)
    if row is None:
        raise KeyError(f"trip_state {trip_id} not found")

    state = row["state"] or {}
    apply_patches(state, patches, actor=actor)
    seq = (row.get("last_seq") or 0) + 1

    db.table("trip_state_events").insert({
        "trip_id": trip_id,
        "seq": seq,
        "actor": actor,
        "phase": phase,
        "patches": [p.model_dump(mode="json") for p in patches],
        "reasoning": reasoning,
    }).execute()

    meta = state.get("meta") or {}
    db.table("trip_states").update({
        "state": state,
        "last_seq": seq,
        "status": meta.get("status") or row["status"],
        "title": meta.get("title") or row["title"],
        "updated_at": _now(),
    }).eq("id", trip_id).execute()

    return state


def get_events(trip_id: str, after_seq: int = 0) -> list[dict]:
    db = get_db()
    res = (
        db.table("trip_state_events")
        .select("seq, actor, phase, patches, reasoning, created_at")
        .eq("trip_id", trip_id)
        .gt("seq", after_seq)
        .order("seq")
        .execute()
    )
    return res.data or []
