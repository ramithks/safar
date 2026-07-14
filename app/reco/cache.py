"""
Redis JSON cache — the cheap layer that keeps the recommender cheap.

Everything here is BEST-EFFORT: if Redis is unreachable, ``get`` returns ``None`` and
``set`` is a no-op, so a cache outage silently degrades to "recompute" and never
fails a request. Mirrors the lazy-client pattern in ``app.trip_state.generator``.
"""
from __future__ import annotations

import hashlib
import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_redis = None
_redis_down = False


def _client():
    global _redis, _redis_down
    if _redis_down:
        return None
    if _redis is None:
        try:
            import redis as redis_lib
            _redis = redis_lib.from_url(settings.redis_url, decode_responses=True)
        except Exception as exc:  # noqa: BLE001 — cache is optional
            logger.warning("[reco.cache] redis unavailable: %s", exc)
            _redis_down = True
            return None
    return _redis


def key(*parts: str) -> str:
    return "reco:" + ":".join(str(p) for p in parts)


def digest(obj) -> str:
    """Stable short hash of any JSON-able object — for cache keys / doc hashes."""
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def get_json(k: str):
    r = _client()
    if r is None:
        return None
    try:
        raw = r.get(k)
        return json.loads(raw) if raw else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reco.cache] get %s failed: %s", k, exc)
        return None


def set_json(k: str, value, ttl: int = 600) -> None:
    r = _client()
    if r is None:
        return
    try:
        r.set(k, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reco.cache] set %s failed: %s", k, exc)


def raw_client():
    """Expose the client for the few callers needing hashes/sorted-sets (session
    bias). Returns None when Redis is down — callers must handle it."""
    return _client()
