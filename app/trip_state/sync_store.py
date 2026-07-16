"""
Mirror a trip's CURRENT graph into the search store, keyed by REAL leg/node ids
(docs/persistence_architecture.md Phase 3).

The generator's dual-write (app.trip_state.generator) runs before the graph
exists, so it keys transport batches by "going"/"return" and hotels by the
destination string. Once the builder has built a multi-city graph, the legs and
stops have real ids — this module re-mirrors the graph so ``resolve_options`` can
key by the actual leg/stop. It also backfills historical trips (scripts/
backfill_search_store.py).

Idempotent: a scope's batch is content-hashed; an unchanged graph is a no-op, a
changed one replaces that scope's batch (delete cascades its results). Additive
and best-effort — it only writes the new store, never the trip snapshot.
"""
from __future__ import annotations

import logging

from . import batches
from .batches import SearchBatchStore, params_hash

logger = logging.getLogger(__name__)


# ── extract (kind, scope_id, meta, provider-items) groups from a state dict ──────
def _edge_option_items(edge: dict) -> list[tuple[str, dict]]:
    """Every (mode, provider_item) across all routes/hops of a leg, deduped by
    option id. Only options that still carry their provider ``raw`` are returned:
    a leg loaded from the (thinned) snapshot has no raw and is ALREADY in the
    store — re-mirroring it would overwrite the good batch with degraded rows."""
    seen: set[str] = set()
    out: list[tuple[str, dict]] = []
    for route in edge.get("routes") or []:
        for hop in route.get("hops") or []:
            for opt in hop.get("options") or []:
                if not isinstance(opt, dict):
                    continue
                raw = opt.get("raw")
                if not (isinstance(raw, dict) and raw):
                    continue  # thin (already-stored) option — skip
                oid = str(opt.get("id") or id(opt))
                if oid in seen:
                    continue
                seen.add(oid)
                out.append((opt.get("mode") or "", raw))
    return out


def graph_groups(state: dict) -> list[dict]:
    """Batches to mirror from a state's graph. Falls back to the legacy
    transport/stay blocks for trips that predate the graph."""
    groups: list[dict] = []
    graph = state.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    if edges or nodes:
        for e in edges:
            items = _edge_option_items(e)
            if items:
                groups.append({
                    "kind": "transport", "scope_id": str(e.get("id") or ""),
                    "origin": e.get("from_city"), "destination": e.get("to_city"),
                    "depart_date": e.get("date"), "items": items,
                })
        for n in nodes:
            # Only fresh hotels (still carrying heavy fields) are mirrored; a hotel
            # loaded from the thinned snapshot is already in the store.
            hotels = [h for h in ((n.get("stay") or {}).get("search_results") or [])
                      if isinstance(h, dict) and any(k in h for k in ("photos", "amenities", "raw"))]
            if hotels:
                groups.append({
                    "kind": "hotel", "scope_id": str(n.get("id") or ""),
                    "origin": None, "destination": n.get("city"),
                    "depart_date": n.get("arrival_date"), "hotels": hotels,
                })
        return groups

    # ── legacy (no graph): mirror going/return transport + stay hotels ──
    transport = state.get("transport") or {}
    for scope, block in (("going", transport.get("going")),
                         ("return", transport.get("return_journey"))):
        if isinstance(block, dict) and any(
            (block.get(k) or {}).get("items") for k in ("flights", "trains", "buses")
        ):
            groups.append({"kind": "transport", "scope_id": scope,
                           "origin": None, "destination": None,
                           "depart_date": None, "shaped": block})
    hotels = ((state.get("stay") or {}).get("hotels")) or []
    hotels = [h for h in hotels if isinstance(h, dict)]
    if hotels:
        dest = (state.get("request") or {}).get("destination")
        groups.append({"kind": "hotel", "scope_id": dest or "",
                       "origin": None, "destination": dest,
                       "depart_date": None, "hotels": hotels})
    return groups


def _rows_for(group: dict, trip_id: str) -> list[dict]:
    kind = group["kind"]
    if kind == "hotel":
        return batches.hotel_rows_from_block("", trip_id, {"items": group["hotels"]})
    if "shaped" in group:  # legacy transport block
        return batches.transport_rows_from_shaped("", trip_id, group.get("depart_date"), group["shaped"])
    rows: list[dict] = []
    for mode, item in group["items"]:
        rows.append(batches.transport_result_row("", trip_id, mode or item.get("mode") or "flight",
                                                 group.get("depart_date"), item))
    return rows


def _signature(rows: list[dict]) -> str:
    """Content hash of a scope's results — an unchanged graph re-syncs to a no-op."""
    ids = sorted(f"{r.get('item_type')}:{r.get('ext_id')}:{r.get('price')}" for r in rows)
    return params_hash("sync", "", "", "", {"ids": ids})


