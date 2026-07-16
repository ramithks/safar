"""
Backfill slugs for every hotel that lacks one (growth OS Phase 4 —
"launch ALL pages in the sitemap").

A hotel page renders fine from master-table data alone (the LLM profile only
enriches it), so the slug — the page's identity — must not wait for profile
generation. Same recipe as reco_tasks._slug: slugified name+city + first 8
chars of the id (guaranteed unique).

Usage:
    python scripts/backfill_hotel_slugs.py          # dry run — counts only
    python scripts/backfill_hotel_slugs.py --run    # write slugs
"""
import re
import sys

sys.path.insert(0, ".")


def _slug(name: str, city: str, hotel_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{name or ''} {city or ''}".lower()).strip("-")[:60]
    return f"{base}-{str(hotel_id)[:8]}"


def main(write: bool) -> None:
    from app.db.client import get_db

    db = get_db()
    done = 0
    scanned = 0
    while True:
        rows = (db.table("hotels").select("id, name, city")
                .is_("slug", "null").limit(500).execute().data or [])
        if not rows:
            break
        scanned += len(rows)
        if not write:
            print(f"would slug {len(rows)} more (scanned {scanned})…")
            if len(rows) < 500:
                break
            # dry run can't page past unslugged rows without writing — estimate via count
            total = (db.table("hotels").select("id", count="exact")
                     .is_("slug", "null").limit(1).execute().count)
            print(f"TOTAL missing slugs: {total}")
            return
        for r in rows:
            db.table("hotels").update({"slug": _slug(r.get("name") or "hotel",
                                                     r.get("city") or "", r["id"])}) \
                .eq("id", r["id"]).execute()
            done += 1
        print(f"slugged {done} so far…")
    print(f"DONE — {done} slugs written" if write else "nothing to do")


if __name__ == "__main__":
    main(write="--run" in sys.argv)
