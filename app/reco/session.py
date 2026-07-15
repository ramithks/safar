"""
Online session learning — the fast loop (minutes), from Part 9.2.

As a user clicks / wishlists inside one session we nudge that session's ranking
toward what they're gravitating to (e.g. keeps opening beachfront stays → boost the
``beachfront`` vibe for the rest of the session). Stored as a small decaying hash in
Redis, bounded and short-lived; it never persists unless it survives to
``distill_from_trip``. Best-effort — no Redis ⇒ everything here is a no-op and the
ranking simply isn't session-personalised.
"""
from __future__ import annotations

import logging

from app.reco import cache

logger = logging.getLogger(__name__)

_TTL = 1800          # 30 min — a session, not a profile
_MAX_TAGS = 24
_POS = {"click": 1.0, "wishlist_add": 2.0, "book_start": 3.0, "compare_open": 0.5}
_NEG = {"skip": -0.4, "wishlist_remove": -1.0}


def _bias_key(session_id: str) -> str:
    return cache.key("sess", session_id, "bias")


def record_engagement(session_id: str, item: dict, event: str) -> None:
    """Bump the tags of an engaged item for this session. Cheap, best-effort."""
    if not session_id or not item:
        return
    r = cache.raw_client()
    if r is None:
        return
    delta = _POS.get(event) or _NEG.get(event)
    if not delta:
        return
    tags = [str(t).lower() for t in (item.get("personality_tags") or [])]
    if not tags:
        return
    try:
        k = _bias_key(session_id)
        pipe = r.pipeline()
        for t in tags:
            pipe.hincrbyfloat(k, t, delta)
        pipe.expire(k, _TTL)
        pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reco.session] record failed: %s", exc)


def session_bias(session_id: str) -> dict[str, float]:
    """Return {tag: weight} learned this session (positive weights only), normalised
    to 0..1. Empty when nothing learned / Redis down."""
    if not session_id:
        return {}
    r = cache.raw_client()
    if r is None:
        return {}
    try:
        raw = r.hgetall(_bias_key(session_id)) or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reco.session] read failed: %s", exc)
        return {}
    scores = {t: float(v) for t, v in raw.items() if _safe_pos(v)}
    if not scores:
        return {}
    hi = max(scores.values()) or 1.0
    return {t: round(v / hi, 3) for t, v in sorted(scores.items(), key=lambda x: -x[1])[:_MAX_TAGS]}


def apply_bias(ctx: dict, session_id: str, threshold: float = 0.5) -> dict:
    """Fold the session's learned vibes into the ranking context (union, capped).
    Returns ctx (mutated) so callers can chain."""
    bias = session_bias(session_id)
    if not bias:
        return ctx
    boosted = [t for t, w in bias.items() if w >= threshold]
    if boosted:
        vibes = list(ctx.get("vibes") or [])
        for t in boosted:
            if t not in vibes:
                vibes.append(t)
        ctx["vibes"] = vibes[:16]
        ctx["_session_boost"] = boosted
    return ctx


def _safe_pos(v) -> bool:
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False
