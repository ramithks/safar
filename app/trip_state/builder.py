"""
Trip builder — the copilot moves that grow a trip into a multi-city graph, one
turn at a time:

  handle_add_leg            — "add a flight from A to B", "from B to C by train",
                              "come back from C to A". Routes the hop (direct +
                              gateway candidates in parallel), appends the edge,
                              creates the destination node if new, reprices.
                              When no direct route exists, offers 2-3 nearby
                              gateway cities that DO reach the destination.
  handle_add_stop_itinerary — "add a 2-day itinerary for B". Builds a real N-day
                              itinerary for a stop (same pipeline the planner
                              uses) and attaches it to that node.

Both operate on the canonical graph (app.trip_state.graph), regenerate the
compatibility projections, and emit patches for `/graph` plus the projected
`/transport`, `/itinerary/days`, `/costs` so live viewers update immediately.
The graph never stores a recommendation — see docs/trip_graph.md.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.services import geo

from . import graph as G
from .patch import PatchOp

logger = logging.getLogger(__name__)


# ── Small graph mutations ────────────────────────────────────────────────────────

def _fresh_id(prefix: str, used: set[str], start: int) -> str:
    """First `{prefix}-{n}` not already taken. Counting off `len()` alone breaks
    once pruning a displaced leg shrinks the list — the next index would collide
    with a surviving id (e.g. reusing leg-2 while the old leg-2 still exists),
    silently corrupting leg/stop targeting."""
    i = start
    while f"{prefix}-{i}" in used:
        i += 1
    return f"{prefix}-{i}"


def _next_stop_id(graph: G.TripGraph) -> str:
    return _fresh_id("stop", {n.id for n in graph.nodes}, len(graph.nodes))


def _next_leg_id(graph: G.TripGraph) -> str:
    return _fresh_id("leg", {e.id for e in graph.edges}, len(graph.edges))


def _prune_displaced(graph: G.TripGraph, from_node_id: str, keep_to_id: str) -> None:
    """Inserting a stop after `from` displaces `from`'s old onward leg — threading
    Goa in makes the original Bengaluru→Jaipur direct leg obsolete. Drop those
    stale successor edges (same source, a DIFFERENT target) so the graph stays a
    single clean path and the projection can't pick the phantom direct leg as the
    journey. The freshly added from→to leg and any return-to-origin leg survive."""
    origin = next((n for n in graph.nodes if n.role == "origin"), None)
    origin_id = origin.id if origin else None
    graph.edges = [
        e for e in graph.edges
        if not (
            e.from_node_id == from_node_id
            and e.to_node_id != keep_to_id
            and e.to_node_id != origin_id
        )
    ]


def _plus_days(date_str: str | None, n: int) -> str | None:
    if not date_str:
        return None
    try:
        return (datetime.strptime(date_str[:10], "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _ensure_node(graph: G.TripGraph, city: str, role_hint: str) -> G.TripStop:
    """Find the node for `city`, creating it if new. `role_hint` is used only when
    creating: the very first node is always the origin."""
    node = graph.node_by_city(city)
    if node:
        return node
    role = "origin" if not graph.nodes else role_hint
    node = G.TripStop(
        id=_next_stop_id(graph), city=city.strip(),
        normalized_city=geo.normalize(city), role=role,
    )
    graph.nodes.append(node)
    return node


async def _geocode_nodes(graph: G.TripGraph) -> None:
    """Best-effort coordinates for nodes that lack them — feeds the trip map.
    Hub cities resolve instantly from the static table; the rest hit the cached
    Places geocode. Never fails the edit."""
    from app.services.geo_nearby import geocode_city

    missing = [n for n in graph.nodes if not n.coordinates]
    if not missing:
        return
    try:
        results = await asyncio.gather(*(geocode_city(n.city) for n in missing))
        for node, coords in zip(missing, results):
            if coords:
                node.coordinates = {"lat": coords[0], "lng": coords[1]}
    except Exception as exc:  # noqa: BLE001 — the map is a nice-to-have
        logger.warning("[builder] geocoding stops failed: %s", exc)


def _resequence_dates(graph: G.TripGraph, start_date: str | None) -> None:
    """Re-sort nodes into travel order, then walk them assigning arrival/departure
    dates from each stop's nights and stamp every outbound leg's date from its
    origin node's departure. Ordering first is what keeps a mid-trip-inserted
    stop's dates (and everything derived from them) correct."""
    G.order_nodes_along_path(graph)
    cursor = start_date
    for node in graph.nodes:
        node.arrival_date = cursor
        node.departure_date = _plus_days(cursor, max(int(node.nights or 0), 0)) or cursor
        cursor = node.departure_date
    for leg in graph.edges:
        src = graph.node(leg.from_node_id)
        if src and src.departure_date:
            leg.date = src.departure_date
        for route in leg.routes:
            for hop in route.hops:
                hop.date = leg.date


