"""
Personas + contextual ranking weights.

A *persona* is the coarse traveller archetype we rank for (backpacker, honeymoon,
business, …). It's derived from the extracted intent (trip purpose × traveller type
× budget) and selects a base weight vector. The intent's per-axis ``importance``
then nudges that vector so two honeymooners who care about different things don't get
identical rankings.

Weights are plain module dicts (like ``app.services.scoring``) so they're tunable
without code changes and, later, hot-loadable from the ``ranking_weights`` table.
Every weight vector is renormalised at use time, so the numbers below only need to
express *relative* emphasis.
"""
from __future__ import annotations

# ── Persona set ────────────────────────────────────────────────────────────────
BACKPACKER = "backpacker"
BUDGET_FAMILY = "budget_family"
WEEKEND_FRIENDS = "weekend_friends"
HONEYMOON = "honeymoon"
LUXURY = "luxury"
BUSINESS = "business"
WORKCATION = "workcation"
DEFAULT = "default"

PERSONAS = (
    BACKPACKER, BUDGET_FAMILY, WEEKEND_FRIENDS, HONEYMOON,
    LUXURY, BUSINESS, WORKCATION, DEFAULT,
)


def derive_persona(intent: dict) -> str:
    """Map an extracted intent → one persona. Deterministic, no LLM."""
    purpose = (intent.get("trip_purpose") or "").lower()
    ttype = (intent.get("traveller_type") or "").lower()
    budget = (intent.get("budget_tier") or "").lower()

    if purpose in ("honeymoon",) or (purpose == "leisure" and ttype == "couple" and budget == "high"):
        return HONEYMOON
    if purpose == "business":
        return BUSINESS
    if purpose == "workcation":
        return WORKCATION
    if purpose in ("backpacking",) or (budget == "low" and ttype == "solo"):
        return BACKPACKER
    if ttype == "family":
        return BUDGET_FAMILY
    if ttype == "friends":
        return WEEKEND_FRIENDS
    if budget == "high":
        return LUXURY
    return DEFAULT


# ── Base weights ───────────────────────────────────────────────────────────────
# Hotel feature keys (must match app.reco.features.hotel_components output):
#   price rating location personality flexibility comfort deal semantic
HOTEL_PERSONA_WEIGHTS: dict[str, dict[str, float]] = {
    BACKPACKER:      {"price": .34, "rating": .12, "location": .18, "personality": .12, "flexibility": .06, "comfort": .03, "deal": .07, "semantic": .08},
    BUDGET_FAMILY:   {"price": .28, "rating": .18, "location": .14, "personality": .09, "flexibility": .10, "comfort": .09, "deal": .06, "semantic": .06},
    WEEKEND_FRIENDS: {"price": .22, "rating": .16, "location": .21, "personality": .13, "flexibility": .06, "comfort": .06, "deal": .06, "semantic": .10},
    HONEYMOON:       {"price": .10, "rating": .20, "location": .16, "personality": .18, "flexibility": .06, "comfort": .14, "deal": .02, "semantic": .14},
    LUXURY:          {"price": .06, "rating": .22, "location": .16, "personality": .16, "flexibility": .05, "comfort": .18, "deal": .02, "semantic": .15},
    BUSINESS:        {"price": .14, "rating": .18, "location": .26, "personality": .05, "flexibility": .13, "comfort": .13, "deal": .03, "semantic": .08},
    WORKCATION:      {"price": .18, "rating": .16, "location": .16, "personality": .15, "flexibility": .10, "comfort": .12, "deal": .04, "semantic": .09},
    DEFAULT:         {"price": .24, "rating": .18, "location": .18, "personality": .10, "flexibility": .08, "comfort": .08, "deal": .05, "semantic": .09},
}

