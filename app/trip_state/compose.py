"""
Unified trip generator — `generate_from_spec` builds a whole trip (0..N stops)
in ONE pass from the immutable Trip Specification
(docs/trip_engine_architecture.md §2, §5, M3).

Pipeline:
  spec → graph skeleton (nodes+legs, nights allocated, no options)
       → leg search via the builder's stabilization loop
         (_sync_legs_after_change: resequence → re-point → re-search, twice —
          the same machinery structural edits use, so dates and travel-time
          cascades behave identically everywhere)
       → gateway fallback for unroutable legs (auto-picks the best hub)
       → per-stop itineraries + stays (parallel)
       → selection carry-forward (regeneration keeps the user's picks where
         the same ride still exists)
       → store-first mirror (permanent result_ids stamped onto options)
       → ONE atomic critical commit (graph + projections + manifest)

Zero stops is simply origin → destination → origin through the same code path.
"""
from __future__ import annotations

import asyncio
import logging

from . import builder as B
from . import graph as G

logger = logging.getLogger(__name__)


# ── nights allocation ─────────────────────────────────────────────────────────

def allocate_nights(days: int, stops: list[dict]) -> tuple[list[int], int]:
    """(nights per stop, terminal nights). Deterministic: explicit nights are
    honored, unspecified stops get 1, the terminal absorbs the remainder
    (minimum 1). A trip of `days` days has days-1 nights to distribute."""
    total_nights = max(int(days or 1) - 1, 1)
    stop_nights = [max(int(s.get("nights") or 1), 1) for s in stops]
    terminal = max(total_nights - sum(stop_nights), 1)
    return stop_nights, terminal


# ── graph skeleton ────────────────────────────────────────────────────────────

def build_skeleton(spec: dict) -> G.TripGraph:
    """Nodes and empty legs for origin → stops… → terminal → origin. No
    provider data; the stabilization loop fills options in."""
    origin_city = (spec.get("origin") or "").strip()
    dest_city = (spec.get("destination") or "").strip()
    stops = spec.get("stops") or []
    stop_nights, terminal_nights = allocate_nights(spec.get("days") or 3, stops)

    graph = G.TripGraph()
    origin = G.TripStop(id="stop-0", city=origin_city, role="origin", nights=0)
    graph.nodes.append(origin)
    prev = origin
    for i, (stop, nights) in enumerate(zip(stops, stop_nights), start=1):
        node = G.TripStop(id=f"stop-{i}", city=stop["city"].strip(), role="stop", nights=nights)
        graph.nodes.append(node)
        graph.edges.append(G.TripLeg(
            id=f"leg-{len(graph.edges) + 1}", from_node_id=prev.id, to_node_id=node.id,
            from_city=prev.city, to_city=node.city, routes=[], status="routed",
        ))
        prev = node
    terminal = G.TripStop(id=f"stop-{len(stops) + 1}", city=dest_city,
                          role="terminal", nights=terminal_nights)
    graph.nodes.append(terminal)
    graph.edges.append(G.TripLeg(
        id=f"leg-{len(graph.edges) + 1}", from_node_id=prev.id, to_node_id=terminal.id,
        from_city=prev.city, to_city=terminal.city, routes=[], status="routed",
    ))
    graph.edges.append(G.TripLeg(
        id=f"leg-{len(graph.edges) + 1}", from_node_id=terminal.id, to_node_id=origin.id,
        from_city=terminal.city, to_city=origin.city, routes=[], status="routed",
    ))
    return graph


# ── gateway fallback ──────────────────────────────────────────────────────────

async def _route_via_best_gateway(leg: G.TripLeg, travelers: int, budget: str,
                                  user_id: str, stay_nights: int) -> str | None:
    """A leg with no direct options gets routed through the best nearby hub —
    the composer decides (cheapest fare, else nearest) instead of asking, the
    way the chat flow did. Returns a note, or None if no gateway worked."""
    from app.services.geo_nearby import find_gateways

    try:
        gateways = await find_gateways(leg.from_city, leg.to_city, leg.date or "",
                                       travelers=travelers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compose] gateway lookup %s→%s failed: %s",
                       leg.from_city, leg.to_city, exc)
        return None
    priced = [g for g in (gateways or []) if g.get("fare_per_person")]
    ranked = sorted(priced, key=lambda g: g["fare_per_person"]) or (gateways or [])
    for gw in ranked[:2]:
        via = gw.get("city")
        if not via:
            continue
        hop1, hop2 = await asyncio.gather(
            B._shaped_journey(leg.from_city, via, leg.date, travelers, budget, user_id),
            B._shaped_journey(via, leg.to_city, leg.date, travelers, budget, user_id,
                              stay_nights=stay_nights or 1),
        )
        if not (B._block_has_options(hop1) and B._block_has_options(hop2)):
            continue
        hop2_search = {
            "flights": {"flights": (hop2.get("flights") or {}).get("items") or []},
            "trains": {"trains": (hop2.get("trains") or {}).get("items") or []},
            "buses": {"buses": (hop2.get("buses") or {}).get("items") or []},
        }
        leg.routes = [B._gateway_route(leg.id, via, hop1, hop2_search,
                                       leg.from_city, leg.to_city, leg.date)]
        leg.status = "routed"
        leg.selection = None
        leg.searched_date = leg.date  # extra="allow"
        return f"routed {leg.from_city} → {leg.to_city} via {via}"
    return None

