"""
Personality tagging (Parts 3 + 4) — cheap first, LLM last.

Tiers, in order of cost:
  1. Deterministic RULES from structured fields (below) — run always, free.
  2. Embedding → nearest personality CENTROID (Part 3.2) — assigns the fuzzy,
     review-driven tags with no per-hotel LLM call.
  3. Gemini — ONLY when the top centroid similarity is below ``TAU_LOW`` (ambiguous
     hotel). Lives in the worker (app.workers.reco_tasks); this module just flags it.

Controlled vocabularies keep tags filterable + comparable; free-form nuance lives in
the embedding.
"""
from __future__ import annotations

from app.services.scoring import _f

HOTEL_VOCAB = (
    "luxury_palace", "heritage_royal", "boutique_design", "instagram_icon",
    "hidden_gem", "budget_explorer", "backpacker_social", "family_comfort",
    "workcation_ready", "lake_view", "beachfront", "mountain_view", "city_central",
    "nature_retreat", "peaceful_retreat", "party_hub", "foodie_haven", "wellness_spa",
    "romantic_couples", "business_express", "pet_friendly", "accessible",
    "eco_conscious", "long_stay",
)
TRANSPORT_VOCAB = (
    "budget_hero", "best_value", "fast_escape", "nonstop_premium", "red_eye",
    "comfort_pick", "business_express", "family_friendly", "backpacker_choice",
    "scenic_route", "sleeper_champion", "weekend_winner", "early_bird",
    "late_departure", "generous_baggage", "flexible_fare",
)

TAU_LOW = 0.45  # below this top-centroid cosine → escalate to the LLM


def _landmark_kw(h: dict, *keywords: str) -> bool:
    # NB: dict.get(k, "") returns None when the key exists with a null value (common
    # for nullable DB columns like property_type) — coerce every piece to str.
    landmarks = h.get("landmarks") or []
    transport = h.get("nearest_transport")
    transport = [transport] if isinstance(transport, dict) else (transport or [])
    parts = [
        str(h.get("name") or ""), str(h.get("property_type") or ""),
        *[str((l or {}).get("name") or "") for l in landmarks if isinstance(l, dict)],
        *[str((t or {}).get("name") or "") for t in transport if isinstance(t, dict)],
    ]
    hay = " ".join(parts).lower()
    return any(k in hay for k in keywords)


def hotel_rule_tags(h: dict, *, price_hi: float | None = None, price_lo: float | None = None) -> set[str]:
    """Unambiguous tags from structured fields. Cheap enough for serve time."""
    t: set[str] = set()
    sub = h.get("subscores") or {}
    price = _f(h.get("price_per_night")) or _f(h.get("estimated_price_per_night"))
    star = _f(h.get("star_rating")) or 0
    ptype = (h.get("property_type") or "").lower()

    if star >= 5 and price_hi and price and price >= price_hi:
        t.add("luxury_palace")
    if _landmark_kw(h, "heritage", "palace", "haveli", "fort"):
        t.add("heritage_royal")
    if (_f(sub.get("location")) or 0) >= 8.5 and (_f(h.get("distance_km")) or 9) <= 1.5:
        t.add("city_central")
    if price_lo and price and price <= price_lo and (_f(h.get("review_score")) or 0) >= 7.5:
        t.add("budget_explorer")
    if "hostel" in ptype or _landmark_kw(h, "hostel", "backpacker"):
        t.add("backpacker_social")
    if h.get("family_friendly") or "apartment" in ptype or "villa" in ptype:
        t.add("family_comfort")
    if h.get("free_wifi") and h.get("has_workspace"):
        t.add("workcation_ready")
    if _landmark_kw(h, "lake"):
        t.add("lake_view")
    if _landmark_kw(h, "beach", "sea"):
        t.add("beachfront")
    if _landmark_kw(h, "mountain", "hill", "valley"):
        t.add("mountain_view")
    if h.get("has_spa") or (_f(sub.get("cleanliness")) or 0) >= 9.2:
        t.add("wellness_spa")
    if h.get("couple_friendly"):
        t.add("romantic_couples")
    if h.get("free_cancellation") and star >= 4 and (_f(sub.get("location")) or 0) >= 8:
        t.add("business_express")
    return {x for x in t if x in HOTEL_VOCAB}


