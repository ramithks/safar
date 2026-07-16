"""
Instant doc mutations — what a click in the workspace does.

Pure functions: doc in → (doc, human note) out. No planning logic, no LLM,
no graph. The endpoint persists the result with one UPDATE.

Raises ValueError with a user-safe message on bad targets (endpoint → 400).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .serialize import compute_budget


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_transport(doc: dict, leg_id: str, option_id: str, hop_index: int = 0) -> tuple[dict, str]:
    leg = next((l for l in doc.get("transport") or [] if l.get("leg_id") == leg_id), None)
    if leg is None:
        raise ValueError(f"No leg '{leg_id}' on this trip")
    hops = leg.get("hops") or []
    if not (0 <= hop_index < len(hops)):
        raise ValueError(f"Leg {leg.get('from')} → {leg.get('to')} has no hop {hop_index}")
    hop = hops[hop_index]
    option = next((o for o in hop.get("options") or [] if o.get("id") == option_id), None)
    if option is None:
        raise ValueError("That ride is no longer in the list — refresh and pick again")
    hop["selected_id"] = option_id
    doc["budget"] = compute_budget(doc)
    doc["updated_at"] = _now()
    name = option.get("name") or option.get("operator") or option.get("mode") or "ride"
    return doc, f"Picked {name} for {hop.get('from')} → {hop.get('to')}"


def select_hotel(doc: dict, stop_id: str, hotel_id: str) -> tuple[dict, str]:
    stops = doc.get("itinerary") or []
    stop = next((s for s in stops if s.get("stop_id") == stop_id), None)
    if stop is None:  # allow city-name targeting for convenience
        key = (stop_id or "").strip().lower()
        stop = next((s for s in stops if (s.get("city") or "").strip().lower() == key), None)
    if stop is None:
        raise ValueError(f"No stop '{stop_id}' on this trip")
    block = stop.setdefault("hotels", {})
    hotel = next((h for h in block.get("options") or [] if h.get("id") == hotel_id), None)
    if hotel is None:
        raise ValueError("That stay is no longer in the list — refresh and pick again")
    block["selected_id"] = hotel_id
    doc["budget"] = compute_budget(doc)
    doc["updated_at"] = _now()
    return doc, f"{hotel.get('name') or 'Stay'} set for {stop.get('city')}"
