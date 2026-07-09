#!/usr/bin/env python3
"""
Crawl the goSTOPS catalog + hostel details and upsert them into the `hostels`
master table (static data only — no live prices).

Flow:
  1. catalog()                → all active hostels (one request)
  2. hostel_detail(slug)      → per-hostel static detail (RSC parse)
  3. upsert rows into `hostels` on (source, source_hostel_id)

Usage:
  python scripts/scrape_gostops.py                 # full crawl + upsert
  python scripts/scrape_gostops.py --dry-run       # fetch + print, no DB write
  python scripts/scrape_gostops.py --limit 3       # first N hostels (testing)
  python scripts/scrape_gostops.py --no-detail     # catalog-only (fast, shallow)

Exit code 0 if every hostel was processed, 1 if any detail fetch failed.
"""
import argparse
import asyncio
import json
import logging
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.gostops import client, normalize  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("scrape_gostops")

# Politeness: goSTOPS is a partner, but don't hammer the RSC endpoint.
_CONCURRENCY = 4


async def _fetch_detail(cat: dict, sem: asyncio.Semaphore) -> tuple[dict, dict | None]:
    """Return (catalog_entry, detail_or_None). Never raises — logs and skips."""
    city = (cat.get("city") or {}).get("name") or (cat.get("city") or {}).get("slug")
    slug = cat.get("slug")
    if not city or not slug:
        logger.warning("skip %s: missing city/slug", cat.get("id"))
        return cat, None
    city_path = urllib.parse.quote(city)
    async with sem:
        try:
            detail = await client.hostel_detail(city_path, slug)
            return cat, detail
        except client.GostopsError as exc:
            logger.error("detail failed for %s (%s): %s", slug, cat.get("id"), exc)
            return cat, None


async def crawl(limit: int | None, with_detail: bool) -> tuple[list[dict], int]:
    cats = await client.catalog()
    logger.info("catalog: %d hostels", len(cats))
    if limit:
        cats = cats[:limit]

    rows: list[dict] = []
    failures = 0

    if not with_detail:
        return [normalize.hostel_row(c) for c in cats], 0

    sem = asyncio.Semaphore(_CONCURRENCY)
    results = await asyncio.gather(*(_fetch_detail(c, sem) for c in cats))
    for cat, detail in results:
        if detail is None:
            failures += 1
        rows.append(normalize.hostel_row(cat, detail))
    return rows, failures


def upsert(rows: list[dict]) -> None:
    from app.db.client import get_db

    db = get_db()
    # Chunk to keep each request small and resilient. Small chunks + a short
    # retry ride out flaky links (mobile hotspots can corrupt large TLS records).
    chunk_size = 3
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        attempts = 6
        for attempt in range(attempts):
            try:
                db.table("hostels").upsert(
                    chunk, on_conflict="source,source_hostel_id"
                ).execute()
                break
            except Exception as exc:
                if attempt == attempts - 1:
                    raise
                logger.warning("upsert chunk %d retry %d: %s", i, attempt + 1, exc)
                time.sleep(2)
        logger.info("upserted %d rows (%d/%d)", len(chunk), i + len(chunk), len(rows))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="fetch + print, no DB write")
    ap.add_argument("--limit", type=int, default=None, help="only first N hostels")
    ap.add_argument("--no-detail", action="store_true", help="catalog-only, skip RSC detail")
    args = ap.parse_args()

    rows, failures = await crawl(args.limit, with_detail=not args.no_detail)
    logger.info("built %d rows (%d detail failures)", len(rows), failures)

    if args.dry_run:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 1 if failures else 0

    if rows:
        upsert(rows)
        logger.info("done: %d hostels in master table", len(rows))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
