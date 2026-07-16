"""
Legacy trip_states.state → TripDoc conversion. TRANSITION-ONLY.

This is the last consumer of the old read machinery: it hydrates thin graph
options back from the search store, derives a graph for pre-graph trips, and
serializes through the same doc_from_graph the new generator uses — so a
converted trip and a freshly generated one are indistinguishable.

Deleted together with app/trip_state once every live trip carries a doc.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.trip_state import graph as G
from app.trips_doc.serialize import doc_from_graph

logger = logging.getLogger(__name__)


def _spec_of(state: dict) -> dict:
    """The trip's spec — derived from `request` for trips that predate spec
    formalization (mirrors the old endpoint's _spec_of)."""
    spec = state.get("spec") or {}
    if spec.get("origin"):
        return dict(spec)
    req = state.get("request") or {}
    return {
        "origin": req.get("origin"), "destination": req.get("destination"),
        "departure_date": req.get("departure_date"), "days": req.get("days") or 3,
        "travelers": req.get("travelers") or 1,
        "budget_tier": req.get("budget_tier") or "mid",
        "interests": req.get("interests") or [], "stops": req.get("stops") or [],
        "version": 1,
    }


def needs_conversion(row: dict) -> bool:
    """A row needs (re)conversion when it has legacy content the doc doesn't
    reflect yet. v3-native trips keep state == {} and are never reconverted."""
    doc = row.get("doc")
    state = row.get("state") or {}
    has_legacy = bool(state.get("meta") or state.get("graph") or state.get("transport"))
    if not has_legacy:
        return False
    if not doc:
        return True
    marker = doc.get("last_seq_converted")
    marker = int(marker) if marker is not None else -1   # 0 is a valid marker
    return (row.get("last_seq") or 0) > marker


def _legacy_days_onto_terminal(state: dict, graph: G.TripGraph) -> None:
    """Pre-graph trips keep their day-by-day plan in the flat itinerary block;
    the derived graph's terminal node may not carry it. Attach so the doc's
    per-stop days are complete."""
    days = (state.get("itinerary") or {}).get("days") or []
    if not days:
        return
    terminal = next((n for n in graph.nodes if n.role == "terminal"), None)
    if terminal is not None and not terminal.days:
        terminal.days = days


def _legacy_stay_onto_terminal(state: dict, graph: G.TripGraph) -> None:
    """Same for the flat stay block (hotel list + selected)."""
    stay = state.get("stay") or {}
    hotels = [h for h in (stay.get("hotels") or []) if isinstance(h, dict)]
    if not hotels:
        return
    terminal = next((n for n in graph.nodes if n.role == "terminal"), None)
    if terminal is None or terminal.stay.search_results:
        return
    terminal.stay.search_results = hotels
    selected = stay.get("selected")
    if isinstance(selected, dict):
        terminal.stay.selected_id = str(
            selected.get("id") or selected.get("hotel_id") or selected.get("name") or "") or None


async def doc_from_legacy_row(row: dict) -> dict:
    """Convert one trip_states row (with `state`) into a TripDoc.

    Search-store hydration (re-attaching provider raws to thinned graph
    options) was deleted with app/trip_state/retrieval.py — every live trip
    was already backfilled WITH hydration (scripts/backfill_trip_docs.py ran
    before the deletion). A straggler legacy row converts degraded: options
    keep their denormalized fields but lose deep provider detail."""
    trip_id = str(row["id"])
    state = row.get("state") or {}

    graph = G.ensure_graph(state)
    G.order_nodes_along_path(graph)
    _legacy_days_onto_terminal(state, graph)
    _legacy_stay_onto_terminal(state, graph)

    meta = state.get("meta") or {}
    dest = state.get("destination") or {}
    status = row.get("status") or meta.get("status") or "ready"
    doc = doc_from_graph(
        trip_id,
        _spec_of(state),
        graph,
        title=row.get("title") or meta.get("title"),
        status=status if status in ("planning", "ready", "error") else "ready",
        progress=100 if status != "planning" else 50,
        destination={k: v for k, v in {
            "name": dest.get("name"),
            "description": dest.get("description"),
            "cover_photo_ref": dest.get("cover_photo_ref"),
        }.items() if v is not None} or None,
        weather=state.get("weather") or None,
        documents=(state.get("documents") or None),
        generation=(meta.get("generation") or None),
        base_budget=state.get("costs") or None,
    )
    doc["last_seq_converted"] = row.get("last_seq") or 0
    if row.get("created_at"):
        doc["generated_at"] = str(row["created_at"])
    doc["converted_at"] = datetime.now(timezone.utc).isoformat()
    return doc
