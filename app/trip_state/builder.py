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


def _days_between(a: str | None, b: str | None) -> int | None:
    try:
        da = datetime.strptime((a or "")[:10], "%Y-%m-%d")
        db = datetime.strptime((b or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (db - da).days


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


def _anchor_origin(graph: G.TripGraph, req: dict) -> None:
    """An empty graph must anchor on request.origin BEFORE any edited city gets
    a node — otherwise (e.g. editing against a degraded snapshot that lost its
    transport blocks) the first city the user mentions becomes the 'origin',
    return legs attach to the wrong node, and the whole chain walks backwards."""
    if graph.nodes:
        return
    origin = (req.get("origin") or "").strip()
    if origin:
        _ensure_node(graph, origin, role_hint="origin")


def _resolve_from_node(graph: G.TripGraph, city: str) -> G.TripStop:
    """The node a new leg leaves FROM: the LATEST visit of that city in travel
    order. A city can legitimately appear twice (Udaipur → Jaipur → Udaipur →
    Delhi) — resolving to the first visit would prune the wrong onward leg."""
    key = geo.normalize(city)
    ordered, _, _ = G.ordered_path(graph)
    matches = [n for n in ordered if (n.normalized_city or geo.normalize(n.city)) == key]
    if matches:
        return matches[-1]
    return _ensure_node(graph, city, role_hint="stop")


def _resolve_to_node(graph: G.TripGraph, from_node: G.TripStop, city: str) -> G.TripStop:
    """The node a new leg arrives AT. The origin always merges (a leg home is
    the return); a city with no inbound leg merges (new, or rejoining after its
    old inbound was displaced). A city already reached by another leg becomes a
    NEW visit node — a revisit is two stops, not a cycle: a cycle can't be
    walked, so its legs silently vanish from the projected journey."""
    node = graph.node_by_city(city)
    if node is None:
        return _ensure_node(graph, city, role_hint="stop")
    if node.role == "origin" or node.id == from_node.id:
        return node
    has_inbound = any(
        e.to_node_id == node.id and e.from_node_id != node.id for e in graph.edges
    )
    if not has_inbound:
        return node
    revisit = G.TripStop(
        id=_next_stop_id(graph), city=city.strip(),
        normalized_city=geo.normalize(city), role="stop",
        coordinates=node.coordinates,
    )
    graph.nodes.append(revisit)
    return revisit


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


def _stamp_hop_dates(leg: G.TripLeg) -> None:
    """Date every hop of every route cumulatively: hop N departs when hop N-1's
    ride actually arrives (a 2-day train pushes the connecting bus 2 days out,
    not onto the same calendar day)."""
    for route in leg.routes:
        cursor = leg.date
        for hop in route.hops:
            hop.date = cursor
            best = G.recommended_option(hop)
            if best:
                cursor = _plus_days(cursor, G.option_day_span(best)) or cursor


def _resequence_dates(graph: G.TripGraph, start_date: str | None) -> None:
    """Re-sort nodes into travel order, then walk the path assigning each stop's
    arrival/departure from the previous stop's departure PLUS the leg's real
    travel time (a 39-hour Pune→Manali journey arrives 2 days after it leaves,
    and every later date — stays, onward legs, the return — shifts with it).
    Ordering first is what keeps a mid-trip-inserted stop's dates correct."""
    G.order_nodes_along_path(graph)
    ordered, outbound, inbound = G.ordered_path(graph)
    if not ordered:
        return

    origin = ordered[0]
    origin.arrival_date = start_date
    origin.departure_date = _plus_days(start_date, max(int(origin.nights or 0), 0)) or start_date

    prev = origin
    for leg in outbound:
        leg.date = prev.departure_date
        _stamp_hop_dates(leg)
        dst = graph.node(leg.to_node_id)
        if dst is None:
            continue
        dst.arrival_date = _plus_days(leg.date, G.leg_travel_days(leg)) or leg.date
        dst.departure_date = _plus_days(dst.arrival_date, max(int(dst.nights or 0), 0)) or dst.arrival_date
        prev = dst

    # The return leaves when the traveller is done at its departure city.
    if inbound:
        src = graph.node(inbound.from_node_id)
        if src and src.departure_date:
            inbound.date = src.departure_date
        _stamp_hop_dates(inbound)


# ── Live routing ─────────────────────────────────────────────────────────────────

async def _shaped_journey(from_city: str, to_city: str, date: str | None,
                          travelers: int, budget: str, user_id: str,
                          stay_nights: int = 1, include_hotels: bool = False) -> dict | None:
    """Run the production journey pipeline for one hop and return the shaped block
    (buses/trains/flights buckets), or None on failure/empty. The pipeline always
    fetches destination hotels — `include_hotels` keeps them on the block so the
    caller can attach them to the stop instead of throwing them away."""
    from app.api.v1.plan_trip.journey import run_plan_journey
    from app.api.v1.plan_trip.models import PlanJourneyRequest
    from app.api.v1.plan_trip.plan import _shape_journey

    try:
        req = PlanJourneyRequest(
            origin=from_city, destination=to_city, travelers=travelers,
            budget=budget, departure_date=date, stay_nights=max(int(stay_nights or 1), 1),
        )
        result = await run_plan_journey(req, user_id, profile={})
        block = _shape_journey(result, include_hotels=include_hotels)
        return block
    except Exception as exc:  # noqa: BLE001 — a routing miss is not fatal
        logger.warning("[builder] journey %s→%s failed: %s", from_city, to_city, exc)
        return None


def _attach_stay(node: G.TripStop, hotels: list[dict], nights: int) -> bool:
    """Attach hotel candidates to a stop that has none (or whose list was fetched
    for a different stay length). Returns True when the stay list changed."""
    if not hotels:
        return False
    fetched_for = getattr(node.stay, "fetched_nights", None)
    if node.stay.search_results and fetched_for == nights:
        return False
    keep_selected = node.stay.selected_id if node.stay.search_results else None
    # Cap per stop — 60+ raw hotel payloads per city bloats the snapshot row.
    node.stay = G.Stay(search_results=list(hotels)[:24], selected_id=keep_selected)
    node.stay.fetched_nights = nights  # extra="allow"
    return True


async def _fetch_stop_hotels(node: G.TripStop, travelers: int, budget: str) -> list[dict]:
    """Live hotel search for one stop, dated by the stop's actual arrival and
    stay length. Best-effort — an empty list on failure."""
    from app.api.v1.plan_trip.helpers import rank_hotels
    from app.services.hotels import HotelService

    nights = max(int(node.nights or 1), 1)
    checkin = node.arrival_date
    if not checkin:
        checkin = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    checkout = _plus_days(checkin, nights) or checkin
    try:
        result = await HotelService().search(
            node.city, checkin, checkout,
            adults=max(travelers, 1), budget=budget,
        )
        return rank_hotels(result.get("hotels") or [], budget)
    except Exception as exc:  # noqa: BLE001 — stays are additive, never fatal
        logger.warning("[builder] hotel search for %s failed: %s", node.city, exc)
        return []


def _seed_searched_dates(graph: G.TripGraph) -> None:
    """Legs from before `searched_date` existed (persisted or legacy-derived)
    record their current date as the one their options were searched for — the
    baseline that lets the refresh detect a later shift. Call right after
    loading the graph, BEFORE any resequencing overwrites leg dates."""
    for leg in graph.edges:
        if getattr(leg, "searched_date", None) is None and leg.date:
            leg.searched_date = leg.date  # extra="allow"


def _repoint_return_leg(graph: G.TripGraph) -> str | None:
    """After the chain grows, the return must leave from the trip's REAL
    terminal — adding Chandigarh after Manali moves it to Chandigarh→origin.
    Pure re-point; the stale-leg refresh re-searches its options."""
    ordered, outbound, inbound = G.ordered_path(graph)
    if not inbound or not outbound or len(ordered) < 2:
        return None
    terminal = graph.node(outbound[-1].to_node_id)
    if terminal is None or terminal.role == "origin" or inbound.from_node_id == terminal.id:
        return None
    old_city = inbound.from_city
    inbound.from_node_id = terminal.id
    inbound.from_city = terminal.city
    inbound.date = terminal.departure_date
    inbound.routes = []          # options were for the old city pair
    inbound.selection = None
    return f"moved the return to start from {terminal.city} (it used to leave from {old_city})"


async def _refresh_stale_legs(state: dict, graph: G.TripGraph, user_id: str) -> list[str]:
    """Re-search every leg whose live options no longer match the trip: the date
    shifted since they were fetched (seats found for the 19th say nothing about
    the 21st), or the leg was re-pointed and has no routes at all. Runs after
    `_resequence_dates`; searches run in parallel. Returns human notes about
    what changed."""
    stale = [
        leg for leg in graph.edges
        if leg.date and (
            (not leg.routes and leg.status != "no_route")
            or (leg.routes and getattr(leg, "searched_date", None) != leg.date)
        )
    ]
    if not stale:
        return []

    req = state.get("request") or {}
    travelers = max(int(req.get("travelers") or 1), 1)
    budget = req.get("budget_tier") or "mid"

    blocks = await asyncio.gather(*(
        _shaped_journey(leg.from_city, leg.to_city, leg.date, travelers, budget, user_id)
        for leg in stale
    ))

    notes: list[str] = []
    for leg, block in zip(stale, blocks):
        if not _block_has_options(block):
            if not leg.routes:
                leg.status = "no_route"
            leg.searched_date = leg.date  # don't hammer the same failing search
            notes.append(
                f"couldn't find live {leg.from_city} → {leg.to_city} options for "
                f"{leg.date} — that leg needs attention"
            )
            continue
        leg.routes = [_direct_route(leg.id, block, leg.from_city, leg.to_city, leg.date)]
        leg.selection = None
        leg.status = "routed"
        leg.searched_date = leg.date  # extra="allow"
        _stamp_hop_dates(leg)
        notes.append(f"re-searched {leg.from_city} → {leg.to_city} for {leg.date} (dates shifted)")
    return notes


async def _sync_legs_after_change(state: dict, graph: G.TripGraph, user_id: str) -> list[str]:
    """The one call every structural edit makes after mutating the graph:
    resequence dates (travel-time aware), point the return at the real terminal,
    and re-search any leg whose options went stale. Two rounds because fresh
    options can change a leg's travel days and shift dates downstream once more.
    Returns notes for the reply."""
    req = state.get("request") or {}
    notes: list[str] = []
    for _ in range(2):
        _resequence_dates(graph, req.get("departure_date"))
        note = _repoint_return_leg(graph)
        if note:
            notes.append(note)
        round_notes = await _refresh_stale_legs(state, graph, user_id)
        if not round_notes:
            break
        notes += round_notes
    _resequence_dates(graph, req.get("departure_date"))
    return notes


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

def _sync_request_dates(state: dict, graph: G.TripGraph) -> list[PatchOp]:
    """Mirror the graph's resequenced dates onto `request` — when 2 nights in
    Chandigarh push the return from the 19th to the 21st, return_date/end_date
    (what the header and any re-plan read) must follow."""
    req = state.get("request") or {}
    ordered, outbound, inbound = G.ordered_path(graph)
    if not outbound:
        return []
    terminal = graph.node(outbound[-1].to_node_id)
    new_return = (inbound.date if inbound else None) or (terminal.departure_date if terminal else None)
    if not new_return:
        return []

    changed: dict = {}
    if (inbound or req.get("return_date")) and req.get("return_date") != new_return:
        changed["return_date"] = new_return
    if req.get("end_date") and req.get("end_date") != new_return:
        changed["end_date"] = new_return
    if not changed:
        return []
    new_req = {**req, **changed}
    state["request"] = new_req
    return [PatchOp(op="set", path="/request", value=new_req)]


def _graph_patches(state: dict, graph: G.TripGraph) -> list[PatchOp]:
    """Persist the graph and its regenerated projections. Emits `/graph` plus the
    merged legacy projections so both graph-aware and current clients converge."""
    graph_dump = graph.model_dump(mode="json")
    # Keep the FULL graph in memory so the post-edit sync_trip_to_store can mirror
    # every option's raw payload into the search store, but PERSIST a thinned dump
    # (raw + hotel blobs stripped — they live in the store, served by /options and
    # /results). This is what keeps the stored snapshot skinny at rest.
    state["graph"] = graph_dump
    thin_dump = G.thin_graph_dump(graph_dump)
    proj = G.project(state, graph)
    patches = [PatchOp(op="set", path="/graph", value=thin_dump)]
    patches += _sync_request_dates(state, graph)

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

def _shift_chain_for_date(state: dict, graph: G.TripGraph,
                          src: G.TripStop, new_date: str) -> str | None:
    """Make `src`'s departure land on `new_date` — the lever behind "no seats on
    the 19th, shift to the 21st?". Origin: move the whole trip's start. Any other
    stop: adjust its nights, so the resequence carries the shift to every later
    date (onward legs, the return)."""
    current = src.departure_date
    if src.role == "origin":
        req = dict(state.get("request") or {})
        current = current or req.get("departure_date")
        if current == new_date:
            return None
        req["departure_date"] = new_date
        state["request"] = req
        return f"moved the trip start to {new_date}"
    delta = _days_between(current, new_date)
    if not delta:
        return None
    src.nights = max(int(src.nights or 0) + delta, 0)
    return (
        f"shifted your {src.city} departure to {new_date} "
        f"({src.nights} night{'s' if src.nights != 1 else ''} there now)"
    )


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
    _seed_searched_dates(graph)
    _anchor_origin(graph, req)

    # Idempotency: a leg we already have is not re-added. This is the backstop
    # that stops the agent's re-plan from stacking duplicate legs and looping
    # (the "Added the Goa → Jaipur leg" ×4 symptom). An explicit `via` is a
    # reroute request, and an explicit different `date` is a reschedule — both
    # act on the existing leg rather than no-opping.
    existing = _find_leg(graph, None, from_city, to_city)
    if existing and not via:
        if date and existing.date and date != existing.date:
            _resequence_dates(graph, start_date)   # stamp node dates before shifting
            src = graph.node(existing.from_node_id)
            shift_note = _shift_chain_for_date(state, graph, src, date) if src else None
            if shift_note:
                notes = await _sync_legs_after_change(state, graph, user_id)
                await _geocode_nodes(graph)
                patches = _graph_patches(state, graph)
                patches.append(PatchOp(op="set", path="/request", value=state["request"]))
                detail = f" {'; '.join(notes)}." if notes else ""
                return patches, f"Done — {shift_note}, and re-checked every affected leg.{detail}", []
        fare = round(G.leg_fare_per_person(existing, prefer=mode, budget=budget))
        price = f" (₹{fare}/person)" if fare else ""
        return [], (
            f"The {from_city} → {to_city} leg is already in your trip{price} — "
            "say “via <city>” to reroute it, or pick a specific ride to change it."
        ), []

    from_node = _resolve_from_node(graph, from_city)
    # A leg whose target is the origin city is the return; a leg back to a city
    # already on the path becomes a fresh visit node (never a cycle).
    to_node = _resolve_to_node(graph, from_node, to_city)
    if existing and via:
        graph.edges = [e for e in graph.edges if e.id != existing.id]  # replace on reroute
    _resequence_dates(graph, start_date)
    if date and from_node.departure_date and date != from_node.departure_date:
        # The user named a departure day for this leg — bend the chain to it.
        _shift_chain_for_date(state, graph, from_node, date)
        _resequence_dates(graph, state.get("request", {}).get("departure_date") or start_date)
    leg_id = _next_leg_id(graph)
    leg_date = from_node.departure_date or date or start_date

    is_return = to_node.role == "origin"

    # ── Explicit gateway: build the two-hop route directly. ──
    if via:
        via = via.strip()
        hop1, hop2 = await asyncio.gather(
            _shaped_journey(from_city, via, leg_date, travelers, budget, user_id),
            _shaped_journey(via, to_city, _plus_days(leg_date, 0), travelers, budget, user_id,
                            stay_nights=to_node.nights or 1, include_hotels=not is_return),
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
        leg.searched_date = leg_date  # extra="allow"
        graph.edges.append(leg)
        _prune_displaced(graph, from_node.id, to_node.id)
        if is_return:
            # ONE return: a new leg home replaces any earlier inbound edge.
            graph.edges = [e for e in graph.edges
                           if e.id == leg.id or e.to_node_id != to_node.id]
        stay_note = ""
        if not is_return and (to_node.nights or 0) > 0:
            hop2_hotels = ((hop2.get("hotels") or {}).get("items")) or []
            if _attach_stay(to_node, hop2_hotels, to_node.nights or 1):
                stay_note = f" Pulled {len(to_node.stay.search_results)} live stays for {to_city} too."
        notes = await _sync_legs_after_change(state, graph, user_id)
        await _geocode_nodes(graph)
        patches = _graph_patches(state, graph)
        fare = round(G.leg_fare_per_person(leg, budget=budget))
        detail = f" Also: {'; '.join(notes)}." if notes else ""
        return patches, (
            f"Routed {from_city} → {to_city} via {via} — {from_city}→{via} then "
            f"{via}→{to_city}, ₹{fare}/person. Added it as the {from_city}→{to_city} "
            f"leg.{stay_note}{detail}"
        ), []

    # ── Direct routing + gateway discovery, in parallel. ──
    from app.services.geo_nearby import find_gateways

    direct_block, gateways = await asyncio.gather(
        _shaped_journey(from_city, to_city, leg_date, travelers, budget, user_id,
                        stay_nights=to_node.nights or 1, include_hotels=not is_return),
        find_gateways(from_city, to_city, leg_date or "", travelers=travelers),
    )

    if _block_has_options(direct_block):
        route = _direct_route(leg_id, direct_block, from_city, to_city, leg_date)
        leg = G.TripLeg(
            id=leg_id, from_node_id=from_node.id, to_node_id=to_node.id,
            from_city=from_city, to_city=to_city, date=leg_date,
            routes=[route], status="routed",
        )
        leg.searched_date = leg_date  # extra="allow"
        graph.edges.append(leg)
        _prune_displaced(graph, from_node.id, to_node.id)
        if is_return:
            # ONE return: a new leg home replaces any earlier inbound edge.
            graph.edges = [e for e in graph.edges
                           if e.id == leg.id or e.to_node_id != to_node.id]
        stay_note = ""
        # Stays attach only to stops the traveller SLEEPS in (nights known) —
        # a transit city ("Delhi without stay") must not fill the Stays tab.
        if not is_return and (to_node.nights or 0) > 0:
            hotel_items = ((direct_block.get("hotels") or {}).get("items")) or []
            if _attach_stay(to_node, hotel_items, to_node.nights or 1):
                stay_note = f" Pulled {len(to_node.stay.search_results)} live stays for {to_city} too."
        notes = await _sync_legs_after_change(state, graph, user_id)
        await _geocode_nodes(graph)
        patches = _graph_patches(state, graph)

        best = G.recommended_option(route.hops[0], prefer=mode, budget=budget)
        fare = round(G.leg_fare_per_person(leg, prefer=mode, budget=budget))
        picked = f"{best.operator} ({best.mode})" if best else "the best option"
        reply = f"Added the {from_city} → {to_city} leg — {picked}, ₹{fare}/person.{stay_note}"
        if mode and not any(
            o.mode == mode and G.option_is_available(o.mode, o.raw or {})
            for h in route.hops for o in h.options
        ):
            reply += (
                f" One thing: no bookable {mode} seats came back for {leg_date} — "
                f"I've used the best alternative. Say “shift to <date>” if you'd "
                f"rather take a {mode} on another day."
            )
        if notes:
            reply += f" Also: {'; '.join(notes)}."
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

    # ── Nothing on this date, anywhere: probe the next two days before giving
    #    up — sold out today often just means "go tomorrow". ──
    if leg_date:
        alt_dates = [d for d in (_plus_days(leg_date, 1), _plus_days(leg_date, 2)) if d]
        alt_blocks = await asyncio.gather(*(
            _shaped_journey(from_city, to_city, d, travelers, budget, user_id)
            for d in alt_dates
        ))
        for alt, blk in zip(alt_dates, alt_blocks):
            if _block_has_options(blk):
                return [], (
                    f"Nothing bookable {from_city} → {to_city} on {leg_date} — but "
                    f"there ARE seats on {alt}. Want me to shift that leg to {alt}? "
                    "Every later date (stays, onward legs, the return) adjusts with it."
                ), [f"Shift to {alt}", "Keep the date, I'll decide later"]

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
    _seed_searched_dates(graph)
    _anchor_origin(graph, req)
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
        _attach_stay(node, list(hotels), n_days)

    # New nights shift every later date — resequence, re-point/re-search the
    # return, and refresh any leg whose options went stale, BEFORE fetching
    # stays so the hotel search uses the stop's real check-in.
    notes = await _sync_legs_after_change(state, graph, user_id)

    stay_note = ""
    if not (node.stay.search_results and getattr(node.stay, "fetched_nights", None) == n_days):
        stop_hotels = await _fetch_stop_hotels(node, travelers, budget)
        if _attach_stay(node, stop_hotels, n_days):
            checkin = node.arrival_date or "your arrival"
            stay_note = (
                f" Found {len(node.stay.search_results)} live stays in {city} for "
                f"{n_days} night{'s' if n_days > 1 else ''} from {checkin} — "
                "they're on the stop, tell me which to lock in."
            )

    await _geocode_nodes(graph)
    patches = _graph_patches(state, graph)

    spots = [a.get("title") for d in day_list for a in (d.get("activities") or [])][:3]
    tail = f" — {', '.join(s for s in spots if s)}" if any(spots) else ""
    detail = f" Also: {'; '.join(notes)}." if notes else ""
    when = f" ({node.arrival_date} → {node.departure_date})" if node.arrival_date else ""
    return patches, (
        f"Added a {n_days}-day itinerary for {city}{when}{tail}. It's slotted into "
        f"your trip in order.{stay_note}{detail}"
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
