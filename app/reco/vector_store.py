"""
pgvector access for the recommendation engine.

Thin, best-effort wrappers over the vector tables (Part 12.4). Every function
degrades to an empty/None result when pgvector isn't provisioned, the table is
missing, or a query fails — the ranker then simply omits the semantic feature
(renormalised out), so the engine runs identically with or without the vector layer.

Similarity search uses a SQL function ``match_hotels`` (defined in
``migrations/reco_engine.sql``) invoked via PostgREST RPC, because pgvector ANN can't
be expressed through the REST query builder.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_CENTROIDS: dict | None = None
_CENTROIDS_AT = 0.0
_CENTROID_TTL = 3600


# ── Hotel embeddings ─────────────────────────────────────────────────────────────
def _hotel_embeddings_sync(hotel_ids: list[str]) -> dict[str, list[float]]:
    from app.db.client import get_db
    res = (
        get_db().table("hotel_embeddings")
        .select("hotel_id, embedding").in_("hotel_id", hotel_ids).execute()
    )
    out = {}
    for row in (res.data or []):
        emb = row.get("embedding")
        if isinstance(emb, str):  # pgvector may serialise as '[...]'
            emb = _parse_vec(emb)
        if emb:
            out[str(row["hotel_id"])] = emb
    return out


async def get_hotel_embeddings(hotel_ids: list[str]) -> dict[str, list[float]]:
    if not hotel_ids:
        return {}
    try:
        from app.db.client import run_db
        return await run_db(_hotel_embeddings_sync, hotel_ids)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[vector_store] hotel emb fetch failed: %s", exc)
        return {}


async def attach_embeddings(hotels: list[dict]) -> list[dict]:
    """Attach ``_embedding`` to candidates that carry a resolvable master hotel id.
    Best-effort: candidates without an id (or not yet embedded) are left as-is, and
    the semantic feature is simply absent for them."""
    ids = [str(h["hotel_id"]) for h in hotels if h.get("hotel_id")]
    ids += [str(h["id"]) for h in hotels if h.get("id") and not h.get("hotel_id")]
    if not ids:
        return hotels
    embs = await get_hotel_embeddings(list(dict.fromkeys(ids)))
    if not embs:
        return hotels
    for h in hotels:
        key = str(h.get("hotel_id") or h.get("id") or "")
        if key in embs:
            h["_embedding"] = embs[key]
    return hotels


# ── Traveller embedding ──────────────────────────────────────────────────────────
def _traveller_emb_sync(user_id: str):
    from app.db.client import get_db
    res = (
        get_db().table("traveller_profiles")
        .select("embedding").eq("user_id", user_id).limit(1).execute()
    )
    if not res.data:
        return None
    emb = res.data[0].get("embedding")
    return _parse_vec(emb) if isinstance(emb, str) else emb


async def get_traveller_embedding(user_id: str):
    if not user_id:
        return None
    try:
        from app.db.client import run_db
        return await run_db(_traveller_emb_sync, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[vector_store] traveller emb fetch failed: %s", exc)
        return None


# ── Nearest hotels (semantic "similar stays" / SEO links) ────────────────────────
def _match_hotels_sync(embedding: list[float], destination: str | None, k: int) -> list[dict]:
    from app.db.client import get_db
    res = get_db().rpc("match_hotels", {
        "query_embedding": embedding, "match_destination": destination, "match_count": k,
    }).execute()
    return res.data or []


async def nearest_hotels(embedding: list[float], destination: str | None = None, k: int = 6) -> list[dict]:
    if not embedding:
        return []
    try:
        from app.db.client import run_db
        return await run_db(_match_hotels_sync, embedding, destination, k)
    except Exception as exc:  # noqa: BLE001 — RPC/pgvector absent → no similar-stays
        logger.debug("[vector_store] match_hotels failed: %s", exc)
        return []


# ── Personality centroids ────────────────────────────────────────────────────────
def _centroids_sync() -> dict[str, list[float]]:
    from app.db.client import get_db
    res = get_db().table("personality_centroids").select("tag, centroid").execute()
    out = {}
    for row in (res.data or []):
        c = row.get("centroid")
        c = _parse_vec(c) if isinstance(c, str) else c
        if c:
            out[row["tag"]] = c
    return out


async def get_centroids() -> dict[str, list[float]]:
    """Cached in-process (1h) — centroids change only when re-fit offline."""
    global _CENTROIDS, _CENTROIDS_AT
    if _CENTROIDS is not None and (time.monotonic() - _CENTROIDS_AT) < _CENTROID_TTL:
        return _CENTROIDS
    try:
        from app.db.client import run_db
        _CENTROIDS = await run_db(_centroids_sync)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[vector_store] centroids fetch failed: %s", exc)
        _CENTROIDS = {}
    _CENTROIDS_AT = time.monotonic()
    return _CENTROIDS


def _parse_vec(raw):
    try:
        import json
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        try:
            return [float(x) for x in str(raw).strip("[]").split(",") if x.strip()]
        except Exception:  # noqa: BLE001
            return None