# ── Live routing ─────────────────────────────────────────────────────────────────

async def _shaped_journey(from_city: str, to_city: str, date: str | None,
                          travelers: int, budget: str, user_id: str) -> dict | None:
    """Run the production journey pipeline for one hop and return the shaped block
    (buses/trains/flights buckets), or None on failure/empty."""
    from app.api.v1.plan_trip.journey import run_plan_journey
    from app.api.v1.plan_trip.models import PlanJourneyRequest
    from app.api.v1.plan_trip.plan import _shape_journey

    try:
        req = PlanJourneyRequest(
            origin=from_city, destination=to_city, travelers=travelers,
            budget=budget, departure_date=date, stay_nights=1,
        )
        result = await run_plan_journey(req, user_id, profile={})
        block = _shape_journey(result, include_hotels=False)
        return block
    except Exception as exc:  # noqa: BLE001 — a routing miss is not fatal
        logger.warning("[builder] journey %s→%s failed: %s", from_city, to_city, exc)
        return None


def _block_has_options(block: dict | None) -> bool:
    if not block:
        return False
    return any((block.get(k) or {}).get("items") for k in ("flights", "trains", "buses"))


def _direct_route(leg_id: str, block: dict, from_city: str, to_city: str, date: str | None) -> G.RouteCandidate:
    # Chain-aware: a shaped journey with multiple bookable legs (train + bus,
    # or even train + train) becomes one hop per leg. The flag-derived selection
    # is discarded — for a FRESH search the flags are the planner's suggestion,
    # and recommendation stays derived, never stored.
    route, _ = G._direct_route_from_block(f"{leg_id}-direct", block, from_city, to_city, date)
    return route


def _gateway_route(leg_id: str, gateway: str, hop1_block: dict, hop2_search: dict,
                   from_city: str, to_city: str, date: str | None) -> G.RouteCandidate:
    """Build a two-hop via-gateway route: from→gateway (a freshly-shaped journey
    block) then gateway→dest (the raw search_all captured by the finder)."""
    hop2 = G.hop_from_journey_block(
        {  # reshape search_all → the bucket shape hop_from_journey_block reads
            "flights": {"items": (hop2_search.get("flights") or {}).get("flights") or []},
            "trains": {"items": (hop2_search.get("trains") or {}).get("trains") or []},
            "buses": {"items": (hop2_search.get("buses") or {}).get("buses") or []},
        },
        gateway, to_city, date,
    )
    return G.RouteCandidate(
        id=f"{leg_id}-via-{geo.normalize(gateway)}", kind="via_gateway", gateway_city=gateway,
        hops=[G.hop_from_journey_block(hop1_block, from_city, gateway, date), hop2],
    )


# ── Patch emission ───────────────────────────────────────────────────────────────

