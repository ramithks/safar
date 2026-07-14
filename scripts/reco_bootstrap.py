#!/usr/bin/env python3
"""
Recommendation-engine bootstrap — one command to light up the vector + SEO layers.

Steps (run in order; each is idempotent + resumable):

  1. --check            Verify the migration is applied (tables + pgvector + RPC).
  2. --seed-centroids   Seed personality_centroids from TEXT anchors, so clustering
                        works immediately — no hand-labelled hotels needed first.
                        (fit_centroids later refines these from real tagged hotels.)
  3. --backfill         embed_hotel (doc-hash gated) + profile_hotel for every hotel.
                        Resumable: unchanged hotels are skipped by the §6.3 gate.

Safe by default: with NO flags it only runs --check (never writes). Combine flags to
do more, e.g. seed then backfill 50 Udaipur hotels:

    python scripts/reco_bootstrap.py --seed-centroids --backfill --limit 50 --destination Udaipur

Needs live Supabase + Vertex (creds from .env). Run from the repo root.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tables the migration creates — presence = migration applied.
_RECO_TABLES = [
    "hotel_profiles", "hotel_embeddings", "personality_centroids",
    "traveller_profiles", "transport_options", "rec_feedback",
    "ranking_weights", "expected_price", "destination_priors",
]

# Text anchors for each hotel personality — one descriptive sentence per tag. Embedded
# + stored as the initial centroid so cluster_tags() has something to match against on
# day one. Kept in sync with app.reco.personality.HOTEL_VOCAB.
_ANCHORS: dict[str, str] = {
    "luxury_palace": "an opulent five-star palace hotel with grand suites, butler service and fine dining, the ultimate in luxury",
    "heritage_royal": "a royal heritage haveli and historic fort palace, regal old-world architecture from the maharaja era",
    "boutique_design": "a stylish boutique design hotel with curated interiors, intimate and architect-designed",
    "instagram_icon": "an incredibly photogenic instagrammable hotel with iconic views and a stunning picture-perfect rooftop",
    "hidden_gem": "a hidden gem and underrated local find, off the beaten path, quietly loved by the few who stay",
    "budget_explorer": "an affordable budget hotel, great value with a clean basic room at a low price",
    "backpacker_social": "a lively backpacker hostel with dorm beds and a social common room to meet other travellers",
    "family_comfort": "a comfortable family hotel with spacious kid-friendly rooms, a kitchen and connecting rooms",
    "workcation_ready": "a workcation stay with fast wifi, a proper desk workspace and a quiet spot for calls, good for remote work",
    "lake_view": "a lakefront hotel with beautiful lake views and sunsets over the water",
    "beachfront": "a beachfront resort steps from the sea with ocean views and a sandy beach",
    "mountain_view": "a mountain-view hotel in the hills with valley vistas and himalayan scenery",
    "city_central": "a centrally located hotel in the heart of the city, walkable to everything downtown",
    "nature_retreat": "a nature retreat surrounded by forest and greenery, a serene eco lodge in the wilderness",
    "peaceful_retreat": "a peaceful, quiet and tranquil retreat, restful and calm, far from the noise",
    "party_hub": "a vibrant party hotel with nightlife, a buzzing bar and a lively social scene",
    "foodie_haven": "a food lover's hotel with a renowned restaurant, standout local cuisine and a great breakfast",
    "wellness_spa": "a wellness spa resort with yoga, massage and meditation, rejuvenating and holistic",
    "romantic_couples": "a romantic couples retreat with a honeymoon suite and candlelit dinners, intimate and private",
    "business_express": "an efficient business hotel near the airport with conference facilities, professional and executive",
    "pet_friendly": "a pet-friendly hotel that warmly welcomes dogs and other pets",
    "accessible": "a wheelchair-accessible hotel with step-free access, an elevator and disability-friendly rooms",
    "eco_conscious": "an eco-friendly and sustainable hotel, green and solar-powered with a low environmental impact",
    "long_stay": "a long-stay apartment hotel with a kitchenette and monthly rates, a home away from home",
}


def _db():
    from app.db.client import get_db
    return get_db()


# ── 1. check ──────────────────────────────────────────────────────────────────
def check() -> bool:
    print("── Migration check ──")
    ok = True
    for t in _RECO_TABLES:
        try:
            _db().table(t).select("*").limit(1).execute()
            print(f"  ✓ {t}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ✗ {t:<22} {type(e).__name__}: {str(e)[:70]}")
    # hotels ALTER columns + pgvector RPC
    try:
        _db().table("hotels").select("slug").limit(1).execute()
        print("  ✓ hotels.slug column")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  ✗ hotels.slug column — {str(e)[:70]}")
    try:
        _db().rpc("match_hotels", {"query_embedding": [0.0] * 384, "match_destination": None, "match_count": 1}).execute()
        print("  ✓ match_hotels RPC (pgvector live)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  ✗ match_hotels RPC — {str(e)[:70]}")
    print(f"\n{'✓ migration applied' if ok else '✗ run migrations/2026-07-13_reco_engine.sql first'}\n")
    return ok


# ── 2. seed centroids ─────────────────────────────────────────────────────────
def seed_centroids(dry_run: bool) -> None:
    from app.reco.personality import HOTEL_VOCAB
    from app.services.embeddings import embed_texts

    missing = [t for t in HOTEL_VOCAB if t not in _ANCHORS]
    if missing:
        print(f"  ! no anchor for: {missing} (skipped)")
    tags = [t for t in HOTEL_VOCAB if t in _ANCHORS]
    print(f"── Seeding {len(tags)} personality centroids ──")

    vecs = asyncio.run(embed_texts([_ANCHORS[t] for t in tags]))
    if not vecs:
        print("  ✗ embeddings unavailable (Vertex not configured?) — cannot seed. Aborting.")
        return
    rows = [{"tag": t, "centroid": v, "n_exemplars": 1} for t, v in zip(tags, vecs)]
    if dry_run:
        print(f"  [dry-run] would upsert {len(rows)} centroids (dim={len(vecs[0])})")
        return
    _db().table("personality_centroids").upsert(rows, on_conflict="tag").execute()
    print(f"  ✓ seeded {len(rows)} centroids (dim={len(vecs[0])})")


# ── 3. backfill embed + profile ────────────────────────────────────────────────
def backfill(limit: int | None, destination: str | None, dry_run: bool, seo: bool = False) -> None:
    from app.workers.reco_tasks import embed_hotel_task, profile_hotel_task

    page, size = 0, 200
    seen = embedded = profiled = skipped = failed = 0
    t0 = time.time()
    print(f"── Backfill embed+profile{' +SEO-prose' if seo else ''}"
          f"{f' · {destination}' if destination else ''}"
          f"{f' · limit {limit}' if limit else ''} ──")
    while True:
        q = _db().table("hotels").select("id").order("id").range(page * size, page * size + size - 1)
        if destination:
            q = q.eq("destination", destination)
        rows = q.execute().data or []
        if not rows:
            break
        for r in rows:
            if limit and seen >= limit:
                break
            hid = r["id"]
            seen += 1
            if dry_run:
                continue
            try:
                e = embed_hotel_task(hid)
                p = profile_hotel_task(hid, force_llm=seo)
                embedded += e.get("status") == "embedded"
                profiled += p.get("status") == "profiled"
                skipped += "skipped" in (e.get("status") or "") and "skipped" in (p.get("status") or "")
            except Exception as ex:  # noqa: BLE001
                failed += 1
                print(f"  ! {hid}: {type(ex).__name__}: {str(ex)[:80]}")
            if seen % 25 == 0:
                print(f"  … {seen} seen | {embedded} embedded | {profiled} profiled | "
                      f"{skipped} skipped | {failed} failed | {time.time()-t0:.0f}s")
        if limit and seen >= limit:
            break
        page += 1

    if dry_run:
        print(f"  [dry-run] would process {seen} hotels")
    else:
        print(f"\n✓ done: {seen} hotels | {embedded} embedded | {profiled} profiled | "
              f"{skipped} unchanged | {failed} failed | {time.time()-t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description="Recommendation engine bootstrap")
    ap.add_argument("--check", action="store_true", help="verify migration applied")
    ap.add_argument("--seed-centroids", action="store_true", help="seed personality centroids from text anchors")
    ap.add_argument("--backfill", action="store_true", help="embed + profile all hotels")
    ap.add_argument("--seo", action="store_true", help="also LLM-generate one_liner/seo_summary per hotel (costs a muscle call each, doc-hash gated)")
    ap.add_argument("--limit", type=int, default=None, help="cap hotels processed (backfill)")
    ap.add_argument("--destination", type=str, default=None, help="only this destination (backfill)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    args = ap.parse_args()

    do_any = args.check or args.seed_centroids or args.backfill
    if args.check or not do_any:
        if not check() and not do_any:
            return
    if args.seed_centroids:
        seed_centroids(args.dry_run)
    if args.backfill:
        backfill(args.limit, args.destination, args.dry_run, seo=args.seo)


if __name__ == "__main__":
    main()
