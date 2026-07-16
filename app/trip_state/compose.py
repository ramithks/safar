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

from app.core.background import spawn_background
from app.db.client import run_db, run_db_resilient

from . import builder as B
from . import graph as G
from . import integrity
from .patch import PatchOp

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


# ── selection carry-forward ───────────────────────────────────────────────────

def _option_identity(opt: G.TransportOption) -> tuple:
    raw = opt.raw or {}
    ext = raw.get("flight_number") or raw.get("train_number") or raw.get("service_number")
    return (opt.mode, (opt.operator or "").strip().lower(), str(ext or ""))


def carry_forward_selections(prior_graph: dict | None, graph: G.TripGraph) -> list[str]:
    """Re-apply the user's picks onto a regenerated graph where the SAME ride
    (mode + operator + service number) still exists. Unmatched picks are
    dropped and reported (docs/trip_engine_architecture.md §6)."""
    if not prior_graph:
        return []
    dropped: list[str] = []
    prior_edges = {}
    prior_options = {}
    for e in prior_graph.get("edges") or []:
        key = ((e.get("from_city") or "").lower(), (e.get("to_city") or "").lower())
        prior_edges[key] = e
        for r in e.get("routes") or []:
            for h in r.get("hops") or []:
                for o in h.get("options") or []:
                    if isinstance(o, dict):
                        prior_options[o.get("id")] = o

    for leg in graph.edges:
        prior = prior_edges.get((leg.from_city.lower(), leg.to_city.lower()))
        sel = (prior or {}).get("selection")
        if not sel or not leg.routes:
            continue
        wanted = []
        for oid in sel.get("per_hop_option_ids") or []:
            po = prior_options.get(oid)
            if po:
                raw = po.get("raw") or {}
                ext = raw.get("flight_number") or raw.get("train_number") or raw.get("service_number")
                wanted.append((po.get("mode"), (po.get("operator") or "").strip().lower(),
                               str(ext or "")))
        if not wanted:
            continue
        route = leg.routes[0]
        new_ids: list[str | None] = []
        for hop, ident in zip(route.hops, wanted):
            match = next((o for o in hop.options if _option_identity(o) == ident), None)
            new_ids.append(match.id if match else None)
        if len(new_ids) == len(route.hops) and all(new_ids):
            leg.selection = G.LegSelection(route_id=route.id, per_hop_option_ids=new_ids)
        else:
            dropped.append(f"{leg.from_city} → {leg.to_city}")
    return dropped


# ── the pipeline ──────────────────────────────────────────────────────────────