def _graph_patches(state: dict, graph: G.TripGraph) -> list[PatchOp]:
    """Persist the graph and its regenerated projections. Emits `/graph` plus the
    merged legacy projections so both graph-aware and current clients converge."""
    graph_dump = graph.model_dump(mode="json")
    state["graph"] = graph_dump
    proj = G.project(state, graph)
    patches = [PatchOp(op="set", path="/graph", value=graph_dump)]

    if "/transport" in proj:
        merged = {**(state.get("transport") or {}), **proj["/transport"]}
        state["transport"] = merged
        patches.append(PatchOp(op="set", path="/transport", value=merged))
    if "/itinerary/days" in proj:
        state.setdefault("itinerary", {})["days"] = proj["/itinerary/days"]
        patches.append(PatchOp(op="set", path="/itinerary/days", value=proj["/itinerary/days"]))
    if "/costs" in proj:
        state["costs"] = proj["/costs"]
        patches.append(PatchOp(op="set", path="/costs", value=proj["/costs"]))
    return patches


# ── Handlers ─────────────────────────────────────────────────────────────────────

async def handle_add_leg(
    state: dict, user_id: str, from_city: str, to_city: str,
    mode: str | None = None, date: str | None = None, via: str | None = None,
) -> tuple[list[PatchOp], str, list[str]]:
    """Add a transport leg from `from_city` to `to_city`. Returns
    (patches, reply, clarify_options). When there's no direct route and no `via`
    was given, patches is empty and clarify_options holds the gateway chips."""
    from_city = (from_city or "").strip()
    to_city = (to_city or "").strip()
    if not from_city or not to_city:
        return [], "Which two cities should this leg connect?", []

    req = state.get("request") or {}
    travelers = max(int(req.get("travelers") or 1), 1)
    budget = req.get("budget_tier") or "mid"
    start_date = req.get("departure_date")

    graph = G.ensure_graph(state)

    # Idempotency: a leg we already have is not re-added. This is the backstop
    # that stops the agent's re-plan from stacking duplicate legs and looping
    # (the "Added the Goa → Jaipur leg" ×4 symptom). An explicit `via` is a
    # reroute request, so it replaces the existing leg rather than no-opping.
    existing = _find_leg(graph, None, from_city, to_city)
    if existing and not via:
        fare = round(G.leg_fare_per_person(existing, prefer=mode, budget=budget))
        price = f" (₹{fare}/person)" if fare else ""
        return [], (
            f"The {from_city} → {to_city} leg is already in your trip{price} — "
            "say “via <city>” to reroute it, or pick a specific ride to change it."
        ), []

    from_node = _ensure_node(graph, from_city, role_hint="stop")
    # A leg whose target is the origin city is the return — _ensure_node resolves
    # it to the existing origin node, which the projection treats as inbound.
    to_node = _ensure_node(graph, to_city, role_hint="stop")
    if existing and via:
        graph.edges = [e for e in graph.edges if e.id != existing.id]  # replace on reroute
    _resequence_dates(graph, start_date)
    leg_id = _next_leg_id(graph)
    leg_date = from_node.departure_date or start_date

    # ── Explicit gateway: build the two-hop route directly. ──
    if via:
        via = via.strip()
        hop1, hop2 = await asyncio.gather(
            _shaped_journey(from_city, via, leg_date, travelers, budget, user_id),
            _shaped_journey(via, to_city, _plus_days(leg_date, 0), travelers, budget, user_id),
        )
        if not _block_has_options(hop1) or not _block_has_options(hop2):
            return [], (
                f"I couldn't stitch a route through {via} — one of the "
                f"{from_city}→{via} or {via}→{to_city} hops came back empty. "
                "Try a different gateway city."
            ), []
        hop2_search = {
            "flights": {"flights": (hop2.get("flights") or {}).get("items") or []},
            "trains": {"trains": (hop2.get("trains") or {}).get("items") or []},
            "buses": {"buses": (hop2.get("buses") or {}).get("items") or []},
        }
        route = _gateway_route(leg_id, via, hop1, hop2_search, from_city, to_city, leg_date)
        leg = G.TripLeg(
            id=leg_id, from_node_id=from_node.id, to_node_id=to_node.id,
            from_city=from_city, to_city=to_city, date=leg_date,
            routes=[route], status="routed",
        )
        graph.edges.append(leg)
        _prune_displaced(graph, from_node.id, to_node.id)
        _resequence_dates(graph, start_date)
        await _geocode_nodes(graph)
        patches = _graph_patches(state, graph)
        fare = round(G.leg_fare_per_person(leg, budget=budget))
        return patches, (
            f"Routed {from_city} → {to_city} via {via} — {from_city}→{via} then "
            f"{via}→{to_city}, ₹{fare}/person. Added it as the {from_city}→{to_city} leg."
        ), []

    # ── Direct routing + gateway discovery, in parallel. ──
    from app.services.geo_nearby import find_gateways

    direct_block, gateways = await asyncio.gather(
        _shaped_journey(from_city, to_city, leg_date, travelers, budget, user_id),
        find_gateways(from_city, to_city, leg_date or "", travelers=travelers),
    )

    if _block_has_options(direct_block):
        route = _direct_route(leg_id, direct_block, from_city, to_city, leg_date)
        leg = G.TripLeg(
            id=leg_id, from_node_id=from_node.id, to_node_id=to_node.id,
            from_city=from_city, to_city=to_city, date=leg_date,
            routes=[route], status="routed",
        )
        graph.edges.append(leg)
        _prune_displaced(graph, from_node.id, to_node.id)
        _resequence_dates(graph, start_date)
        await _geocode_nodes(graph)
        patches = _graph_patches(state, graph)

        best = G.recommended_option(route.hops[0], prefer=mode, budget=budget)
        fare = round(G.leg_fare_per_person(leg, prefer=mode, budget=budget))
        picked = f"{best.operator} ({best.mode})" if best else "the best option"
        reply = f"Added the {from_city} → {to_city} leg — {picked}, ₹{fare}/person."
        # Proactively surface a cheaper gateway route if one clearly beats direct.
        if gateways and fare:
            cheap = min((g for g in gateways if g.get("fare_per_person")),
                        key=lambda g: g["fare_per_person"], default=None)
            if cheap and cheap["fare_per_person"] < fare * 0.85:
                reply += (
                    f" Heads-up: routing via {cheap['city']} could be cheaper "
                    f"(~₹{round(cheap['fare_per_person'])}/person on the {cheap['city']}→"
                    f"{to_city} hop). Say “via {cheap['city']}” to use it."
                )
        return patches, reply, []

    # ── No direct route: offer nearby gateways. ──
    if gateways:
        lines = []
        chips = []
        for g in gateways:
            fare_txt = f", from ₹{round(g['fare_per_person'])}/person" if g.get("fare_per_person") else ""
            lines.append(f"• {g['city']} ({g['distance_km']} km away) — {g['mode']}{fare_txt} onward to {to_city}")
            chips.append(f"Via {g['city']}")
        body = "\n".join(lines)
        return [], (
            f"There's no direct {from_city} → {to_city} route. But you can get there "
            f"through a nearby hub:\n{body}\n\nWhich gateway should I route through?"
        ), chips

    return [], (
        f"I couldn't find any {from_city} → {to_city} route, direct or via a nearby "
        "hub, on those dates. Try different dates or a nearby bigger city."
    ), []


