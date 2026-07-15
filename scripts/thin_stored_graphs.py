#!/usr/bin/env python3
"""
Thin the stored graph on existing trips (docs/persistence_architecture.md Inc 5).

Increment 4 made the builder PERSIST a thinned graph (raw payloads stripped — they
live in the search store, served by /options + /results). Trips written before that
still carry the fat graph. This migration brings them forward:

  for each trip WITH a graph:
    1. ensure every leg/stop's raw is in the search store  (sync — idempotent; the
       backfill already did this, so it's a no-op unless a batch is missing)
    2. persist a thinned graph + the skinny transport projection

Safe: the raw is guaranteed in the store (step 1) BEFORE it's stripped (step 2), so
nothing is lost. Idempotent: a trip whose graph is already thin is skipped.

Dry-run by default (reports bytes saved); add --run to persist.

    python scripts/thin_stored_graphs.py              # dry run
    python scripts/thin_stored_graphs.py --run
    python scripts/thin_stored_graphs.py --run --trip <uuid>

Needs live Supabase (creds from .env). Run from the repo root.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("thin-graphs")


def _graph_trips(trip: str | None) -> list[dict]:
    from app.db.client import get_db
    q = get_db().table("trip_states").select("id, state").order("created_at")
    if trip:
        q = q.eq("id", trip)
    return q.execute().data or []


def _has_raw(graph: dict) -> bool:
    for e in graph.get("edges") or []:
        for r in e.get("routes") or []:
            for h in r.get("hops") or []:
                for o in h.get("options") or []:
                    if isinstance(o, dict) and o.get("raw"):
                        return True
    return False


async def main() -> None:
    ap = argparse.ArgumentParser(description="Thin the stored graph on existing trips.")
    ap.add_argument("--run", action="store_true", help="persist (default: dry run)")
    ap.add_argument("--trip", type=str, default=None)
    args = ap.parse_args()

    from app.db.client import get_db, run_db, run_db_resilient
    from app.trip_state import graph as G
    from app.trip_state.sync_store import sync_trip_to_store

    rows = await run_db(_graph_trips, args.trip)
    processed = thinned = saved = failed = 0
    for row in rows:
        state = row.get("state") or {}
        graph_dict = state.get("graph") or {}
        if not (graph_dict.get("edges") and _has_raw(graph_dict)):
            continue   # classic trip or already thin
        processed += 1
        trip_id = row["id"]
        try:
            graph = G.ensure_graph(state)
            # 1) guarantee raw is in the store before we strip it (idempotent, resilient)
            if args.run:
                await sync_trip_to_store(trip_id, state)

            # 2) build the thinned state (thin graph + skinny transport/costs projection)
            proj = G.project(state, graph)
            new_state = dict(state)
            new_state["graph"] = G.thin_graph_dump(graph_dict)
            if "/transport" in proj:
                new_state["transport"] = {**(state.get("transport") or {}), **proj["/transport"]}
            if "/costs" in proj:
                new_state["costs"] = proj["/costs"]
            if "/itinerary/days" in proj:
                new_state.setdefault("itinerary", {})["days"] = proj["/itinerary/days"]

            before, after = len(json.dumps(state)), len(json.dumps(new_state))
            if args.run:
                # idempotent overwrite — safe to retry through a TLS storm
                await run_db_resilient(
                    lambda: get_db().table("trip_states")
                    .update({"state": new_state}).eq("id", trip_id).execute(),
                    label=f"thin:{trip_id}", max_seconds=120)
            saved += before - after
            thinned += 1
            logger.info("%s %s: %d -> %d B (%.0f%% smaller)",
                        "[run]" if args.run else "[dry]", trip_id, before, after,
                        100 * (1 - after / before) if before else 0)
        except Exception as exc:  # noqa: BLE001 — isolate one trip's failure
            failed += 1
            logger.warning("[skip] %s failed: %s", trip_id, exc)

    verb = "would save" if not args.run else "saved"
    logger.info("\nDone. %d graph trips, thinned %d (%d failed), %s %d bytes.",
                processed, thinned, failed, verb, saved)


if __name__ == "__main__":
    asyncio.run(main())
