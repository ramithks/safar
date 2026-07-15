"""
Explainability (Part 15) — every item ships a human "why"; no internal score leaks.

- The HERO pick gets an LLM sentence via the existing ``recommendation_agent`` (it's
  already budget- and memory-aware). On any failure we fall back to the deterministic
  explanation, so there's always a "why".
- Every OTHER item gets a deterministic ``why`` + chips + honest ``tradeoff``
  (vs the cheapest peer) + ``best_for``/``not_for`` — no LLM, no latency.
- Only bucketed ``match`` ("high"/"good"/"fair") is exposed, never ``_reco_score``.
"""
from __future__ import annotations

import logging

from app.services.scoring import _f

logger = logging.getLogger(__name__)

# Internal fields that must NEVER reach the client (raw scores + the embedding vector).
_INTERNAL = ("_reco_score", "_reco_components", "_journey_score", "_embedding")

_TAG_BEST_FOR = {
    "romantic_couples": "couples", "beachfront": "beach lovers", "lake_view": "slow travellers",
    "party_hub": "night-outers", "backpacker_social": "solo backpackers",
    "family_comfort": "families", "workcation_ready": "remote workers",
    "luxury_palace": "a splurge", "heritage_royal": "heritage seekers",
    "foodie_haven": "food lovers", "wellness_spa": "wellness seekers",
    "peaceful_retreat": "anyone who wants quiet", "instagram_icon": "photographers",
    "city_central": "sightseers who want to walk everywhere",
}
_TAG_NOT_FOR = {
    "party_hub": "light sleepers", "backpacker_social": "anyone wanting privacy",
    "luxury_palace": "tight budgets", "peaceful_retreat": "nightlife seekers",
    "heritage_royal": "modern-minimalist tastes",
}


def _hotel_chips(h: dict, cheapest: float | None) -> list[str]:
    chips: list[str] = []
    price = _f(h.get("price_per_night")) or _f(h.get("estimated_price_per_night"))
    if price and cheapest:
        if price <= cheapest * 1.02:
            chips.append("Lowest price")
        elif price <= cheapest * 1.2:
            chips.append(f"₹{round(price - cheapest):,} over cheapest")
    dist = _f(h.get("distance_km"))
    if dist is not None and dist <= 1.5:
        chips.append(f"{dist:g} km from centre")
    rating = _f(h.get("review_score")) or _f(h.get("ta_rating"))
    if rating is not None and rating >= 8.5 and (_f(h.get("review_count")) or 0) >= 50:
        chips.append("Top rated")
    elif rating is not None and rating >= 8:
        chips.append("Highly rated")
    if h.get("free_cancellation"):
        chips.append("Free cancellation")
    if h.get("is_breakfast_included") or h.get("meal_plan"):
        chips.append("Breakfast included")
    save = _f(h.get("savings"))
    if save and save > 300:
        chips.append(f"Save ₹{round(save):,}")
    if isinstance(h.get("available_rooms"), int) and 0 < h["available_rooms"] <= 3:
        chips.append(f"Only {h['available_rooms']} left")
    return chips[:4]


def _flight_chips(t: dict, cheapest: float | None) -> list[str]:
    chips: list[str] = []
    price = _f(t.get("price_total")) or _f(t.get("price"))
    if price and cheapest:
        if price <= cheapest * 1.02:
            chips.append("Lowest fare")
        elif price <= cheapest * 1.2:
            chips.append(f"₹{round(price - cheapest):,} over cheapest")
    if int(t.get("stops") or 0) == 0:
        chips.append("Non-stop")
    bag = _f(t.get("baggage_kg"))
    if bag and bag >= 15:
        chips.append(f"{bag:g}kg baggage")
    if t.get("fare_type"):
        chips.append(str(t["fare_type"]))
    seats = _f(t.get("seats_available"))
    if seats is not None and 0 < seats <= 5:
        chips.append(f"Only {int(seats)} seats left")
    return chips[:4]