async def handle_add_stop_itinerary(
    state: dict, user_id: str, city: str, days: int | None,
) -> tuple[list[PatchOp], str]:
    """Build a real N-day itinerary for a stop and attach it to that node.
    Creates the node if the leg to it hasn't been added yet."""
    from app.api.v1.plan_trip.models import PlanTripRequest
    from app.api.v1.plan_trip.orchestrator import run_plan_trip

    city = (city or "").strip()
    if not city:
        return [], "Which city should I build the itinerary for?"
    req = state.get("request") or {}
    travelers = max(int(req.get("travelers") or 1), 1)
    budget = req.get("budget_tier") or "mid"
    n_days = max(1, min(int(days or 2), 7))

    graph = G.ensure_graph(state)
    node = _ensure_node(graph, city, role_hint="stop")

    # Idempotency: don't rebuild an itinerary that's already there at the asked
    # length (another loop/duplication backstop). A different day count falls
    # through to a rebuild so "make it 3 days in Goa" still resizes.
    if node.days and node.nights == n_days:
        spots = [a.get("title") for d in node.days for a in (d.get("activities") or [])][:3]
        tail = f" — {', '.join(s for s in spots if s)}" if any(spots) else ""
        return [], (
            f"You already have a {n_days}-day itinerary for {city}{tail}. Ask me "
            "to add specific spots, or change the day count to reshape it."
        )

    try:
        trip = await run_plan_trip(
            PlanTripRequest(
                destination=city, days=n_days, budget=budget, travelers=travelers,
                skip_user_trips=True,
            ),
            user_id, profile={},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[builder] itinerary for %s failed: %s", city, exc)
        return [], f"I couldn't pull an itinerary for {city} right now — try again in a moment."

    day_list = [dict(d) for d in ((trip or {}).get("days") or [])]
    if not day_list:
        return [], f"I couldn't find enough to fill {n_days} days in {city} right now."

    node.days = day_list
    node.nights = n_days
    # Attach hotel candidates for the stay, if the trip pipeline returned any.
    hotels = (trip or {}).get("hotels") or (trip or {}).get("stay", {}).get("hotels") or []
    if hotels:
        node.stay = G.Stay(search_results=list(hotels))

    _resequence_dates(graph, req.get("departure_date"))
    await _geocode_nodes(graph)
    patches = _graph_patches(state, graph)

    spots = [a.get("title") for d in day_list for a in (d.get("activities") or [])][:3]
    tail = f" — {', '.join(s for s in spots if s)}" if any(spots) else ""
    return patches, (
        f"Added a {n_days}-day itinerary for {city}{tail}. It's slotted into your "
        "trip in order."
    )


def _live_prices_total(state: dict) -> int:
    """Transport + stay at live operator prices — the ONLY total the workspace
    surfaces (food/activities/misc estimates are excluded everywhere in the UI).
    `costs.total` additionally folds in those estimates, so quoting it in a
    copilot reply contradicts the budget breakdown the user is looking at. This
    mirrors the frontend's liveCosts exactly: breakdown.transport + breakdown.stay."""
    breakdown = (state.get("costs") or {}).get("breakdown") or {}
    return round((breakdown.get("transport") or 0) + (breakdown.get("stay") or 0))


def _find_leg(graph: G.TripGraph, leg_id: str | None,
              from_city: str | None = None, to_city: str | None = None) -> G.TripLeg | None:
    """Resolve a leg by id, falling back to a from→to city match (what the agent
    naturally has in hand when the user says 'the Goa to Indore train')."""
    if leg_id:
        leg = next((e for e in graph.edges if e.id == leg_id), None)
        if leg:
            return leg
    if from_city and to_city:
        f, t = geo.normalize(from_city), geo.normalize(to_city)
        return next(
            (e for e in graph.edges
             if geo.normalize(e.from_city) == f and geo.normalize(e.to_city) == t),
            None,
        )
    return None


def handle_select_leg_option(
    state: dict, leg_id: str | None, mode: str, option_index: int,
    hop_index: int = 0, from_city: str | None = None, to_city: str | None = None,
) -> tuple[list[PatchOp], str]:
    """Pick a specific ride on ANY leg of the graph (the going/return-only picker
    in copilot.py predates multi-city). `option_index` counts within that mode's
    options on the hop — matching both the projected bucket order and the
    `{mode}-{i}` option ids. Writes `leg.selection` (the ONLY persisted choice)
    and re-emits the projections + costs."""
    mode = (mode or "").lower()
    if mode not in ("flight", "train", "bus"):
        return [], "I don't recognise that transport mode."

    graph = G.ensure_graph(state)
    leg = _find_leg(graph, leg_id, from_city, to_city)
    if leg is None:
        return [], "I couldn't find that leg in your trip."

    req = state.get("request") or {}
    budget = req.get("budget_tier") or "mid"
    route = G.active_route(leg, None, budget)
    if route is None or not route.hops:
        return [], f"The {leg.from_city} → {leg.to_city} leg has no routed options yet."
    if not (0 <= hop_index < len(route.hops)):
        return [], "That hop isn't part of this route."

    hop = route.hops[hop_index]
    mode_options = [o for o in hop.options if o.mode == mode]
    if not mode_options:
        # The caller (often the agent) named a mode but the wrong hop — find the
        # hop that actually carries that mode when it's unambiguous.
        candidates = [
            (i, h) for i, h in enumerate(route.hops)
            if any(o.mode == mode for o in h.options)
        ]
        if len(candidates) == 1:
            hop_index, hop = candidates[0]
            mode_options = [o for o in hop.options if o.mode == mode]
        else:
            return [], f"No {mode} options on the {hop.from_city} → {hop.to_city} hop."
    if not (0 <= option_index < len(mode_options)):
        return [], (
            f"That {mode} isn't in the list — pick one of the "
            f"{len(mode_options)} shown."
        )
    picked = mode_options[option_index]

    # Preserve picks on the other hops of the same route; switching routes resets.
    prior = leg.selection
    per_hop: list[str | None] = (
        list(prior.per_hop_option_ids) if prior and prior.route_id == route.id
        else [None] * len(route.hops)
    )
    while len(per_hop) < len(route.hops):
        per_hop.append(None)
    per_hop[hop_index] = picked.id
    leg.selection = G.LegSelection(route_id=route.id, per_hop_option_ids=per_hop)

    patches = _graph_patches(state, graph)
    fare = round(picked.fare_per_person or 0)
    reply = (
        f"Locked in {picked.operator or mode} (₹{fare}/person) for "
        f"{hop.from_city} → {hop.to_city}."
    )
    live = _live_prices_total(state)
    if live:
        reply += f" Rides + stay now ₹{live:,} at live prices."
    return patches, reply


def handle_select_stop_hotel(
    state: dict, stop: str, hotel_index: int,
) -> tuple[list[PatchOp], str]:
    """Pick the stay for one stop of a multi-city trip (`stop` = node id or city).
    Writes `node.stay.selected_id`; stay costs re-derive per stop. When the stop
    is the trip's main destination, the legacy /stay/selected mirror is updated
    too so the current Stays UI stays coherent."""
    graph = G.ensure_graph(state)
    node = graph.node(stop) or graph.node_by_city(stop or "")
    if node is None:
        return [], f"I couldn't find {stop} among your stops."
    results = node.stay.search_results or []
    if not results:
        return [], f"I don't have stay options for {node.city} yet — ask me to find some."
    if not (0 <= hotel_index < len(results)):
        return [], f"That stay isn't in {node.city}'s list — pick one of the {len(results)} shown."

    hotel = results[hotel_index]
    node.stay.selected_id = str(hotel.get("id") or hotel.get("hotel_id") or hotel.get("name"))
    patches = _graph_patches(state, graph)

    req = state.get("request") or {}
    if geo.normalize(node.city) == geo.normalize(req.get("destination") or ""):
        state.setdefault("stay", {})["selected"] = hotel
        patches.append(PatchOp(op="set", path="/stay/selected", value=hotel))

    price = hotel.get("price_per_night") or hotel.get("price")
    nights = max(int(node.nights or 1), 1)
    bits = [f"₹{round(price):,}/night" if isinstance(price, (int, float)) and price > 0 else None,
            f"{nights} night{'s' if nights > 1 else ''}"]
    detail = " · ".join(b for b in bits if b)
    reply = f"Done — {hotel.get('name')} is your stay in {node.city} ({detail})."
    live = _live_prices_total(state)
    if live:
        reply += f" Rides + stay now ₹{live:,} at live prices."
    return patches, reply
