"""
Journey scoring — rank transport by END-TO-END value, not the leg in isolation.

A ₹1,200-cheaper flight that lands at 02:10 is not cheaper once it costs you a hotel
night and a painful transfer. This module folds arrival quality, travel fatigue, and
the hidden-hotel-night into a single journey score used to rank transport options for
a trip, and defers the human trade-off narrative to
``app.services.journey_reasoning`` (which already renders the cheapest/fastest/
balanced comparison).
"""
from __future__ import annotations

from app.services.scoring import _f
from app.reco.features import _hour, _arrival_quality
from app.reco.rank import rank_transport


def _fatigue(t: dict) -> float:
    """0..1 — how draining this option is. Legs, layovers, red-eye, hours in transit."""
    dur_h = (_f(t.get("duration_min")) or 0) / 60.0
    legs = int(t.get("stops") or 0)
    layover_h = (_f(t.get("layover_minutes")) or 0) / 60.0
    arr = _hour(t.get("arrival_time"))
    redeye = 1.0 if (arr is not None and (arr >= 22 or arr < 6)) else 0.0
    raw = 0.05 * dur_h + 0.12 * legs + 0.10 * layover_h + 0.25 * redeye
    return max(0.0, min(1.0, raw))


def journey_score(t: dict, ctx: dict | None = None, hotel_per_night: int | None = None) -> float:
    """0..100 journey-adjusted value for a single transport option. Assumes
    ``t['_reco_score']`` was set by ``rank_transport`` (falls back to 60 if not)."""
    base = _f(t.get("_reco_score"))
    if base is None:
        base = 60.0
    arr = _hour(t.get("arrival_time"))
    arrq = _arrival_quality(arr)
    fatigue = _fatigue(t)

    score = 0.65 * base + 0.20 * ((arrq if arrq is not None else 0.6) * 100) + 0.15 * ((1 - fatigue) * 100)

    # Hidden hotel-night: a late arrival that can't make same-day check-in burns a
    # night. Convert to a points penalty scaled by the actual nightly rate.
    if arr is not None and (arr >= 22 or arr < 6):
        penalty = 12.0
        if hotel_per_night:
            penalty = min(22.0, 8.0 + hotel_per_night / 400.0)
        score -= penalty
    return round(max(0.0, min(100.0, score)), 1)


def rank_journeys(
    transports: list[dict], ctx: dict | None = None, hotel_per_night: int | None = None
) -> list[dict]:
    """Rank transport options by journey-adjusted value. Runs the base transport
    ranker first (so _reco_score exists), then re-orders by journey_score."""
    if not transports:
        return transports
    rank_transport(transports, ctx)  # sets _reco_score + components
    for t in transports:
        t["_journey_score"] = journey_score(t, ctx, hotel_per_night)
        t["match"] = _bucket(t["_journey_score"])
    transports.sort(key=lambda x: x.get("_journey_score", 0.0), reverse=True)
    return transports


def journey_note(t: dict, hotel_per_night: int | None = None) -> str:
    """One honest per-option line — feeds the deterministic explanation layer."""
    arr = _hour(t.get("arrival_time"))
    fatigue = _fatigue(t)
    bits = []
    if arr is not None:
        suffix = "AM" if arr < 12 else "PM"
        bits.append(f"arrives ~{arr % 12 or 12} {suffix}")
        if arr >= 22 or arr < 6:
            extra = f" (likely an extra ₹{hotel_per_night:,} night)" if hotel_per_night else ""
            bits.append(f"a late landing{extra}")
    if int(t.get("stops") or 0) == 0:
        bits.append("non-stop")
    elif fatigue >= 0.5:
        bits.append("a tiring routing")
    return ", ".join(bits) if bits else ""


def _bucket(score: float) -> str:
    if score >= 78:
        return "high"
    if score >= 60:
        return "good"
    return "fair"
