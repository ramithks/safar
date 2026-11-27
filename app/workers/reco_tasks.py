"""
Recommendation background workers (Part 14.3).

All the *expensive* recommendation work happens here, OFF the request path:
  * embed_hotel      — 384-dim embedding, regenerated ONLY when the doc hash changes.
  * profile_hotel    — personality tags + SEO one-liner/summary: rules → cluster →
                       Gemini ONLY when the cluster is ambiguous (cheap by design).
  * nightly jobs     — expected_price (price fairness), destination_priors (cold
                       start), refresh_user_embeddings, fit_personality_centroids.

Every task is idempotent + best-effort: a failure logs and moves on; it never blocks
serving. Beat schedule is registered at import so the worker picks it up.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# ── Document + hash helpers (shared with §6.3 gate) ──────────────────────────────
def _doc_hash(h: dict) -> str:
    """Stale iff review_count / review_score / description changed (§6.3)."""
    sig = f"{h.get('review_count')}|{h.get('review_score')}|{hash(h.get('description') or '')}"
    return hashlib.sha1(sig.encode()).hexdigest()[:16]


def _hotel_doc(h: dict) -> str:
    """The text we embed / cluster. Denoised, vibe-carrying; no volatile price/date."""
    sub = h.get("subscores") or {}
    strong = sorted(((k, v) for k, v in sub.items() if isinstance(v, (int, float))),
                    key=lambda x: -x[1])[:2]
    landmarks = ", ".join(str(l.get("name", "")) for l in (h.get("landmarks") or [])[:3])
    return " ".join(filter(None, [
        f"{h.get('name','')}.",
        f"{h.get('property_type','')} in {h.get('city','')}.",
        f"{h.get('star_rating','')}star." if h.get("star_rating") else "",
        f"Guest score {h.get('review_score')}/10 ({h.get('review_label','')})." if h.get("review_score") else "",
        f"Personality: {', '.join(h.get('personality_tags') or [])}." if h.get("personality_tags") else "",
        f"Near {landmarks}." if landmarks else "",
        f"Strong on {', '.join(k for k, _ in strong)}." if strong else "",
        f"Guests say: {h.get('review_snippet','')}" if h.get("review_snippet") else "",
        f"Amenities: {', '.join((h.get('amenities') or [])[:8])}." if h.get("amenities") else "",
    ])).strip()


def _slug(name: str, city: str, hotel_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{name or ''} {city or ''}".lower()).strip("-")[:60]
    return f"{base}-{str(hotel_id)[:8]}"


# ── embed_hotel ──────────────────────────────────────────────────────────────────
@celery_app.task(name="safar.reco.embed_hotel", max_retries=0)
def embed_hotel_task(hotel_id: str) -> dict:
    from app.db.client import get_db
    from app.services.embeddings import embed_one

    db = get_db()
    row = db.table("hotels").select("*").eq("id", hotel_id).limit(1).execute()
    if not row.data:
        return {"hotel_id": hotel_id, "status": "not_found"}
    h = row.data[0]

    new_hash = _doc_hash(h)
    existing = db.table("hotel_embeddings").select("doc_hash").eq("hotel_id", hotel_id).limit(1).execute()
    if existing.data and existing.data[0].get("doc_hash") == new_hash:
        return {"hotel_id": hotel_id, "status": "skipped_unchanged"}  # §6.3 — the cheap path

    vec = asyncio.run(embed_one(_hotel_doc(h)))
    if not vec:
        return {"hotel_id": hotel_id, "status": "embed_unavailable"}

    from app.config import settings
    db.table("hotel_embeddings").upsert({
        "hotel_id": hotel_id, "embedding": vec, "doc_hash": new_hash,
        "model": settings.vertex_model_embedding,
    }, on_conflict="hotel_id").execute()
    return {"hotel_id": hotel_id, "status": "embedded"}


# ── profile_hotel ────────────────────────────────────────────────────────────────
_PROFILE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "personality_tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "one_liner": {"type": "STRING"},
        "seo_summary": {"type": "STRING"},
        "highlights": {"type": "ARRAY", "items": {"type": "STRING"}},
        "best_for": {"type": "ARRAY", "items": {"type": "STRING"}},
        "not_for": {"type": "ARRAY", "items": {"type": "STRING"}},
        "confidence": {"type": "NUMBER"},
    },
}


@celery_app.task(name="safar.reco.profile_hotel", max_retries=0)
def profile_hotel_task(hotel_id: str, force_llm: bool = False) -> dict:
    """Tags + SEO prose for a hotel. Tags come from rules → cluster (cheap).
    The LLM one_liner/seo_summary runs when the cluster is ambiguous OR when
    ``force_llm`` is set (the deliberate SEO-prose pass, ``--seo``)."""
    from app.db.client import get_db
    from app.reco import personality, vector_store

    db = get_db()
    row = db.table("hotels").select("*").eq("id", hotel_id).limit(1).execute()
    if not row.data:
        return {"hotel_id": hotel_id, "status": "not_found"}
    h = row.data[0]

    # §6.3 gate — skip if the evidence hasn't changed. In --seo mode we still
    # proceed when the prose is missing, so an unchanged-but-prose-less hotel gets
    # its SEO summary written.
    new_hash = _doc_hash(h)
    prof = db.table("hotel_profiles").select("confidence, generated_by, one_liner").eq("hotel_id", hotel_id).limit(1).execute()
    existing_hash = db.table("hotel_embeddings").select("doc_hash").eq("hotel_id", hotel_id).limit(1).execute()
    if prof.data and existing_hash.data and existing_hash.data[0].get("doc_hash") == new_hash:
        has_prose = bool(prof.data[0].get("one_liner"))
        if not force_llm or has_prose:
            return {"hotel_id": hotel_id, "status": "skipped_unchanged"}

    rule = personality.hotel_rule_tags(h)
    emb_row = db.table("hotel_embeddings").select("embedding").eq("hotel_id", hotel_id).limit(1).execute()
    embedding = None
    if emb_row.data:
        embedding = vector_store._parse_vec(emb_row.data[0]["embedding"]) if isinstance(emb_row.data[0]["embedding"], str) else emb_row.data[0]["embedding"]
    centroids = asyncio.run(vector_store.get_centroids())
    cluster, sim = personality.cluster_tags(embedding, centroids)

    tags = sorted(set(rule) | set(cluster))
    one_liner = seo = None
    best_for: list = []
    not_for: list = []
    generated_by = "rules+cluster"

    # Gemini for the ambiguous minority — or always, in the deliberate SEO pass.
    if force_llm or personality.needs_llm(rule, sim):
        from app.config import settings
        llm = _llm_profile(h)
        if llm:
            tags = sorted(set(tags) | {t for t in (llm.get("personality_tags") or []) if t in personality.HOTEL_VOCAB})
            one_liner, seo = llm.get("one_liner"), llm.get("seo_summary")
            best_for, not_for = llm.get("best_for") or [], llm.get("not_for") or []
            generated_by = f"llm:{settings.vertex_model_muscle}"

    db.table("hotel_profiles").upsert({
        "hotel_id": hotel_id, "personality_tags": tags,
        "one_liner": one_liner, "seo_summary": seo,
        "best_for": best_for, "not_for": not_for,
        "confidence": round(max(sim, 0.4), 3),
        "source_review_count": h.get("review_count"), "generated_by": generated_by,
    }, on_conflict="hotel_id").execute()

    # Fill the SEO slug on the master row if missing.
    if not h.get("slug"):
        db.table("hotels").update({"slug": _slug(h.get("name", ""), h.get("city", ""), hotel_id)}).eq("id", hotel_id).execute()
    return {"hotel_id": hotel_id, "status": "profiled", "tags": tags, "generated_by": generated_by}


def _llm_profile(h: dict) -> dict | None:
    from app.services.vertex import generate_json
    from app.reco.personality import HOTEL_VOCAB
    prompt = (
        "Assign personality tags to this hotel from the CONTROLLED VOCAB only: "
        f"{list(HOTEL_VOCAB)}. "
        "Pick 2-5 the evidence supports (reviews + subscores + type, NOT price). Then write a "
        "one_liner (<=22 words, specific, quote what guests praise, no fluff), a 180-300 word "
        "original factual seo_summary (third person, for a public page), and best_for/not_for "
        "(2-3 traveller types each). If evidence is thin, lower confidence and stay generic — "
        "never fabricate a view, award, or guest quote.\n\n"
        f"EVIDENCE: {_evidence(h)}"
    )
    try:
        # brain-tier (not muscle): flash-lite's thinking tokens eat the output budget
        # and truncate the 300-word summary into invalid JSON. brain is more reliable
        # here AND cheaper overall than muscle→muscle→brain escalation on every miss.
        # Headroom on tokens so thinking + the full summary both fit.
        return asyncio.run(generate_json(
            prompt, tier="brain", response_schema=_PROFILE_SCHEMA,
            temperature=0.3, max_output_tokens=2048, timeout=40.0,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[reco.profile_hotel] llm failed for %s: %s", h.get("id"), str(exc)[:140])
        return None


def _evidence(h: dict) -> dict:
    return {k: h.get(k) for k in (
        "name", "property_type", "city", "star_rating", "review_score", "review_label",
        "review_snippet", "subscores", "landmarks", "amenities", "distance_km",
        "social_proof", "family_friendly",
    ) if h.get(k) is not None}


@celery_app.task(name="safar.reco.embed_and_profile", max_retries=0)
def embed_and_profile_task(hotel_ids: list[str]) -> dict:
    """Convenience: embed then profile a batch (embed first so cluster has a vector)."""
    done = 0
    for hid in hotel_ids or []:
        try:
            embed_hotel_task(hid)
            profile_hotel_task(hid)
            done += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("[reco] embed+profile %s failed: %s", hid, str(exc)[:120])
    return {"processed": done}


# ── Nightly aggregation jobs ─────────────────────────────────────────────────────
@celery_app.task(name="safar.reco.refresh_expected_price", max_retries=0)
def refresh_expected_price_task() -> dict:
    """Price-fairness model: per (destination, mode) p10/p50/p90 from transport_options
    + hotels history. Feeds features.PriceFairness."""
    from app.db.client import get_db
    db = get_db()
    try:
        rows = db.table("transport_options").select("mode, destination, price").limit(50000).execute().data or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_data", "error": str(exc)[:120]}
    from collections import defaultdict
    buckets: dict = defaultdict(list)
    for r in rows:
        if r.get("price"):
            buckets[(r.get("mode"), r.get("destination"))].append(float(r["price"]))
    out = []
    for (mode, dest), prices in buckets.items():
        if len(prices) < 5:
            continue
        prices.sort()
        out.append({"scope": f"transport:{mode}", "key": dest or "?",
                    "p10": _pct(prices, 10), "p50": _pct(prices, 50), "p90": _pct(prices, 90),
                    "n": len(prices)})
    if out:
        db.table("expected_price").upsert(out, on_conflict="scope,key").execute()
    return {"status": "ok", "rows": len(out)}


@celery_app.task(name="safar.reco.refresh_destination_priors", max_retries=0)
def refresh_destination_priors_task() -> dict:
    """Cold-start priors: modal personality tags per destination from hotel_profiles
    of hotels people actually engaged with."""
    from app.db.client import get_db
    db = get_db()
    try:
        hotels = db.table("hotels").select("id, destination").limit(20000).execute().data or []
        profiles = db.table("hotel_profiles").select("hotel_id, personality_tags").limit(20000).execute().data or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_data", "error": str(exc)[:120]}
    dest_of = {h["id"]: h.get("destination") for h in hotels}
    from collections import Counter, defaultdict
    tally: dict = defaultdict(Counter)
    for p in profiles:
        dest = dest_of.get(p.get("hotel_id"))
        if dest:
            tally[dest].update(p.get("personality_tags") or [])
    out = [{"destination": d, "top_vibes": [t for t, _ in c.most_common(6)],
            "top_personas": []} for d, c in tally.items() if c]
    if out:
        db.table("destination_priors").upsert(out, on_conflict="destination").execute()
    return {"status": "ok", "destinations": len(out)}


@celery_app.task(name="safar.reco.fit_centroids", max_retries=0)
def fit_centroids_task() -> dict:
    """Re-fit personality centroids = mean of embeddings of hotels tagged with each
    personality. Cheap; runs occasionally."""
    from app.db.client import get_db
    from app.reco import vector_store
    db = get_db()
    try:
        profiles = db.table("hotel_profiles").select("hotel_id, personality_tags").limit(20000).execute().data or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "no_data", "error": str(exc)[:120]}
    ids = [p["hotel_id"] for p in profiles]
    embs = asyncio.run(vector_store.get_hotel_embeddings(ids))
    from collections import defaultdict
    acc: dict = defaultdict(list)
    for p in profiles:
        v = embs.get(str(p["hotel_id"]))
        if not v:
            continue
        for tag in (p.get("personality_tags") or []):
            acc[tag].append(v)
    out = []
    for tag, vecs in acc.items():
        if len(vecs) < 3:
            continue
        dim = len(vecs[0])
        mean = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
        out.append({"tag": tag, "centroid": mean, "n_exemplars": len(vecs)})
    if out:
        db.table("personality_centroids").upsert(out, on_conflict="tag").execute()
    return {"status": "ok", "centroids": len(out)}


def _pct(sorted_vals: list[float], p: int) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(round((p / 100.0) * (len(sorted_vals) - 1))))
    return round(sorted_vals[i], 2)

# NB: the beat schedule for these tasks lives in app/workers/celery_app.py (conf) so
# `celery beat` sees it regardless of import order — see that file's beat_schedule.
