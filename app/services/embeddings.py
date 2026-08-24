"""
Vertex text-embedding client (384-dim) for the recommendation engine.

Reuses the auth / region / HTTP machinery already proven in ``app.services.vertex``
so there's one token cache and one connection pool. Everything here is BEST-EFFORT:
if Vertex isn't configured or a call fails, ``embed_texts`` returns ``None`` and the
caller treats the semantic signal as absent (renormalised out) — no crash, no
blocking. Never call these on the request hot path; embeddings are precomputed by the
Celery worker and only refreshed when a doc's hash changes.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

_BATCH = 50  # Vertex predict accepts many instances per call


def _normalise(vec: list[float]) -> list[float]:
    n = sum(x * x for x in vec) ** 0.5
    return [x / n for x in vec] if n else vec


async def embed_texts(texts: list[str], *, task_type: str = "SEMANTIC_SIMILARITY") -> list[list[float]] | None:
    """Embed a list of texts → list of unit-length 384-dim vectors (same order), or
    ``None`` on any failure / misconfiguration. Batched."""
    texts = [(t or "").strip()[:8000] for t in texts]
    if not any(texts):
        return None
    try:
        from app.services.vertex import (
            _get_access_token, _get_http_client, _regions, get_vertex_host,
        )
        token, project_id = _get_access_token()
    except Exception as exc:  # noqa: BLE001 — no creds → embeddings simply unavailable
        logger.debug("[embeddings] auth unavailable: %s", exc)
        return None

    regions = _regions()
    model = settings.vertex_model_embedding
    client = _get_http_client()
    out: list[list[float]] = []

    for i in range(0, len(texts), _BATCH):
        chunk = texts[i:i + _BATCH]
        body = {
            "instances": [{"content": t, "task_type": task_type} for t in chunk],
            "parameters": {"outputDimensionality": settings.embedding_dim},
        }
        vecs = await _predict(client, token, project_id, regions, model, body,
                              host_for=get_vertex_host)
        if vecs is None or len(vecs) != len(chunk):
            return None
        out.extend(_normalise(v) for v in vecs)
    return out


async def embed_one(text: str, *, task_type: str = "SEMANTIC_SIMILARITY") -> list[float] | None:
    res = await embed_texts([text], task_type=task_type)
    return res[0] if res else None


async def _predict(client, token, project_id, regions, model, body,
                   *, host_for) -> list[list[float]] | None:
    """
    `host_for` is `vertex.get_vertex_host`, and passing it is not ceremony.

    This module builds its own URL because `:predict` is a different endpoint
    from `:generateContent`, and it took the region prefix as given — which was
    true for as long as the configured region list held only real regions. It
    stopped being true the moment `global` was added to reach Gemini 3.x:
    `global-aiplatform.googleapis.com` does not resolve, so the first region
    tried would have failed DNS on every call.

    The failure would have been quiet. Every exception here is swallowed and the
    loop moves to the next region, so embeddings would simply have got slower
    and no louder. One helper, one asymmetry, everywhere that builds a Vertex
    URL.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    last_exc: Exception | None = None
    for region in regions:
        url = (
            f"https://{host_for(region)}/v1/projects/{project_id}"
            f"/locations/{region}/publishers/google/models/{model}:predict"
        )
        try:
            r = await client.post(url, headers=headers, json=body, timeout=30.0)
            if r.status_code in (429, 503):  # region saturated → try next
                last_exc = RuntimeError(f"{r.status_code} at {region}")
                continue
            r.raise_for_status()
            preds = r.json().get("predictions") or []
            return [(p.get("embeddings") or {}).get("values") or [] for p in preds]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    logger.debug("[embeddings] all regions failed: %s", last_exc)
    return None