# Transport feature keys (must match app.reco.features.transport_components):
#   price duration stops arrival departure baggage flexibility comfort personality
TRANSPORT_PERSONA_WEIGHTS: dict[str, dict[str, float]] = {
    BACKPACKER:      {"price": .40, "duration": .16, "stops": .08, "arrival": .06, "departure": .04, "baggage": .06, "flexibility": .05, "comfort": .07, "personality": .08},
    BUDGET_FAMILY:   {"price": .30, "duration": .16, "stops": .12, "arrival": .08, "departure": .05, "baggage": .08, "flexibility": .07, "comfort": .08, "personality": .06},
    WEEKEND_FRIENDS: {"price": .28, "duration": .20, "stops": .10, "arrival": .08, "departure": .06, "baggage": .05, "flexibility": .05, "comfort": .06, "personality": .12},
    HONEYMOON:       {"price": .14, "duration": .18, "stops": .12, "arrival": .12, "departure": .06, "baggage": .06, "flexibility": .08, "comfort": .16, "personality": .08},
    LUXURY:          {"price": .08, "duration": .20, "stops": .14, "arrival": .12, "departure": .06, "baggage": .06, "flexibility": .08, "comfort": .18, "personality": .08},
    BUSINESS:        {"price": .12, "duration": .28, "stops": .14, "arrival": .14, "departure": .06, "baggage": .04, "flexibility": .10, "comfort": .08, "personality": .04},
    WORKCATION:      {"price": .20, "duration": .20, "stops": .12, "arrival": .10, "departure": .06, "baggage": .06, "flexibility": .08, "comfort": .10, "personality": .08},
    DEFAULT:         {"price": .28, "duration": .22, "stops": .12, "arrival": .10, "departure": .06, "baggage": .05, "flexibility": .06, "comfort": .06, "personality": .05},
}

# Which intent.importance axis nudges which weight key (0..1 axis; 0.5 = neutral).
_HOTEL_AXIS_MAP = {
    "location": "location", "convenience": "location", "cleanliness": "comfort",
    "cancellation": "flexibility", "quiet": "comfort", "photogenic": "personality",
    "food": "personality",
}
_TRANSPORT_AXIS_MAP = {
    "convenience": "duration", "cancellation": "flexibility", "cleanliness": "comfort",
}

_NUDGE = 0.6  # how hard importance can push a base weight (±)


def _nudge(base: dict[str, float], importance: dict, axis_map: dict[str, str]) -> dict[str, float]:
    """Scale base weights by intent importance, then renormalise to sum 1."""
    out = dict(base)
    for axis, key in axis_map.items():
        imp = importance.get(axis)
        if imp is None or key not in out:
            continue
        try:
            out[key] = max(0.0, out[key] * (1.0 + _NUDGE * (float(imp) - 0.5)))
        except (TypeError, ValueError):
            continue
    # Price sensitivity is a first-class knob on the price weight.
    z = sum(out.values()) or 1.0
    return {k: v / z for k, v in out.items()}


def resolve_hotel_weights(persona: str, intent: dict | None = None) -> dict[str, float]:
    base = HOTEL_PERSONA_WEIGHTS.get(persona, HOTEL_PERSONA_WEIGHTS[DEFAULT])
    imp = (intent or {}).get("importance") or {}
    weights = _nudge(base, imp, _HOTEL_AXIS_MAP)
    ps = (intent or {}).get("price_sensitivity")
    if ps is not None:
        weights = _apply_price_sensitivity(weights, ps)
    return weights


def resolve_transport_weights(persona: str, intent: dict | None = None) -> dict[str, float]:
    base = TRANSPORT_PERSONA_WEIGHTS.get(persona, TRANSPORT_PERSONA_WEIGHTS[DEFAULT])
    imp = (intent or {}).get("importance") or {}
    weights = _nudge(base, imp, _TRANSPORT_AXIS_MAP)
    ps = (intent or {}).get("price_sensitivity")
    if ps is not None:
        weights = _apply_price_sensitivity(weights, ps)
    return weights


def _apply_price_sensitivity(weights: dict[str, float], ps) -> dict[str, float]:
    """Let explicit price sensitivity (0..1) scale the price weight, renormalise."""
    try:
        ps = float(ps)
    except (TypeError, ValueError):
        return weights
    out = dict(weights)
    if "price" in out:
        out["price"] = max(0.0, out["price"] * (1.0 + _NUDGE * (ps - 0.5)))
    z = sum(out.values()) or 1.0
    return {k: v / z for k, v in out.items()}
