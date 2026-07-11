"""
Per-user travel preference memory.

A lightweight, learned profile the agents read to warm-start planning,
recommendations, and the copilot — so a returning traveller isn't treated as a
blank slate. Backed by the `user_travel_memory` table (one JSONB row per user);
no external memory dependency.

Two ways memory grows, both best-effort (a memory write must NEVER fail an edit
or a generation):

  record_signals()   — deterministic capture of EXPLICIT signals as they happen
                       (the budget tier picked, hotel types searched, the axis a
                       user optimised transport for, activity kinds added).
  distill_from_trip() — one LLM pass at trip completion that reads the finished
                       plan and writes a short prose `notes` + structured prefs.

memory_grounding() renders the stored memory into a compact block the copilot
router and the recommendation layer fold into their prompts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# List-valued memory keys are UNIONED across trips (dedup, most-recent-last),
# capped so the profile stays compact. Scalar keys are overwritten with the
# latest value; `trips_planned` is a counter.
_LIST_KEYS = {
    "hotel_property_types", "amenities", "interests", "dietary", "destinations",
}
_LIST_CAP = 12
_SCALAR_KEYS = {"budget_tier", "pace", "transport_pref", "notes"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Read ─────────────────────────────────────────────────────────────────────

def _read_sync(user_id: str) -> dict:
    from app.db.client import get_db

    res = (
        get_db().table("user_travel_memory")
        .select("memory").eq("user_id", user_id).limit(1).execute()
    )
    return (res.data[0].get("memory") if res.data else None) or {}


async def get_memory(user_id: str) -> dict:
    """Return the user's learned memory dict, or {} on any failure."""
    if not user_id:
        return {}
    from app.db.client import run_db

    try:
        return await run_db(_read_sync, user_id)
    except Exception as exc:  # noqa: BLE001 — memory is advisory, never fatal
        logger.warning("[memory] read failed for %s: %s", user_id, exc)
        return {}


# ── Merge + write ────────────────────────────────────────────────────────────

def _merge(existing: dict, signals: dict) -> dict:
    """Fold `signals` into `existing`: lists union (capped), scalars overwrite,
    trips_planned increments. Empty/None signal values are ignored."""
    out = dict(existing or {})
    for key, val in (signals or {}).items():
        if val in (None, "", [], {}):
            continue
        if key in _LIST_KEYS:
            merged = list(out.get(key) or [])
            incoming = val if isinstance(val, list) else [val]
            for item in incoming:
                s = str(item).strip().lower()
                if s and s not in merged:
                    merged.append(s)
            out[key] = merged[-_LIST_CAP:]
        elif key == "trips_planned":
            out[key] = int(out.get("trips_planned") or 0) + int(val)
        else:  # scalar (budget_tier, pace, transport_pref, notes, …)
            out[key] = val
    return out


def _write_sync(user_id: str, signals: dict) -> None:
    from app.db.client import get_db

    db = get_db()
    existing = _read_sync(user_id)
    merged = _merge(existing, signals)
    db.table("user_travel_memory").upsert({
        "user_id": user_id,
        "memory": merged,
        "updated_at": _now(),
    }).execute()


async def record_signals(user_id: str, signals: dict) -> None:
    """Merge explicit preference signals into the user's memory. Best-effort."""
    if not user_id or not signals:
        return
    from app.db.client import run_db

    try:
        await run_db(_write_sync, user_id, signals)
    except Exception as exc:  # noqa: BLE001 — never block the caller
        logger.warning("[memory] write failed for %s: %s", user_id, exc)


# ── Distillation at trip completion ──────────────────────────────────────────

_DISTILL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "budget_tier": {"type": "STRING", "nullable": True, "enum": ["low", "mid", "high"]},
        "pace": {"type": "STRING", "nullable": True, "enum": ["relaxed", "balanced", "packed"]},
        "hotel_property_types": {"type": "ARRAY", "nullable": True, "items": {"type": "STRING"}},
        "interests": {"type": "ARRAY", "nullable": True, "items": {"type": "STRING"}},
        "notes": {"type": "STRING", "nullable": True},
    },
}