# ── sync one trip (sync body; run via run_db) ────────────────────────────────────
def _sync_sync(trip_id: str, state: dict) -> dict:
    from app.db.client import get_db
    db = get_db()
    stats = {"batches": 0, "results": 0, "skipped": 0}
    for group in graph_groups(state):
        kind, scope_id = group["kind"], group["scope_id"]
        rows = _rows_for(group, trip_id)
        if not rows:
            continue
        phash = _signature(rows)
        existing = (db.table("search_batches").select("id, params_hash")
                    .eq("trip_id", trip_id).eq("kind", kind).eq("scope_id", scope_id)
                    .eq("status", "ready").order("created_at", desc=True).limit(1).execute().data)
        if existing and existing[0].get("params_hash") == phash:
            stats["skipped"] += 1
            continue
        # Replace this scope's batch(es): delete cascades old results.
        db.table("search_batches").delete().eq("trip_id", trip_id).eq("kind", kind).eq("scope_id", scope_id).execute()
        batch_id = SearchBatchStore.create_batch(
            trip_id, kind, scope_id=scope_id, phash=phash,
            origin=group.get("origin"), destination=group.get("destination"),
            depart_date=group.get("depart_date"))
        for r in rows:
            r["batch_id"] = batch_id
        n = SearchBatchStore.insert_results(rows)
        SearchBatchStore.finalize(batch_id, n)
        stats["batches"] += 1
        stats["results"] += n
        stats.setdefault("scored", 0)
    return stats


async def sync_trip_to_store(trip_id: str, state: dict, *, ctx: dict | None = None) -> dict:
    """Mirror a trip's current graph into the store (real leg/node scope_ids) and
    refresh its reco index. Best-effort — returns stats, never raises.

    Uses the RESILIENT DB path: the mirror is content-hash idempotent (an already-
    mirrored scope is skipped on retry), so the whole call can be replayed on a
    fresh client through the SSLV3_ALERT_BAD_RECORD_MAC storms that this redesign
    exists to survive — the backfill and post-edit sync both run off the request path."""
    from app.db.client import run_db_resilient
    try:
        stats = await run_db_resilient(
            lambda: _sync_sync(trip_id, state), label=f"sync_store:{trip_id}", max_seconds=120)
    except Exception as exc:  # noqa: BLE001 — mirroring is best-effort
        logger.warning("[sync-store] sync failed for %s: %s", trip_id, exc)
        return {"error": str(exc)}
    # Re-score every ready batch of the trip (cheap; batches are small).
    try:
        from app.reco import index
        ready = await run_db(_ready_batches, trip_id)
        for b in ready:
            await index.score_batch(trip_id, b["id"], scope_id=b.get("scope_id") or "",
                                    kind=b.get("kind") or "transport", ctx=ctx)
        stats["scored"] = len(ready)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[sync-store] scoring after sync failed for %s: %s", trip_id, exc)
    logger.info("[sync-store] %s: %s", trip_id, stats)
    return stats


def _ready_batches(trip_id: str) -> list[dict]:
    from app.db.client import get_db
    return (get_db().table("search_batches").select("id, kind, scope_id")
            .eq("trip_id", trip_id).eq("status", "ready").execute().data or [])


# ── store-first write with permanent-id stamping (trip engine v4, M3) ────────────

def mirror_graph_with_ids(trip_id: str, graph) -> dict:
    """PRIMARY store write for a fresh generation (sync; run via run_db_resilient).

    For every leg / stayed stop of the (fat, in-memory) graph: insert this
    scope's provider items into search_results and stamp each returned
    permanent id back onto the option/hotel as `result_id`. The graph then
    persists as references; the read path hydrates raw by id — exact, no fuzzy
    matching (docs/trip_engine_architecture.md §5.2).

    Old batches for a scope are NOT deleted (archived snapshots may reference
    their rows); readers pick the newest batch per scope, superseded ones age
    out by TTL. Returns {batches, results} stats.
    """
    from app.db.client import get_db

    db = get_db()
    stats = {"batches": 0, "results": 0}

    def _insert_scope(kind: str, scope_id: str, origin, destination, depart_date,
                      rows: list[dict], targets: list) -> None:
        """Insert rows for one scope and stamp returned ids onto `targets`
        (same order — transport_result_row/hotel_result_row preserve input order)."""
        if not rows:
            return
        batch_id = SearchBatchStore.create_batch(
            trip_id, kind, scope_id=scope_id,
            origin=origin, destination=destination, depart_date=depart_date)
        for r in rows:
            r["batch_id"] = batch_id
        inserted = db.table("search_results").insert(rows).execute().data or []
        SearchBatchStore.finalize(batch_id, len(inserted))
        for target, row in zip(targets, inserted):
            if isinstance(target, dict):
                target["result_id"] = row["id"]
            else:
                target.result_id = row["id"]
        stats["batches"] += 1
        stats["results"] += len(inserted)

    for edge in graph.edges:
        opts, rows = [], []
        seen: set[int] = set()
        for route in edge.routes or []:
            for hop in route.hops or []:
                for opt in hop.options or []:
                    if id(opt) in seen or not opt.raw:
                        continue
                    seen.add(id(opt))
                    opts.append(opt)
                    rows.append(batches.transport_result_row(
                        "", trip_id, opt.mode or "flight", edge.date, opt.raw))
        _insert_scope("transport", str(edge.id), edge.from_city, edge.to_city,
                      edge.date, rows, opts)

    for node in graph.nodes:
        hotels = [h for h in (node.stay.search_results or []) if isinstance(h, dict)]
        rows = [batches.hotel_result_row("", trip_id, h) for h in hotels]
        _insert_scope("hotel", str(node.id), None, node.city,
                      node.arrival_date, rows, hotels)

    logger.info("[sync-store] store-first mirror %s: %s", trip_id, stats)
    return stats

