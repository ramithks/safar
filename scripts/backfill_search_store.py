#!/usr/bin/env python3
"""
Backfill the search store from existing trips (docs/persistence_architecture.md
Phase 3).

Walks ``trip_states`` and mirrors each trip's current graph (or legacy
transport/stay blocks) into ``search_batches``/``search_results`` keyed by real
leg/node ids, then populates the reco index. Uses the same idempotent
``sync_trip_to_store`` the live post-edit sync uses, so:

  - re-running is safe: an unchanged trip is a no-op (content-hash gated),
  - it never touches ``trip_states.state`` — additive, best-effort per trip.

Safe by default: with NO flags it only COUNTS trips (never writes). Add --run to
mirror. Resumable + rate-friendly (one trip at a time).

    python scripts/backfill_search_store.py                 # dry run: how many trips
    python scripts/backfill_search_store.py --run           # backfill all
    python scripts/backfill_search_store.py --run --limit 50
    python scripts/backfill_search_store.py --run --trip <uuid>

Needs live Supabase (creds from .env). Run from the repo root.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill")


def _page(offset: int, limit: int, trip: str | None) -> list[dict]:
    from app.db.client import get_db
    q = get_db().table("trip_states").select("id, state").order("created_at")
    if trip:
        q = q.eq("id", trip)
    return q.range(offset, offset + limit - 1).execute().data or []


def _reco_ctx(state: dict) -> dict:
    req = (state or {}).get("request") or {}
    return {"budget": req.get("budget_tier") or "mid",
            "intent": {"interests": req.get("interests") or []}}


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill the search store from existing trips.")
    ap.add_argument("--run", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="max trips to process (0 = all)")
    ap.add_argument("--trip", type=str, default=None, help="a single trip id")
    ap.add_argument("--page-size", type=int, default=100)
    args = ap.parse_args()

    from app.db.client import run_db
    from app.trip_state.sync_store import sync_trip_to_store, graph_groups

    processed = mirrored = total_batches = total_results = 0
    offset = 0
    while True:
        page = await run_db(_page, offset, args.page_size, args.trip)
        if not page:
            break
        for row in page:
            trip_id, state = row["id"], row.get("state") or {}
            groups = graph_groups(state)
            processed += 1
            if not groups:
                continue
            if not args.run:
                logger.info("[dry] %s → %d scope(s) to mirror", trip_id, len(groups))
                mirrored += 1
            else:
                stats = await sync_trip_to_store(trip_id, state, ctx=_reco_ctx(state))
                b, r = stats.get("batches", 0), stats.get("results", 0)
                total_batches += b
                total_results += r
                if b or stats.get("skipped"):
                    mirrored += 1
                logger.info("[run] %s → %s", trip_id, stats)
            if args.limit and processed >= args.limit:
                break
        if args.limit and processed >= args.limit:
            break
        if args.trip:
            break
        offset += args.page_size

    verb = "would mirror" if not args.run else "mirrored"
    logger.info("\nDone. processed %d trips; %s %d with search data "
                "(%d batches, %d results written).",
                processed, verb, mirrored, total_batches, total_results)


if __name__ == "__main__":
    asyncio.run(main())
