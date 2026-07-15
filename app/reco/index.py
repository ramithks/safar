"""
Recommendation index — precomputed scores per (trip, result) so the copilot and
render paths never re-rank on the hot path (docs/persistence_architecture.md §7).

The reco engine (``app.reco.rank``) computes scores once when a search batch
finalizes and UPSERTs them here; ``RetrievalService.resolve_options`` joins
these rows so chat gets scores + explanations for free. Best-effort, off the hot
path — mirrors ``reco.transport_store``.

Phase 1 ships the store; wiring the batch-finalize hook that populates it is
Phase 2. Writes are sync (run via ``run_db``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RecommendationIndex:
    @staticmethod
    def _rows(trip_id: str, scope_id: str, scored: list[dict], persona: str) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for s in scored:
            if not s.get("result_id"):
                continue
            rows.append({
                "trip_id": trip_id,
                "scope_id": scope_id or "",
                "batch_id": s.get("batch_id"),
                "result_id": s["result_id"],
                "score": float(s.get("score") or 0.0),
                "subscores": s.get("subscores") or {},
                "explanation": s.get("explanation"),
                "signals": s.get("signals") or {},
                "persona": persona or "default",
                "computed_at": now,
            })
        return rows

    @staticmethod
    def upsert_scores(trip_id: str, scope_id: str, scored: list[dict],
                      persona: str = "default") -> int:
        """UPSERT precomputed scores for a batch's results. `scored` items:
        {result_id, batch_id, score, subscores, explanation, signals}."""
        rows = RecommendationIndex._rows(trip_id, scope_id, scored, persona)
        if not rows:
            return 0
        from app.db.client import get_db
        (get_db().table("trip_recommendations")
         .upsert(rows, on_conflict="trip_id,result_id,persona").execute())
        return len(rows)

    @staticmethod
    def load_scores(trip_id: str, scope_id: str | None = None,
                    persona: str = "default") -> dict[str, dict]:
        """Scores keyed by result_id, for the resolve/render path."""
        from app.db.client import get_db
        q = (get_db().table("trip_recommendations")
             .select("result_id, score, subscores, explanation")
             .eq("trip_id", trip_id).eq("persona", persona))
        if scope_id is not None:
            q = q.eq("scope_id", scope_id)
        return {r["result_id"]: r for r in (q.execute().data or [])}


def _score_batch_sync(trip_id: str, batch_id: str, scope_id: str, kind: str,
                      ctx: dict | None) -> int:
    """Read a finalized batch's results, rank them with the reco engine, and
    UPSERT scores keyed by result_id. Runs on the DB executor."""
    from app.db.client import get_db
    from app.reco import rank

    res = (get_db().table("search_results")
           .select("id, item_type, raw").eq("batch_id", batch_id).execute())
    rows = res.data or []
    if not rows:
        return 0

    # Rank per family; rank_* mutate the raw dicts in place adding _reco_score.
    by_id = {r["id"]: (r.get("raw") or {}) for r in rows}
    transport = [raw for r, raw in ((r, r.get("raw") or {}) for r in rows)
                 if r.get("item_type") in ("flight", "train", "bus")]
    hotels = [r.get("raw") or {} for r in rows if r.get("item_type") == "hotel"]
    if transport:
        rank.rank_transport(transport, ctx)
    if hotels:
        rank.rank_hotels(hotels, ctx)

    scored = []
    for rid, raw in by_id.items():
        if "_reco_score" not in raw:
            continue
        scored.append({
            "result_id": rid,
            "batch_id": batch_id,
            "score": raw.get("_reco_score"),
            "subscores": {k: v for k, v in (raw.get("_reco_components") or {}).items()
                          if v is not None},
            "explanation": (f"{raw['match']} match" if raw.get("match") else None),
        })
    return RecommendationIndex.upsert_scores(trip_id, scope_id, scored)


async def score_batch(trip_id: str, batch_id: str, *, scope_id: str = "",
                      kind: str = "transport", ctx: dict | None = None) -> int:
    """Best-effort: compute + persist reco scores for a batch. Fire-and-forget
    after the batch finalizes; a failure just leaves the index without this batch."""
    if not batch_id:
        return 0
    try:
        # Resilient: scoring is an idempotent upsert (PK trip_id,result_id,persona),
        # so it can be replayed on a fresh client through the TLS storms the backfill
        # provokes. Off the request path.
        from app.db.client import run_db_resilient
        n = await run_db_resilient(
            lambda: _score_batch_sync(trip_id, batch_id, scope_id, kind, ctx),
            label=f"score_batch:{batch_id}", max_seconds=90)
        logger.info("[reco.index] scored %d results for batch %s (trip %s)", n, batch_id, trip_id)
        return n
    except Exception as exc:  # noqa: BLE001 — index is best-effort
        logger.warning("[reco.index] score_batch failed for %s: %s", trip_id, exc)
        return 0


async def save_scores(trip_id: str, scope_id: str, scored: list[dict],
                      persona: str = "default") -> int:
    """Best-effort async wrapper — call fire-and-forget after a batch finalizes."""
    if not scored:
        return 0
    try:
        from app.db.client import run_db
        n = await run_db(RecommendationIndex.upsert_scores, trip_id, scope_id, scored, persona)
        logger.info("[reco.index] saved %d scores for trip %s scope %s", n, trip_id, scope_id)
        return n
    except Exception as exc:  # noqa: BLE001 — index is best-effort
        logger.warning("[reco.index] save failed for %s: %s", trip_id, exc)
        return 0
