"""
Integrity layer — generation manifests, snapshot checksums, pre-change archives.

Three guarantees (docs/trip_engine_architecture.md §3, §6, §9):

1. **Generation Manifest** (`state.meta.generation`) — an immutable record of
   exactly what a generation run used: model ids, provider parser versions,
   spec version, timestamps. Prompt/ranking/scraper evolution never erases how
   an old trip was produced.

2. **Checksums** (`state.meta.checksums`) — every committed snapshot carries
   sha256 seals of its spec, graph, and referenced search result ids, stamped
   inside the same CAS-guarded write as the data. Reads recompute and compare:
   a mismatch marks the trip `corrupted` (visible, alertable) instead of
   silently serving bad state. Detects the storage/transport corruption class
   (e.g. the SSLV3_ALERT_BAD_RECORD_MAC storms), not logical drift.

3. **Snapshots** (`trip_snapshots` table) — the full committed state is
   archived before every regeneration/structural rebuild, so a bad rebuild is
   restorable through the normal write path. Bounded per-trip retention.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CHECKSUM_ALGO = "sha256-canonical-json-v1"

# Bumped whenever the trip engine's behavior changes meaningfully (prompts,
# ranking, pipeline shape) — recorded in every manifest.
ENGINE_VERSION = "2026.07.16"

# Per-provider parser/adapter versions. Bump a provider's entry whenever its
# scraper or normalizer changes the shape/meaning of what it returns — that is
# what makes an old trip's results interpretable months later.
PROVIDER_VERSIONS = {
    "flight": "v1",   # booking + easemytrip + gotogate normalizers
    "hotel": "v1",    # easemytrip + booking + agoda + ixigo normalizers
    "train": "v1",    # railway (confirmtkt-shaped) normalizer
    "bus": "v1",      # redbus + flixbus normalizers
}

_SNAPSHOTS_KEPT_PER_TRIP = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── canonical hashing ─────────────────────────────────────────────────────────

def canonical_json(obj) -> bytes:
    """Deterministic byte serialization: sorted keys, no whitespace variance,
    non-JSON types coerced via str. Same object → same bytes, always."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode("utf-8")


def _sha(obj) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj)).hexdigest()


def _referenced_result_ids(graph: dict) -> list[str]:
    """Sorted `result_id`s the graph references (graph v2). Legacy graphs whose
    options predate stable ids contribute nothing — their search seal is the
    hash of the empty list, still stable."""
    ids: set[str] = set()
    for edge in graph.get("edges") or []:
        for route in edge.get("routes") or []:
            for hop in route.get("hops") or []:
                for opt in hop.get("options") or []:
                    rid = isinstance(opt, dict) and opt.get("result_id")
                    if rid:
                        ids.add(str(rid))
    for node in graph.get("nodes") or []:
        for hotel in ((node.get("stay") or {}).get("search_results")) or []:
            rid = isinstance(hotel, dict) and hotel.get("result_id")
            if rid:
                ids.add(str(rid))
    return sorted(ids)


def compute_checksums(state: dict) -> dict:
    """Seals for the sections that define the trip. `spec` falls back to
    `request` until spec formalization backfills old trips."""
    return {
        "spec": _sha(state.get("spec") or state.get("request") or {}),
        "graph": _sha(state.get("graph") or {}),
        "search": _sha(_referenced_result_ids(state.get("graph") or {})),
        "algo": CHECKSUM_ALGO,
        "sealed_at": _now(),
    }


def stamp(state: dict) -> dict:
    """Recompute and attach seals to `state` (mutates; returns it). Called on
    every snapshot commit so seal and payload land in the same write."""
    meta = state.setdefault("meta", {})
    meta["checksums"] = compute_checksums(state)
    return state


def verify(state: dict) -> str:
    """'ok' | 'corrupted' | 'unsealed'. Never raises — integrity checking must
    not take a read down."""
    try:
        stored = (state.get("meta") or {}).get("checksums")
        if not isinstance(stored, dict) or not stored.get("graph"):
            return "unsealed"          # pre-integrity row; nothing to compare
        current = compute_checksums(state)
        for key in ("spec", "graph", "search"):
            if stored.get(key) != current[key]:
                return "corrupted"
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[integrity] verify failed: %s", exc)
        return "unsealed"


# ── generation manifest ───────────────────────────────────────────────────────

