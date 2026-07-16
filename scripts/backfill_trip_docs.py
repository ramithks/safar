#!/usr/bin/env python3
"""
Backfill TripDocs from legacy trip_states.state (docs/tripstate_removal_plan.md
step 4).

Walks ``trip_states`` and converts every row the new read path would consider
stale (no doc yet, or a legacy write bumped last_seq since conversion) through
``app.trips_doc.convert.doc_from_legacy_row`` — the same lazy conversion the
v3 GET performs, so a backfilled trip and a lazily-converted one are identical.

  - re-running is safe: an up-to-date doc is skipped (last_seq_converted gate),
  - never touches ``trip_states.state`` or ``last_seq`` — additive per trip.

MUST run while app/trip_state still exists (conversion hydrates thin graphs
through the old retrieval layer). This is the old machinery's last job.

Safe by default: with NO flags it only COUNTS trips. Add --run to convert.

    python scripts/backfill_trip_docs.py                 # dry run: how many need it
    python scripts/backfill_trip_docs.py --run           # convert all
    python scripts/backfill_trip_docs.py --run --limit 50
    python scripts/backfill_trip_docs.py --run --trip <uuid>

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
logger = logging.getLogger("backfill-docs")

_PAGE = 50


def _page(offset: int, trip: str | None) -> list[dict]:
    from app.db.client import get_db
    q = (get_db().table("trip_states")
         .select("id, user_id, title, status, visibility, progress, last_seq, doc, state, created_at, updated_at")
         .order("created_at"))
    if trip:
        q = q.eq("id", trip)
    return q.range(offset, offset + _PAGE - 1).execute().data or []


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="actually convert (default: count only)")
    ap.add_argument("--force", action="store_true",
                    help="reconvert even up-to-date docs (after a serializer change)")
    ap.add_argument("--limit", type=int, default=0, help="max trips to convert")
    ap.add_argument("--trip", help="single trip id")
    args = ap.parse_args()

    from app.db.client import run_db
    from app.trips_doc import convert, store

    offset = converted = skipped = failed = pending = 0
    while True:
        rows = await run_db(_page, offset, args.trip)
        if not rows:
            break
        offset += len(rows)
        for row in rows:
            has_legacy = bool((row.get("state") or {}).get("meta")
                              or (row.get("state") or {}).get("graph")
                              or (row.get("state") or {}).get("transport"))
            if not (convert.needs_conversion(row) or (args.force and has_legacy)):
                skipped += 1
                continue
            pending += 1
            if not args.run:
                continue
            trip_id = str(row["id"])
            try:
                doc = await convert.doc_from_legacy_row(row)
                await store.put_doc(trip_id, doc)
                converted += 1
                logger.info("converted %s (%s)", trip_id, doc.get("title"))
            except Exception as exc:  # noqa: BLE001 — one bad trip must not stop the walk
                failed += 1
                logger.error("FAILED %s: %s", trip_id, exc)
            if args.limit and converted >= args.limit:
                break
        if args.limit and converted >= args.limit:
            break
        if args.trip:
            break

    if args.run:
        logger.info("done: %d converted, %d already current, %d failed",
                    converted, skipped, failed)
    else:
        logger.info("dry run: %d need conversion, %d already current (add --run)",
                    pending, skipped)


if __name__ == "__main__":
    asyncio.run(main())