def transport_tags(t: dict, stats) -> set[str]:
    """Transport personality tags — almost entirely deterministic (Part 4.2)."""
    out: set[str] = set()
    mode = t.get("mode") or ("flight" if t.get("airline") else t.get("type"))
    price = _f(t.get("price_total")) or _f(t.get("price")) or _f(t.get("fare"))
    dur = _f(t.get("duration_min"))
    stops = int(t.get("stops") or 0)
    baggage = _f(t.get("baggage_kg")) or 0
    fare = (t.get("fare_type") or "").lower()
    cls = (t.get("class") or t.get("cabin_class") or "").upper()

    lo_p = getattr(stats, "lo_price", 0.0)
    hi_p = getattr(stats, "hi_price", 0.0)
    lo_d = getattr(stats, "lo_dur", 0.0)
    if price is not None and hi_p > lo_p and price <= lo_p + (hi_p - lo_p) * 0.15:
        out.add("budget_hero")
    if (t.get("_reco_score") or 0) >= 78:
        out.add("best_value")
    if stops == 0 and mode == "flight":
        out.add("nonstop_premium")
    if dur and lo_d and dur <= lo_d * 1.1:
        out.add("fast_escape")
    dh = _dep_hour(t)
    if dh is not None and (dh >= 22 or dh < 5):
        out.add("red_eye")
    elif dh is not None and 5 <= dh < 8:
        out.add("early_bird")
    if dh is not None and dh >= 20:
        out.add("late_departure")
    if baggage >= 20:
        out.add("generous_baggage")
    if fare in ("flexi", "flex", "refundable") or t.get("is_refundable") is True:
        out.add("flexible_fare")
    if mode == "train" and cls in ("SL", "2S"):
        out.add("sleeper_champion")
    if mode == "bus" and "sleeper" in (t.get("class") or "").lower():
        out.add("comfort_pick")
    # Gotogate fare_tags map straight through.
    for ft in (t.get("fare_tags") or []):
        if str(ft).upper() == "PRICE":
            out.add("budget_hero")
        elif str(ft).upper() == "WEIGHTED":
            out.add("best_value")
    return {x for x in out if x in TRANSPORT_VOCAB}


def cluster_tags(embedding, centroids: dict[str, list[float]], top: int = 4) -> tuple[list[str], float]:
    """Assign fuzzy tags by cosine to precomputed personality centroids. Returns
    (tags, top_similarity). ([], 0.0) when no embedding/centroids → caller falls back
    to rules and may escalate to the LLM if confidence is low."""
    if not embedding or not centroids:
        return [], 0.0
    from app.reco.features import cosine01
    sims = []
    for tag, c in centroids.items():
        s = cosine01(embedding, c)
        if s is not None:
            sims.append((tag, s))
    if not sims:
        return [], 0.0
    sims.sort(key=lambda x: x[1], reverse=True)
    top_sim = sims[0][1]
    chosen = [tag for tag, s in sims[:top] if s >= TAU_LOW]
    return chosen, top_sim


def needs_llm(rule_tags_out: set[str], cluster_sim: float) -> bool:
    """Escalate to Gemini only for the ambiguous minority: weak cluster AND sparse
    rules. This is what keeps tagging ~90% cheaper than an LLM-per-hotel approach."""
    return cluster_sim < TAU_LOW and len(rule_tags_out) < 2


def _dep_hour(t: dict) -> int | None:
    from app.reco.features import _hour
    return _hour(t.get("departure_time"))
