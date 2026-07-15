"""
Ranking context assembly (Part 2.3).

Merges the three time-horizons of preference into the single ``ctx`` dict every
ranker + explainer reads: EXPLICIT (this trip's intent) ⊕ LEARNED
(``user_travel_memory``) ⊕ SESSION (live clicks/skips) ⊕ COLD-START priors. Also
attaches the traveller-profile embedding for semantic personalisation (best-effort —
``None`` when the embedding layer isn't provisioned, which the ranker handles).
"""
from __future__ import annotations

import logging

from app.reco import personas, session, cold_start

logger = logging.getLogger(__name__)


async def build_ranking_context(
    user_id: str | None,
    intent: dict,
    *,
    session_id: str | None = None,
    destination: str | None = None,
    memory: dict | None = None,
    grounding: str | None = None,
) -> dict:
    """Assemble the ranking context. Never raises — every enrichment is optional.
    ``memory``/``grounding`` may be passed in to avoid a second DB read when the
    caller already fetched them (e.g. for intent extraction)."""
    if memory is None or grounding is None:
        memory = {}
        grounding = ""
        try:
            from app.services.user_memory import get_memory, memory_grounding
            memory = await get_memory(user_id) if user_id else {}
            grounding = memory_grounding(memory)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[reco.profile] memory unavailable: %s", exc)
    memory = memory or {}
    grounding = grounding or ""

    ctx: dict = {
        "user_id": user_id,
        "intent": intent,
        "persona": personas.derive_persona(intent),
        "budget": intent.get("budget_tier"),
        "price_sensitivity": intent.get("price_sensitivity"),
        "risk_tolerance": intent.get("risk_tolerance"),
        "vibes": list(intent.get("vibes") or []),
        "time_prefs": intent.get("time_prefs") or {},
        "traveller_memory": grounding,
        "hard": intent.get("hard") or {},
        "profile_embedding": None,
    }

    # Learned property-type / interest hints fold into vibes (union, capped).
    for key in ("hotel_property_types", "interests"):
        for v in (memory.get(key) or []):
            if str(v).lower() not in ctx["vibes"]:
                ctx["vibes"].append(str(v).lower())
    ctx["vibes"] = ctx["vibes"][:16]

    ctx = await cold_start.seed_context(ctx, memory, destination)
    if session_id:
        session.apply_bias(ctx, session_id)

    ctx["profile_embedding"] = await _profile_embedding(user_id, ctx, grounding)
    return ctx


async def _profile_embedding(user_id: str | None, ctx: dict, grounding: str):
    """Traveller-profile embedding for semantic match — stored vector first, else
    embed the profile doc on the fly, else None. All best-effort."""
    try:
        from app.reco import vector_store
        if user_id:
            stored = await vector_store.get_traveller_embedding(user_id)
            if stored:
                return stored
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reco.profile] stored profile emb miss: %s", exc)

    try:
        from app.services.embeddings import embed_one
        doc = _profile_doc(ctx, grounding)
        if doc:
            return await embed_one(doc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[reco.profile] live profile emb failed: %s", exc)
    return None


def _profile_doc(ctx: dict, grounding: str) -> str:
    intent = ctx.get("intent") or {}
    parts = []
    if intent.get("trip_purpose"):
        parts.append(f"{intent['trip_purpose']} trip")
    if ctx.get("vibes"):
        parts.append("wants: " + ", ".join(ctx["vibes"][:8]))
    if ctx.get("budget"):
        parts.append(f"{ctx['budget']} budget")
    if grounding:
        parts.append(grounding)
    return ". ".join(parts).strip()
