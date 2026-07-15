"""
The ranker: features × persona weights → one internal score, then order.

- ``_weighted_present`` (from ``app.services.scoring``) renormalises over the signals
  an item actually has, so a thin-provider listing is judged on what it carries, not
  buried for what it lacks. Semantic similarity is just another feature in the weight
  vector — when embeddings are absent it's ``None`` and drops out, so the vector layer
  is strictly additive and zero-risk.
- Provider confidence is a gentle multiplier (0.85–1.0), never a hammer that nukes a
  cheap-but-thin source.
- The raw score is INTERNAL (``_reco_score``); callers expose only the ``match``
  bucket + the deterministic "why" (see app.reco.explain). Numbers never leak.
- ``mmr_diversify`` gives a varied first screen / alternatives set (Part 10) instead
  of five near-identical results.
"""
from __future__ import annotations

from app.services.scoring import (
    _weighted_present,
    _HOTEL_PROVIDER_CONFIDENCE, _FLIGHT_PROVIDER_CONFIDENCE, _DEFAULT_CONFIDENCE,
)
from app.reco import personas
from app.reco.features import (
    SetStats, hotel_components, transport_components, _hotel_price,
)


def match_bucket(score: float) -> str:
    """Bucketed match label — never the raw number (Part 15)."""
    if score >= 78:
        return "high"
    if score >= 62:
        return "good"
    return "fair"


def _provider_tilt(conf: float) -> float:
    return 0.85 + 0.15 * conf


def rank_hotels(hotels: list[dict], ctx: dict | None = None) -> list[dict]:
    """Annotate + sort hotels for a traveller context. Mutates in place, returns."""
    if not hotels:
        return hotels
    ctx = ctx or {}
    persona = ctx.get("persona") or personas.derive_persona(ctx.get("intent") or {})
    weights = personas.resolve_hotel_weights(persona, ctx.get("intent"))
    stats = SetStats.for_hotels(hotels)
    for h in hotels:
        comps = hotel_components(h, stats, ctx)
        conf = _HOTEL_PROVIDER_CONFIDENCE.get(h.get("source"), _DEFAULT_CONFIDENCE)
        h["_reco_components"] = comps
        h["_reco_score"] = round(_weighted_present(comps, weights) * _provider_tilt(conf), 1)
        h["match"] = match_bucket(h["_reco_score"])
    hotels.sort(key=lambda x: x.get("_reco_score", 0.0), reverse=True)
    return hotels


def rank_transport(items: list[dict], ctx: dict | None = None) -> list[dict]:
    """Annotate + sort transport options (any mode) for a traveller context."""
    if not items:
        return items
    ctx = ctx or {}
    persona = ctx.get("persona") or personas.derive_persona(ctx.get("intent") or {})
    weights = personas.resolve_transport_weights(persona, ctx.get("intent"))
    stats = SetStats.for_transport(items)
    for t in items:
        comps = transport_components(t, stats, ctx)
        conf = _FLIGHT_PROVIDER_CONFIDENCE.get(t.get("source"), _DEFAULT_CONFIDENCE)
        t["_reco_components"] = comps
        t["_reco_score"] = round(_weighted_present(comps, weights) * _provider_tilt(conf), 1)
        t["match"] = match_bucket(t["_reco_score"])
    items.sort(key=lambda x: x.get("_reco_score", 0.0), reverse=True)
    return items


# ── Diversity (MMR) ──────────────────────────────────────────────────────────────
def _price_band(price: float | None, lo: float, hi: float) -> int:
    if price is None or hi <= lo:
        return 0
    return int(min(2, max(0, (price - lo) / ((hi - lo) / 3 or 1))))


def hotel_similarity(a: dict, b: dict) -> float:
    """0..1 similarity for diversification — same personality + price band + area."""
    ta = {str(t).lower() for t in (a.get("personality_tags") or [])}
    tb = {str(t).lower() for t in (b.get("personality_tags") or [])}
    tag = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    same_area = 1.0 if (a.get("city") and a.get("city") == b.get("city")
                        and (a.get("area") or a.get("address")) == (b.get("area") or b.get("address"))) else 0.0
    pa, pb = _hotel_price(a), _hotel_price(b)
    same_price = 1.0 if (pa and pb and abs(pa - pb) / max(pa, pb) < 0.15) else 0.0
    return max(0.0, min(1.0, 0.5 * tag + 0.25 * same_area + 0.25 * same_price))


def mmr_diversify(items: list[dict], k: int, sim_fn=hotel_similarity, lambda_: float = 0.72) -> list[dict]:
    """Maximal-marginal-relevance reorder: keep relevance high but avoid showing
    near-duplicates back to back. Returns the full list, just reordered."""
    if len(items) <= 2:
        return items
    selected = [items[0]]
    pool = items[1:]
    while pool and len(selected) < k:
        best, best_val = None, float("-inf")
        for c in pool:
            rel = c.get("_reco_score", 0.0) / 100.0
            div = max((sim_fn(c, s) for s in selected), default=0.0)
            val = lambda_ * rel - (1.0 - lambda_) * div
            if val > best_val:
                best, best_val = c, val
        selected.append(best)
        pool.remove(best)
    return selected + pool