def _trip_digest(state: dict) -> str:
    req = state.get("request") or {}
    stay = (state.get("stay") or {}).get("selected") or {}
    days = (state.get("itinerary") or {}).get("days") or []
    cats = [
        a.get("category")
        for d in days for a in (d.get("activities") or [])
        if a.get("category")
    ]
    top_cats = ", ".join(dict.fromkeys(cats))[:200]
    return (
        f"Destination: {req.get('destination')}; {req.get('days')} days, "
        f"{req.get('travelers')} travelers, budget tier {req.get('budget_tier')}.\n"
        f"Interests given: {', '.join(req.get('interests') or []) or 'none'}.\n"
        f"Chose to stay at: {stay.get('name') or 'n/a'} "
        f"({stay.get('property_type') or '?'}, ₹{stay.get('price_per_night') or stay.get('price') or '?'}/night).\n"
        f"Itinerary activity kinds: {top_cats or 'n/a'} across {len(days)} days."
    )


async def distill_from_trip(user_id: str, state: dict) -> None:
    """One LLM pass over a completed plan → durable preference signals. Best-effort;
    always also records the deterministic signals even if the LLM pass fails."""
    if not user_id:
        return
    req = state.get("request") or {}

    # Deterministic signals we can capture without the model.
    base_signals: dict[str, Any] = {
        "budget_tier": req.get("budget_tier"),
        "interests": req.get("interests") or [],
        "destinations": [req.get("destination")] if req.get("destination") else [],
        "trips_planned": 1,
    }
    selected = (state.get("stay") or {}).get("selected") or {}
    if selected.get("property_type"):
        base_signals["hotel_property_types"] = [selected["property_type"]]

    distilled: dict = {}
    try:
        from app.services.vertex import generate_json

        distilled = await generate_json(
            "You maintain a returning traveller's preference profile. From the "
            "trip they just finished planning, infer durable preferences (not "
            "one-off facts). Write a one-sentence `notes` in the third person "
            "capturing what matters to them (e.g. price sensitivity, group type, "
            "vibe). Leave a field null if the trip doesn't clearly signal it.\n\n"
            f"TRIP:\n{_trip_digest(state)}",
            tier="muscle", response_schema=_DISTILL_SCHEMA,
            temperature=0.2, max_output_tokens=256, timeout=20.0,
        )
    except Exception as exc:  # noqa: BLE001 — distillation is optional
        logger.warning("[memory] distill failed for %s: %s", user_id, exc)

    signals = _merge(base_signals, {
        k: v for k, v in (distilled or {}).items() if v not in (None, "", [])
    })
    await record_signals(user_id, signals)


# ── Grounding for prompts ────────────────────────────────────────────────────

def memory_grounding(memory: dict) -> str:
    """Render memory into a compact block for the copilot router / rec context.
    Empty memory → '' so callers can conditionally include it."""
    if not memory:
        return ""
    bits: list[str] = []
    if memory.get("budget_tier"):
        bits.append(f"usually travels on a {memory['budget_tier']} budget")
    if memory.get("pace"):
        bits.append(f"prefers a {memory['pace']} pace")
    if memory.get("hotel_property_types"):
        bits.append("likes staying in " + ", ".join(memory["hotel_property_types"][:4]))
    if memory.get("transport_pref"):
        bits.append(f"tends to optimise transport for {memory['transport_pref']}")
    if memory.get("interests"):
        bits.append("into " + ", ".join(memory["interests"][:5]))
    if memory.get("dietary"):
        bits.append("dietary: " + ", ".join(memory["dietary"][:3]))
    line = "; ".join(bits)
    notes = (memory.get("notes") or "").strip()
    if notes:
        line = f"{line}. {notes}" if line else notes
    return line.strip()