async def generate_from_spec(trip_id: str, spec: dict, user_id: str, emit,
                             manifest: dict, prior_graph: dict | None = None) -> dict:
    """Build the whole trip from `spec` and land ONE atomic critical commit.
    Returns the final state dict (for the My Trips mirror / memory distill).
    Raises on catastrophic failure — the caller owns the error commit."""
    from app.api.v1.plan_trip.models import PlanTripRequest
    from app.api.v1.plan_trip.orchestrator import run_plan_trip

    from . import sync_store

    travelers = max(int(spec.get("travelers") or 1), 1)
    budget = spec.get("budget_tier") or spec.get("budget") or "mid"
    interests = spec.get("interests") or []
    stops = spec.get("stops") or []

    # Local working state — _graph_patches projects into it; the critical emit
    # persists the sections onto the stored snapshot.
    state: dict = {
        "spec": spec,
        "request": {
            "origin": spec.get("origin"), "destination": spec.get("destination"),
            "departure_date": spec.get("departure_date"), "days": spec.get("days") or 3,
            "stay_nights": max(int(spec.get("days") or 3) - 1, 1),
            "travelers": travelers, "budget_tier": budget,
            "interests": interests, "stops": stops,
        },
        "transport": {}, "itinerary": {}, "costs": {}, "stay": {}, "destination": {},
    }

    graph = build_skeleton(spec)
    B._resequence_dates(graph, spec.get("departure_date"))
    route_label = " → ".join([n.city for n in graph.nodes])
    await emit("understanding", f"Planning {route_label}", [], 8)

    # ── legs: the builder's stabilization loop searches every empty leg with
    # date-cascade correctness (two rounds). Concurrency is capped inside
    # _shaped_journey itself (builder._journey_semaphore), shared with edits.
    notes = await B._sync_legs_after_change(state, graph, user_id)

    # Gateway fallback for anything still unroutable (sequential — rare path).
    for leg in graph.edges:
        if leg.status == "no_route" or not leg.routes:
            note = await _route_via_best_gateway(
                leg, travelers, budget, user_id,
                stay_nights=(graph.node(leg.to_node_id) or G.TripStop(id="", city="")).nights,
            )
            if note:
                notes.append(note)
    B._resequence_dates(graph, spec.get("departure_date"))

    routed = sum(1 for e in graph.edges if e.routes)
    if routed == 0:
        raise RuntimeError(f"no leg of {route_label} could be routed")
    await emit("transport_ready", "Transport found for your route",
               B._graph_patches(state, graph), 45)

    # ── itineraries + stays for every city the traveller sleeps in ──────────
    stayed = [n for n in graph.nodes if n.role != "origin" and (n.nights or 0) > 0]

    async def _itinerary_for(node: G.TripStop) -> dict | None:
        days = (node.nights or 1) + (1 if node.role == "terminal" else 0)
        try:
            return await run_plan_trip(
                PlanTripRequest(
                    destination=node.city, days=min(days, 7), budget=budget,
                    travelers=travelers, interests=interests,
                    start_date=node.arrival_date, end_date=node.departure_date,
                    skip_user_trips=True,
                ),
                user_id, profile={},
            )
        except Exception as exc:  # noqa: BLE001 — a stop without an itinerary is degraded, not fatal
            logger.warning("[compose] itinerary for %s failed: %s", node.city, exc)
            return None

    trips = await asyncio.gather(*(_itinerary_for(n) for n in stayed))
    terminal_trip: dict = {}
    for node, trip in zip(stayed, trips):
        if not trip:
            continue
        node.days = [dict(d) for d in (trip.get("days") or [])]
        hotels = trip.get("hotels") or (trip.get("stay") or {}).get("hotels") or []
        if hotels:
            B._attach_stay(node, list(hotels), node.nights or 1)
        if node.role == "terminal":
            terminal_trip = trip

    # Stays for any stayed node the itinerary pipeline didn't cover.
    for node in stayed:
        if not node.stay.search_results:
            hotels = await B._fetch_stop_hotels(node, travelers, budget)
            B._attach_stay(node, hotels, node.nights or 1)

    dropped = carry_forward_selections(prior_graph, graph)
    if dropped:
        notes.append("couldn't keep your picks on: " + ", ".join(dropped))

    await B._geocode_nodes(graph)

    # ── store-first: permanent ids stamped BEFORE the graph commit ──────────
    try:
        await run_db_resilient(
            lambda: sync_store.mirror_graph_with_ids(trip_id, graph),
            label=f"store-first:{trip_id}", max_seconds=90,
        )
    except Exception as exc:  # noqa: BLE001 — degraded (result_id None), never blocking
        logger.warning("[compose] store-first mirror failed for %s — options keep "
                       "denormalized fields only: %s", trip_id, exc)

    # ── ONE atomic commit: graph + projections + stay + destination + manifest ──
    patches = B._graph_patches(state, graph)
    terminal_node = next((n for n in graph.nodes if n.role == "terminal"), None)
    if terminal_node and terminal_node.stay.search_results:
        patches += [
            PatchOp(op="set", path="/stay/hotels", value=terminal_node.stay.search_results),
            PatchOp(op="set", path="/stay/selected", value=None),
        ]
    title = (terminal_trip.get("trip_name")
             or f"{spec.get('origin')} → {spec.get('destination')}")
    patches += [
        PatchOp(op="set", path="/spec", value=spec),
        PatchOp(op="set", path="/request", value=state["request"]),
        PatchOp(op="set", path="/destination/name", value=spec.get("destination")),
        PatchOp(op="set", path="/destination/description",
                value=terminal_trip.get("destination_description")),
        PatchOp(op="set", path="/destination/cover_photo_ref",
                value=terminal_trip.get("cover_photo_ref")),
        PatchOp(op="set", path="/itinerary/intelligence",
                value=terminal_trip.get("trip_intelligence") or {}),
        PatchOp(op="set", path="/meta/title", value=title),
        PatchOp(op="set", path="/meta/status", value="ready"),
        PatchOp(op="set", path="/meta/generation",
                value=integrity.finalize_manifest(manifest)),
    ]
    if notes:
        patches.append(PatchOp(op="set", path="/agent_meta/notes",
                               value=[{"agent": "composer", "message": n} for n in notes]))
    await emit("complete", "Trip ready", patches, 100, critical=True)
    logger.info("[compose] %s built: %s | legs routed %d/%d | %d stayed stops",
                trip_id, route_label, routed, len(graph.edges), len(stayed))

    # Post-commit, best-effort: score the fresh batches for /recommendations.
    async def _score() -> None:
        from app.reco import index
        for b in await run_db(sync_store._ready_batches, trip_id):
            await index.score_batch(trip_id, b["id"], scope_id=b.get("scope_id") or "",
                                    kind=b.get("kind") or "transport")
    spawn_background(_score(), name=f"reco-score:{trip_id}")

    # Final state dict for the shared tail (My Trips mirror, memory distill).
    fd = dict(state)
    fd["meta"] = {"trip_id": trip_id, "status": "ready", "title": title,
                  "generation": manifest}
    return fd