def build_manifest(spec_version: int | None = None) -> dict:
    """Open a manifest at generation start. Model ids are read from config AT
    RUN TIME so the manifest records what actually ran."""
    from app.config import settings

    return {
        "generation_id": str(uuid.uuid4()),
        "spec_version": spec_version if spec_version is not None else 1,
        "route_model": settings.vertex_model_brain,
        "itinerary_model": settings.vertex_model_brain,
        "extraction_model": settings.vertex_model_muscle,
        "provider_versions": dict(PROVIDER_VERSIONS),
        "engine_version": ENGINE_VERSION,
        "started_at": _now(),
        "completed_at": None,
        "status": "running",
    }


def finalize_manifest(manifest: dict, status: str = "complete") -> dict:
    manifest["completed_at"] = _now()
    manifest["status"] = status
    return manifest


# ── snapshots (archive / restore) ─────────────────────────────────────────────

def archive_snapshot(trip_id: str, state: dict, *, reason: str,
                     generation_id: str | None = None,
                     last_seq: int | None = None) -> str | None:
    """Persist an immutable copy of `state` (sync; run via run_db). Returns the
    snapshot id, or None when there is nothing worth archiving. Retention:
    oldest rows beyond the newest N are pruned."""
    if not state or not (state.get("graph") or state.get("transport")):
        return None                    # blank/planning shell — nothing to restore later
    from app.db.client import get_db

    db = get_db()
    row = db.table("trip_snapshots").insert({
        "trip_id": trip_id,
        "generation_id": generation_id,
        "reason": reason,
        "spec_version": ((state.get("spec") or {}).get("version")) or 1,
        "last_seq": last_seq,
        "state": state,
    }).execute()
    snap_id = row.data[0]["id"]

    # Bounded retention — keep the newest N per trip.
    try:
        keep = (db.table("trip_snapshots").select("id")
                .eq("trip_id", trip_id)
                .order("created_at", desc=True)
                .limit(_SNAPSHOTS_KEPT_PER_TRIP).execute().data or [])
        keep_ids = {r["id"] for r in keep}
        old = (db.table("trip_snapshots").select("id")
               .eq("trip_id", trip_id).execute().data or [])
        stale = [r["id"] for r in old if r["id"] not in keep_ids]
        if stale:
            db.table("trip_snapshots").delete().in_("id", stale).execute()
    except Exception as exc:  # noqa: BLE001 — retention is housekeeping
        logger.debug("[integrity] snapshot retention prune failed for %s: %s", trip_id, exc)
    return snap_id


def load_snapshot(snapshot_id: str) -> dict | None:
    """One archived snapshot row (sync; run via run_db)."""
    from app.db.client import get_db

    res = (get_db().table("trip_snapshots").select("*")
           .eq("id", snapshot_id).limit(1).execute().data)
    return res[0] if res else None


def list_snapshots(trip_id: str) -> list[dict]:
    """Snapshot metadata for a trip, newest first (sync; run via run_db)."""
    from app.db.client import get_db

    return (get_db().table("trip_snapshots")
            .select("id, reason, generation_id, spec_version, created_at")
            .eq("trip_id", trip_id)
            .order("created_at", desc=True).execute().data or [])


async def restore_snapshot(trip_id: str, snapshot_id: str) -> bool:
    """Copy an archived state back through the NORMAL write path (per-trip
    lock + CAS + event row), so a restore is just another audited commit —
    never a raw column overwrite."""
    from app.db.client import run_db

    from . import store
    from .patch import PatchOp

    snap = await run_db(load_snapshot, snapshot_id)
    if not snap or snap["trip_id"] != trip_id:
        return False
    archived = snap["state"] or {}
    patches = [
        PatchOp(op="set", path=f"/{section}", value=value)
        for section, value in archived.items()
        if section != "meta"
    ]
    # meta merges (keeps trip_id/status housekeeping) rather than replacing.
    if archived.get("meta"):
        patches.append(PatchOp(op="merge", path="/meta", value={
            k: v for k, v in archived["meta"].items() if k != "checksums"
        }))
    await run_db(
        store.append_events, trip_id, "user", patches,
        f"restored:{snapshot_id}",
    )
    logger.info("[integrity] restored trip %s from snapshot %s", trip_id, snapshot_id)
    return True
