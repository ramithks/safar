"""
Recommendation API — the traveller-first ranking surface (Part 11).

Funnel (the decided order): hard-filter → feature-score (all) → top-50 →
semantic re-rank → top-15 → LLM hero → top-1. Deterministic ranking ships the list;
the LLM only ever explains the shortlist's hero. Every stage degrades gracefully, so
a Vertex / pgvector / Redis outage never fails a request — it just drops a signal.

Responses expose a `hero`, a ranked `results` list (each with `why`/`match`), a couple
of labelled `alternatives`, and the resolved `intent` — never a raw score.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends

from app.deps.auth import require_user
from app.reco import intent as intent_mod, profile as profile_mod
from app.reco import rank, explain, feedback, journey
from .models import (
    HotelRecommendRequest, TransportRecommendRequest, JourneyRecommendRequest,
    ExplainRequest, FeedbackRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/recommend", tags=["recommend"])


# ── Shared context ───────────────────────────────────────────────────────────────
async def _context(user, *, text, intake, budget, destination, session_id):
    """Extract intent + assemble ranking context, fetching memory once. Never raises."""
    uid = str(user.id) if user else None
    intake = dict(intake or {})
    if budget:
        intake.setdefault("budget", budget)
    memory, grounding = {}, ""
    try:
        from app.services.user_memory import get_memory, memory_grounding
        memory = await get_memory(uid) if uid else {}
        grounding = memory_grounding(memory)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[recommend] memory unavailable: %s", exc)
    intent = await intent_mod.extract_intent(text, intake, uid, grounding)
    ctx = await profile_mod.build_ranking_context(
        uid, intent, session_id=session_id, destination=destination,
        memory=memory, grounding=grounding,
    )
    ctx["destination"] = destination
    return intent, ctx


def _alternatives(items: list[dict], kind: str) -> list[dict]:
    """A couple of labelled alternatives so the user sees the frontier, not one answer."""
    if len(items) < 2:
        return []
    price_key = ("price_per_night", "estimated_price_per_night") if kind == "hotel" else ("price_total", "price")

    def price(x):
        for k in price_key:
            v = x.get(k)
            if v:
                return v
        return 1e12
    out = []
    cheapest = min(items[1:], key=price, default=None)
    if cheapest:
        out.append({"label": "Cheapest option", "item_id": _iid(cheapest), "why": cheapest.get("why", "")})
    splurge = max(items[1:], key=lambda x: (x.get("match") == "high", price(x) if price(x) < 1e12 else 0), default=None)
    if splurge and splurge is not cheapest:
        out.append({"label": "The upgrade", "item_id": _iid(splurge), "why": splurge.get("why", "")})
    return out


def _iid(x: dict):
    return x.get("hotel_id") or x.get("id") or x.get("booking_token") or x.get("name")


# ── Hotels ───────────────────────────────────────────────────────────────────────
@router.post("/hotels")
async def recommend_hotels(req: HotelRecommendRequest, user=Depends(require_user)):
    intent, ctx = await _context(
        user, text=req.text, intake=req.intake, budget=req.budget,
        destination=req.destination, session_id=req.session_id,
    )
    cands = req.candidates or await _fetch_hotels(req, ctx)
    if not cands:
        return {"hero": None, "results": [], "alternatives": [], "relaxations": [],
                "intent": _intent_echo(intent), "note": "No stays found for these dates."}

    # Funnel: score everything → top-15 shortlist → LLM hero. (Embeddings are not
    # used on the live path — hotels are fetched fresh each time; ranking runs on
    # rules + price + rating + personality tags.)
    rank.rank_hotels(cands, ctx)
    shortlist = cands[:15]

    hero = await explain.hero_explanation(shortlist, ctx, "hotel") if req.explain else None
    display = rank.mmr_diversify(shortlist, min(req.limit, len(shortlist)))
    explain.annotate_list(display, ctx, "hotel")

    _fire(feedback.log_impressions(display, user_id=str(user.id), item_type="hotel",
                                   session_id=req.session_id, context={"persona": ctx.get("persona")}))
    return {
        "hero": hero or (display[0] if display else None),
        "results": display,
        "alternatives": _alternatives(display, "hotel"),
        "relaxations": ctx.get("_relaxations", []),
        "intent": _intent_echo(intent),
    }


async def _fetch_hotels(req: HotelRecommendRequest, ctx: dict) -> list[dict]:
    try:
        from app.services.hotels import HotelService
        from datetime import date, timedelta
        checkin = req.checkin or (date.today() + timedelta(days=21)).isoformat()
        checkout = req.checkout or (date.today() + timedelta(days=23)).isoformat()
        res = await HotelService().search(
            req.destination, checkin, checkout, adults=req.pax,
            budget=(ctx.get("budget") or "mid"),
        )
        return res.get("hotels") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[recommend] hotel fetch failed: %s", str(exc)[:140])
        return []


# ── Transport ────────────────────────────────────────────────────────────────────
@router.post("/transport")
async def recommend_transport(req: TransportRecommendRequest, user=Depends(require_user)):
    intent, ctx = await _context(
        user, text=req.text, intake=req.intake, budget=req.budget,
        destination=req.destination, session_id=req.session_id,
    )
    cands = req.candidates or await _fetch_transport(req, user)
    if not cands:
        return {"hero": None, "results": [], "by_mode": {}, "alternatives": [],
                "intent": _intent_echo(intent), "note": "No transport found for this route/date."}

    journey.rank_journeys(cands, ctx, hotel_per_night=req.hotel_per_night)
    shortlist = cands[:15]
    hero = await explain.hero_explanation(shortlist, ctx, "flight") if req.explain else None
    display = shortlist[:req.limit]
    explain.annotate_list(display, ctx, "flight")

    by_mode = {}
    for m in ("flight", "train", "bus"):
        best = next((x for x in display if (x.get("mode") == m or (m == "flight" and x.get("airline")))), None)
        if best:
            by_mode[m] = _iid(best)

    _fire(feedback.log_impressions(display, user_id=str(user.id), item_type="transport",
                                   session_id=req.session_id, context={"persona": ctx.get("persona")}))
    return {
        "hero": hero or (display[0] if display else None),
        "results": display, "by_mode": by_mode,
        "alternatives": _alternatives(display, "flight"),
        "intent": _intent_echo(intent),
    }


async def _fetch_transport(req: TransportRecommendRequest, user) -> list[dict]:
    try:
        from app.services.transport import TransportService
        from datetime import date, timedelta
        d = req.date or (date.today() + timedelta(days=21)).isoformat()
        res = await TransportService().search_all(req.origin, req.destination, d, travelers=req.pax, modes=req.modes)
        flights = (res.get("flights") or {}).get("flights") or []
        trains = (res.get("trains") or {}).get("trains") or []
        buses = (res.get("buses") or {}).get("buses") or []
        for f in flights:
            f.setdefault("mode", "flight")
        for t in trains:
            t.setdefault("mode", "train")
        for b in buses:
            b.setdefault("mode", "bus")
        # (search_all already persists to transport_options — no double write here.)
        return flights + trains + buses
    except Exception as exc:  # noqa: BLE001
        logger.warning("[recommend] transport fetch failed: %s", str(exc)[:140])
        return []


# ── Journey (transport + stay as one) ────────────────────────────────────────────
@router.post("/journey")
async def recommend_journey(req: JourneyRecommendRequest, user=Depends(require_user)):
    intent, ctx = await _context(
        user, text=req.text, intake=req.intake, budget=None,
        destination=req.destination, session_id=req.session_id,
    )
    stays = req.stays or []
    if stays:
        rank.rank_hotels(stays, ctx)
    stay_hero = await explain.hero_explanation(stays[:8], ctx, "hotel") if (stays and req.explain) else (stays[0] if stays else None)
    hpn = None
    if stay_hero:
        hpn = stay_hero.get("price_per_night") or stay_hero.get("estimated_price_per_night")

    transports = req.transports or await _fetch_transport_for_journey(req, user)
    journey.rank_journeys(transports, ctx, hotel_per_night=hpn)
    trans_hero = await explain.hero_explanation(transports[:8], ctx, "flight") if (transports and req.explain) else (transports[0] if transports else None)

    reasoning = _journey_reasoning(transports, hpn)
    if stays:
        explain.annotate_list(stays[:10], ctx, "hotel")
    explain.annotate_list(transports[:10], ctx, "flight")

    return {
        "transport_hero": trans_hero,
        "stay_hero": stay_hero,
        "transport_options": transports[:10],
        "stay_options": stays[:10],
        "reasoning": reasoning,
        "intent": _intent_echo(intent),
    }


async def _fetch_transport_for_journey(req, user):
    t = TransportRecommendRequest(origin=req.origin, destination=req.destination, date=req.date, pax=req.pax)
    return await _fetch_transport(t, user)


def _journey_reasoning(transports: list[dict], hpn):
    if not transports:
        return None
    try:
        from app.services.journey_reasoning import build_journey_reasoning
        cheapest = min(transports, key=lambda x: x.get("price_total") or x.get("price") or 1e12)
        fastest = min(transports, key=lambda x: x.get("duration_min") or 1e12)
        opts = {
            "cheapest": {"mode": cheapest.get("mode"), "cost_per_person": cheapest.get("price_total") or cheapest.get("price"),
                         "duration_min": cheapest.get("duration_min"), "arrival_time": cheapest.get("arrival_time"),
                         "description": cheapest.get("airline") or cheapest.get("operator")},
            "fastest": {"mode": fastest.get("mode"), "cost_per_person": fastest.get("price_total") or fastest.get("price"),
                        "duration_min": fastest.get("duration_min"), "arrival_time": fastest.get("arrival_time"),
                        "description": fastest.get("airline") or fastest.get("operator")},
            "balanced": {"mode": transports[0].get("mode"), "cost_per_person": transports[0].get("price_total") or transports[0].get("price"),
                         "duration_min": transports[0].get("duration_min"), "arrival_time": transports[0].get("arrival_time"),
                         "description": transports[0].get("airline") or transports[0].get("operator")},
        }
        return build_journey_reasoning(opts, hotel_per_night=hpn)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[recommend] journey reasoning failed: %s", exc)
        return None


# ── Explain (copilot "why did you pick X") ───────────────────────────────────────
@router.post("/explain")
async def recommend_explain(req: ExplainRequest, user=Depends(require_user)):
    _, ctx = await _context(user, text=req.text, intake=req.intake, budget=None,
                            destination=(req.intake or {}).get("destination"), session_id=None)
    from app.reco.explain import deterministic_explanation, _cheapest
    peers = req.peers or [req.item]
    cheapest = _cheapest(peers, req.kind)
    return deterministic_explanation(req.item, cheapest, req.kind)


# ── Feedback ─────────────────────────────────────────────────────────────────────
@router.post("/feedback", status_code=204)
async def recommend_feedback(req: FeedbackRequest, user=Depends(require_user)):
    _fire(feedback.log_event(
        user_id=str(user.id), item_type=req.item_type, item_id=req.item_id, event=req.event,
        trip_id=req.trip_id, session_id=req.session_id, position=req.position,
        dwell_ms=req.dwell_ms, item=req.item,
    ))
    return None


# ── helpers ──────────────────────────────────────────────────────────────────────
def _intent_echo(intent: dict) -> dict:
    return {k: intent.get(k) for k in (
        "trip_purpose", "traveller_type", "budget_tier", "vibes",
        "price_sensitivity", "ambiguities",
    )}


def _fire(coro):
    """Fire-and-forget a best-effort coroutine (logging/persistence). Never awaited."""
    try:
        asyncio.create_task(coro)
    except RuntimeError:  # no running loop (shouldn't happen under FastAPI)
        pass
