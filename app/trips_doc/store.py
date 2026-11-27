"""
TripDoc persistence — plain reads/writes on `trip_states.doc`.

No event log, no CAS, no per-trip locks, no flush queue. Every write is a
whole-doc (or status-only) UPDATE — naturally idempotent, so the resilient
DB path can replay it through transport storms without any reconciliation.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.db.client import get_db, run_db, run_db_resilient

logger = logging.getLogger(__name__)

_ROW_COLS = "id, user_id, title, status, visibility, progress, doc, created_at, updated_at"
_DOC_SIZE_WARN_BYTES = 500_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── reads ────────────────────────────────────────────────────────────────────

def get_row_sync(trip_id: str) -> dict | None:
    res = get_db().table("trip_states").select(_ROW_COLS).eq("id", trip_id).limit(1).execute()
    return res.data[0] if res.data else None


async def get_row(trip_id: str) -> dict | None:
    return await run_db(get_row_sync, trip_id)


_LIST_COLS = "id, title, status, progress, created_at, updated_at"


def list_mine_sync(user_id: str, limit: int = 20) -> list[dict]:
    """The user's own trips, newest first — summary fields only, no doc."""
    res = (get_db().table("trip_states").select(_LIST_COLS)
           .eq("user_id", user_id)
           .order("created_at", desc=True)
           .limit(limit).execute())
    return res.data or []


async def list_mine(user_id: str, limit: int = 20) -> list[dict]:
    return await run_db(list_mine_sync, user_id, limit)


# ── writes ───────────────────────────────────────────────────────────────────

def create_trip_sync(user_id: str | None, doc: dict) -> str:
    """Insert a new trip row; `doc` is the initial (planning) document."""
    row = get_db().table("trip_states").insert({
        "user_id": user_id,
        "title": doc.get("title"),
        "origin": (doc.get("spec") or {}).get("origin"),
        "destination": (doc.get("spec") or {}).get("destination"),
        "status": doc.get("status") or "planning",
        "progress": doc.get("progress") or 0,
        "doc": doc,
    }).execute()
    trip_id = row.data[0]["id"]
    logger.info("[trip-doc] created %s (%s → %s)", trip_id,
                (doc.get("spec") or {}).get("origin"), (doc.get("spec") or {}).get("destination"))
    return trip_id


async def create_trip(user_id: str | None, doc: dict) -> str:
    return await run_db(create_trip_sync, user_id, doc)


def _put_doc_sync(trip_id: str, doc: dict) -> None:
    # returning="minimal": don't echo the whole doc back over the wire.
    get_db().table("trip_states").update({
        "doc": doc,
        "title": doc.get("title"),
        "status": doc.get("status") or "ready",
        "progress": doc.get("progress") if isinstance(doc.get("progress"), int) else 100,
        "updated_at": _now(),
    }, returning="minimal").eq("id", trip_id).execute()


async def put_doc(trip_id: str, doc: dict) -> dict:
    """Persist the whole doc (idempotent — safe to replay). Returns the doc."""
    doc["updated_at"] = _now()
    size = len(json.dumps(doc, default=str))
    if size > _DOC_SIZE_WARN_BYTES:
        logger.warning("[trip-doc] %s doc is %d KB — check option caps", trip_id, size // 1024)
    await run_db_resilient(lambda: _put_doc_sync(trip_id, doc),
                           label=f"trip_doc.put:{trip_id}", max_seconds=120)
    return doc


async def set_status(trip_id: str, status: str, progress: int | None = None,
                     error: str | None = None) -> None:
    """Cheap status/progress-only transition (row columns + doc fields).
    A terminal error also records a user-honest reason on the doc so the
    client can say WHY instead of spinning forever."""
    def _sync() -> None:
        row = get_row_sync(trip_id)
        if row is None:
            return
        doc = row.get("doc") or {}
        doc["status"] = status
        if error is not None:
            doc["error"] = error
        elif status == "ready":
            doc.pop("error", None)
        if progress is not None:
            doc["progress"] = progress
        doc["updated_at"] = _now()
        payload = {"status": status, "doc": doc, "updated_at": _now()}
        if progress is not None:
            payload["progress"] = progress
        get_db().table("trip_states").update(payload).eq("id", trip_id).execute()
    await run_db_resilient(_sync, label=f"trip_doc.status:{trip_id}", max_seconds=60)


# ── pre-regeneration archive (the one piece of integrity kept) ───────────────

def archive_doc_sync(trip_id: str, doc: dict, reason: str) -> None:
    """One INSERT of the current doc before a rebuild — cheap insurance.
    Bounded retention: keep the newest 5 per trip."""
    db = get_db()
    db.table("trip_snapshots").insert({
        "trip_id": trip_id,
        "reason": reason,
        "spec_version": int((doc.get("spec") or {}).get("version") or 1),
        "state": doc,
    }).execute()
    rows = (db.table("trip_snapshots").select("id")
            .eq("trip_id", trip_id).order("created_at", desc=True).execute().data or [])
    for stale in rows[5:]:
        db.table("trip_snapshots").delete().eq("id", stale["id"]).execute()

