"""
TripState persistence — snapshot in `trip_states`, append-only patch log in
`trip_state_events`. All writes go through the service-role client; the
orchestrator is the single writer.

Supabase calls are sync (postgrest client), so every public function here is
sync and callers hop through run_in_executor — same pattern as supervisor.py.
"""
import logging
import random
import threading
import time
from datetime import datetime, timezone

import httpx
from postgrest.exceptions import APIError

from app.db.client import get_db, reset_db
from .models import SCHEMA_VERSION, TripState
from .patch import PatchOp, apply_patches

logger = logging.getLogger(__name__)

# 5 attempts with exponential backoff (~0.3s → ~4s, ±jitter, ~9s total). The
# observed failure mode is a burst of TLS corruption for a few seconds right
# after the curl_cffi scrapers tear down — 3 fast retries died inside that
# window; backing off past it is what makes the write survive.
_MAX_APPEND_ATTEMPTS = 5


def _backoff(attempt: int) -> float:
    return min(0.3 * (2 ** (attempt - 1)), 4.0) + random.uniform(0, 0.3)

# Same-process writers (generator emitter, supervisor phase callbacks, copilot
# edits) serialize per trip; cross-process/lost-response races are handled by
# the seq-conflict detection + CAS inside _append_events_once. Lock objects are
# tiny and trips per process-lifetime are bounded, so no eviction needed.
_trip_locks: dict[str, threading.Lock] = {}
_trip_locks_guard = threading.Lock()


def _trip_lock(trip_id: str) -> threading.Lock:
    with _trip_locks_guard:
        return _trip_locks.setdefault(trip_id, threading.Lock())


class _SeqConflict(Exception):
    """Another writer advanced the event log/snapshot between our read and write."""


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

    Concurrency + crash safety: writers for the same trip serialize on an
    in-process lock; each attempt re-reads the snapshot, replays any event rows
    the snapshot hasn't absorbed yet (a previous writer's INSERT can commit
    while its response or follow-up UPDATE was lost in transport), recognises
    its own already-committed event instead of duplicating it, and CAS-guards
    the snapshot update. Transport failures retry on a fresh client (the
    client-level retry wrapper deliberately skips POSTs); seq/CAS conflicts
    retry from a fresh read.
    """
    last_exc: Exception | None = None
    with _trip_lock(trip_id):
        for attempt in range(1, _MAX_APPEND_ATTEMPTS + 1):
            try:
                return _append_events_once(trip_id, actor, patches, phase, reasoning)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning(
                    "[trip-state] %s persisting events for %s — retrying on a fresh client (%d/%d)",
                    type(exc).__name__, trip_id, attempt, _MAX_APPEND_ATTEMPTS,
                )
                reset_db()
                time.sleep(_backoff(attempt))
            except _SeqConflict as exc:
                last_exc = exc
                logger.warning(
                    "[trip-state] seq conflict persisting events for %s (%s) — retrying from a fresh read (%d/%d)",
                    trip_id, exc, attempt, _MAX_APPEND_ATTEMPTS,
                )
    assert last_exc is not None
    raise last_exc


def _append_events_once(
    trip_id: str,
    actor: str,
    patches: list[PatchOp],
    phase: str | None,
    reasoning: str | None,
) -> dict:
    db = get_db()
    row = get_trip_state(trip_id)
    if row is None:
        raise KeyError(f"trip_state {trip_id} not found")

    state = row["state"] or {}
    db_last_seq = row.get("last_seq") or 0
    patches_json = [p.model_dump(mode="json") for p in patches]

    def _is_ours(ev: dict | None) -> bool:
        return bool(
            ev and ev.get("actor") == actor and ev.get("phase") == phase
            and ev.get("patches") == patches_json
        )

    # Read the tail of the event log: the newest absorbed event (to detect a
    # fully-committed previous attempt whose response was lost) plus any
    # orphaned events the snapshot hasn't absorbed (INSERT committed, UPDATE
    # lost — the log runs ahead of last_seq).
    tail_events = get_events(trip_id, after_seq=max(db_last_seq - 1, 0))
    orphans = [ev for ev in tail_events if ev["seq"] > db_last_seq]
    newest_absorbed = next((ev for ev in tail_events if ev["seq"] == db_last_seq), None)

    if not orphans and _is_ours(newest_absorbed):
        # Previous attempt committed BOTH writes; only its response was lost.
        # The stored snapshot already contains our patches — nothing to do.
        logger.info("[trip-state] %s: event seq=%s already fully committed", trip_id, db_last_seq)
        return state

    # Replay orphans into the snapshot so this write neither reuses their seq
    # nor erases their changes when it overwrites the state column.
    already_committed = False
    last_seq = db_last_seq
    for ev in orphans:
        try:
            apply_patches(
                state,
                [PatchOp(**p) for p in (ev.get("patches") or [])],
                actor=ev.get("actor") or "supervisor",
            )
        except Exception as exc:  # noqa: BLE001 — a bad old event must not wedge the log
            logger.warning(
                "[trip-state] %s: could not replay orphaned event seq=%s: %s",
                trip_id, ev.get("seq"), exc,
            )
        last_seq = ev["seq"]
        if _is_ours(ev):
            already_committed = True   # our INSERT landed; patches now applied via replay

    seq = last_seq
    if not already_committed:
        apply_patches(state, patches, actor=actor)
        seq = last_seq + 1
        try:
            db.table("trip_state_events").insert({
                "trip_id": trip_id,
                "seq": seq,
                "actor": actor,
                "phase": phase,
                "patches": patches_json,
                "reasoning": reasoning,
            }).execute()
        except APIError as exc:
            if getattr(exc, "code", None) != "23505":
                raise
            # Duplicate seq. If the existing row is byte-identical it is our
            # own lost-response write — the snapshot update is still owed.
            # Anything else is a concurrent writer: retry from a fresh read.
            res = (
                db.table("trip_state_events")
                .select("actor, phase, patches")
                .eq("trip_id", trip_id).eq("seq", seq)
                .limit(1).execute()
            )
            if not _is_ours(res.data[0] if res.data else None):
                raise _SeqConflict(f"seq {seq} taken by another writer") from exc
            logger.info(
                "[trip-state] %s: event seq=%s already committed — finishing snapshot update",
                trip_id, seq,
            )

    meta = state.get("meta") or {}
    updated = db.table("trip_states").update({
        "state": state,
        "last_seq": seq,
        "status": meta.get("status") or row["status"],
        "title": meta.get("title") or row["title"],
        "updated_at": _now(),
    }).eq("id", trip_id).eq("last_seq", db_last_seq).execute()
    if not updated.data:
        # CAS failed: another writer advanced the snapshot after our read. Our
        # event row (if inserted) is safe in the log; the retry's fresh read
        # will see it as an orphan/duplicate and finish the snapshot from there.
        raise _SeqConflict(f"snapshot moved past last_seq={db_last_seq}")

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
