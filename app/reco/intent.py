"""
Traveller Understanding Engine — free text → structured ``TripIntent`` (Part 2).

One cached ``muscle`` LLM pass turns the conversation + structured intake into typed
intent (purpose, persona signals, budget, per-axis importance, vibes, hard
constraints). Reliability rules:

  * EXPLICIT intake always wins — budget/dates/pax the user set are copied verbatim
    over anything the model inferred.
  * The LLM is never on the critical path for correctness: on any failure (or no
    Vertex), ``deterministic_intent`` builds a fully usable intent from the intake
    alone, so ranking always has an objective.
  * Cached on a hash of (conversation + intake) — repeat queries cost nothing.
"""
from __future__ import annotations

import logging

from app.reco import cache

logger = logging.getLogger(__name__)

_AXES = ("location", "cleanliness", "convenience", "cancellation", "food",
         "nightlife", "quiet", "photogenic", "accessibility")

_PURPOSES = ["leisure", "business", "honeymoon", "family", "workcation",
             "backpacking", "pilgrimage", "event", "relocation"]
_TYPES = ["solo", "couple", "family", "friends", "business"]

_INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "trip_purpose": {"type": "STRING", "nullable": True, "enum": _PURPOSES},
        "traveller_type": {"type": "STRING", "nullable": True, "enum": _TYPES},
        "group_size": {"type": "NUMBER", "nullable": True},
        "has_children": {"type": "BOOLEAN", "nullable": True},
        "budget_tier": {"type": "STRING", "nullable": True, "enum": ["low", "mid", "high"]},
        "price_sensitivity": {"type": "NUMBER", "nullable": True},
        "value_sensitivity": {"type": "NUMBER", "nullable": True},
        "risk_tolerance": {"type": "NUMBER", "nullable": True},
        "vibes": {"type": "ARRAY", "nullable": True, "items": {"type": "STRING"}},
        "importance": {
            "type": "OBJECT", "nullable": True,
            "properties": {a: {"type": "NUMBER", "nullable": True} for a in _AXES},
        },
        "time_prefs": {
            "type": "OBJECT", "nullable": True,
            "properties": {
                "depart_daypart": {"type": "STRING", "nullable": True},
                "avoid_redeye": {"type": "BOOLEAN", "nullable": True},
            },
        },
        "preferred_airlines": {"type": "ARRAY", "nullable": True, "items": {"type": "STRING"}},
        "preferred_hotel_brands": {"type": "ARRAY", "nullable": True, "items": {"type": "STRING"}},
        "ambiguities": {"type": "ARRAY", "nullable": True, "items": {"type": "STRING"}},
    },
}

_PROMPT = (
    "You convert a traveller's words into a structured trip intent. Infer only what "
    "the text supports; never contradict the EXPLICIT fields (they are authoritative "
    "— copy them). For 'importance' give 0..1 per axis (how much each matters to THIS "
    "traveller). price_sensitivity/value_sensitivity/risk_tolerance are 0..1. List "
    "anything genuinely ambiguous in 'ambiguities'. Do not invent constraints.\n\n"
    "EXPLICIT (authoritative): {intake}\n"
    "LEARNED ABOUT TRAVELLER (prior, may be overridden): {memory}\n"
    "CONVERSATION:\n{text}"
)


def _clip01(v, default=None):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _default_importance() -> dict[str, float]:
    return {a: 0.5 for a in _AXES}


def deterministic_intent(intake: dict | None) -> dict:
    """A fully usable intent from the structured intake alone — the fallback path
    that guarantees ranking always has an objective, LLM or not."""
    intake = intake or {}
    budget = (intake.get("budget_tier") or intake.get("budget") or "mid").lower()
    if budget not in ("low", "mid", "high"):
        budget = "mid"
    travelers = intake.get("travelers") or intake.get("group_size")
    who = (intake.get("who") or "").lower()
    ttype = None
    if "solo" in who or travelers == 1:
        ttype = "solo"
    elif "couple" in who or "partner" in who or travelers == 2:
        ttype = "couple"
    elif "family" in who or "kid" in who:
        ttype = "family"
    elif "friend" in who or (isinstance(travelers, int) and travelers >= 3):
        ttype = "friends"
    vibes = intake.get("vibes") or ([intake["vibe"]] if intake.get("vibe") else [])
    ps = {"low": 0.75, "mid": 0.5, "high": 0.25}[budget]
    return _normalise({
        "trip_purpose": intake.get("trip_purpose"),
        "traveller_type": ttype,
        "group_size": travelers,
        "budget_tier": budget,
        "price_sensitivity": ps,
        "value_sensitivity": 0.5,
        "risk_tolerance": 0.4,
        "vibes": vibes,
        "importance": _default_importance(),
    }, intake)