def best_not_for(item: dict) -> tuple[list[str], list[str]]:
    tags = [str(t).lower() for t in (item.get("personality_tags") or [])]
    best = [v for t in tags if (v := _TAG_BEST_FOR.get(t))]
    notf = [v for t in tags if (v := _TAG_NOT_FOR.get(t))]
    return best[:3], notf[:2]


def deterministic_explanation(item: dict, cheapest: float | None, kind: str) -> dict:
    chips = _hotel_chips(item, cheapest) if kind == "hotel" else _flight_chips(item, cheapest)
    best, notf = best_not_for(item)
    tradeoff = ""
    price = _f(item.get("price_per_night")) or _f(item.get("price_total")) or _f(item.get("price"))
    if price and cheapest and price > cheapest * 1.02:
        lead = chips[1] if len(chips) > 1 else "more, but stronger on location/quality"
        tradeoff = f"₹{round(price - cheapest):,} over the cheapest — {lead.lower()}."
    why = ", ".join(chips[:2]) + "." if chips else "A solid pick for this trip."
    return {
        "why": why[0].upper() + why[1:] if why else why,
        "why_tags": chips,
        "tradeoff": tradeoff,
        "best_for": best,
        "not_for": notf,
        "match": item.get("match", "good"),
    }


async def hero_explanation(items: list[dict], ctx: dict, kind: str) -> dict | None:
    """LLM pick + reason over the top-8. Returns the hero item enriched with a `why`,
    or None (caller then promotes the top-ranked item with a deterministic why)."""
    if not items:
        return None
    context = {
        "budget": ctx.get("budget"),
        "traveller_memory": ctx.get("traveller_memory"),
        "destination": ctx.get("destination"),
        "vibes": ctx.get("vibes"),
    }
    try:
        if kind == "hotel":
            from app.agents.recommendation_agent import recommend_hotels
            rec = await recommend_hotels(items[:8], context)
        else:
            from app.agents.recommendation_agent import recommend_flights
            rec = await recommend_flights(items[:8], context)
    except Exception as exc:  # noqa: BLE001 — advisory
        logger.warning("[reco.explain] hero LLM failed: %s", str(exc)[:140])
        rec = None
    if not rec:
        return None
    idx = rec.get("recommended_index", 0)
    if not (isinstance(idx, int) and 0 <= idx < len(items)):
        return None
    hero = items[idx]
    cheapest = _cheapest(items, kind)
    det = deterministic_explanation(hero, cheapest, kind)
    hero_out = {k: v for k, v in hero.items() if k not in _INTERNAL}
    hero_out.update({
        "why": rec.get("reason") or det["why"],
        "why_tags": det["why_tags"],
        "pros": rec.get("pros") or [],
        "best_for": [rec["best_for"]] if rec.get("best_for") else det["best_for"],
        "not_for": det["not_for"],
        "tradeoff": det["tradeoff"],
        "match": hero.get("match", "high"),
    })
    return hero_out


def _cheapest(items: list[dict], kind: str) -> float | None:
    key = ("price_per_night", "estimated_price_per_night") if kind == "hotel" else ("price_total", "price", "fare")
    prices = [p for p in (_first(i, key) for i in items) if p]
    return min(prices) if prices else None


def _first(item: dict, keys) -> float | None:
    for k in keys:
        v = _f(item.get(k))
        if v:
            return v
    return None


def annotate_list(items: list[dict], ctx: dict, kind: str) -> list[dict]:
    """Attach deterministic why/chips/match to every item (public shape). Strips the
    internal score fields so they never reach the client."""
    cheapest = _cheapest(items, kind)
    for it in items:
        exp = deterministic_explanation(it, cheapest, kind)
        it.update({k: exp[k] for k in ("why", "why_tags", "tradeoff", "best_for", "not_for", "match")})
        for internal in _INTERNAL:
            it.pop(internal, None)
    return items