def _normalise(raw: dict, intake: dict | None) -> dict:
    """Clamp, default, and enforce explicit-intake precedence."""
    raw = raw or {}
    intake = intake or {}
    imp = {a: _clip01((raw.get("importance") or {}).get(a), 0.5) for a in _AXES}

    budget = (intake.get("budget_tier") or intake.get("budget")
              or raw.get("budget_tier") or "mid").lower()
    if budget not in ("low", "mid", "high"):
        budget = "mid"

    vibes = list(dict.fromkeys(
        [str(v).lower() for v in (raw.get("vibes") or []) if v]
        + [str(v).lower() for v in (intake.get("vibes") or []) if v]
        + ([str(intake["vibe"]).lower()] if intake.get("vibe") else [])
    ))[:12]

    out = {
        "trip_purpose": raw.get("trip_purpose") or intake.get("trip_purpose"),
        "traveller_type": raw.get("traveller_type"),
        "group_size": _to_int(intake.get("travelers") or raw.get("group_size")),
        "has_children": bool(raw.get("has_children")),
        "budget_tier": budget,
        "price_sensitivity": _clip01(raw.get("price_sensitivity"), {"low": .75, "mid": .5, "high": .25}[budget]),
        "value_sensitivity": _clip01(raw.get("value_sensitivity"), 0.5),
        "risk_tolerance": _clip01(raw.get("risk_tolerance"), 0.4),
        "vibes": vibes,
        "importance": imp,
        "time_prefs": raw.get("time_prefs") or {},
        "preferred_airlines": [str(a) for a in (raw.get("preferred_airlines") or [])][:6],
        "preferred_hotel_brands": [str(b) for b in (raw.get("preferred_hotel_brands") or [])][:6],
        "ambiguities": [str(a) for a in (raw.get("ambiguities") or [])][:6],
        "hard": _hard_constraints(intake),
    }
    return out


def _hard_constraints(intake: dict) -> dict:
    return {
        "max_total_budget": _to_int(intake.get("max_total_budget") or intake.get("budget_amount")),
        "must_refundable": bool(intake.get("must_refundable")),
        "date_from": intake.get("departure_date") or intake.get("date_from"),
        "date_to": intake.get("return_date") or intake.get("date_to"),
        "pax": _to_int(intake.get("travelers")) or 1,
    }


def _to_int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def extract_intent(
    text: str | None, intake: dict | None = None, user_id: str | None = None,
    memory_grounding: str = "",
) -> dict:
    """Return a normalised TripIntent. Cached; LLM-extracted with a deterministic
    fallback. Never raises."""
    intake = intake or {}
    text = (text or "").strip()

    # No conversation to reason over → intake is the whole story, skip the LLM.
    if not text:
        return deterministic_intent(intake)

    ckey = cache.key("intent", cache.digest({"t": text, "i": intake, "m": memory_grounding}))
    cached = cache.get_json(ckey)
    if cached:
        return cached

    try:
        from app.services.vertex import generate_json
        raw = await generate_json(
            _PROMPT.format(intake=intake, memory=memory_grounding or "none", text=text[:4000]),
            tier="muscle", response_schema=_INTENT_SCHEMA,
            temperature=0.2, max_output_tokens=512, timeout=20.0,
        )
        intent = _normalise(raw, intake)
    except Exception as exc:  # noqa: BLE001 — LLM is advisory, never fatal
        logger.warning("[reco.intent] extraction failed, using deterministic: %s", str(exc)[:160])
        return deterministic_intent(intake)

    cache.set_json(ckey, intent, ttl=86400)
    return intent
