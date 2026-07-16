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

# Per-process cap on concurrent journey searches. One journey fans out to every
# transport provider at once (each with its own breaker/concurrency cap) — this
# keeps a multi-stop composition or a burst of edits from running unbounded
# numbers of those fan-outs simultaneously. Loop-scoped like generation_queue
# (Celery runs tasks under fresh asyncio.run loops).
_JOURNEY_FANOUT = 3
_journey_sem: tuple[int, asyncio.Semaphore] | None = None


def _journey_semaphore() -> asyncio.Semaphore:
    global _journey_sem
    loop_id = id(asyncio.get_running_loop())
    if _journey_sem is None or _journey_sem[0] != loop_id:
        _journey_sem = (loop_id, asyncio.Semaphore(_JOURNEY_FANOUT))
    return _journey_sem[1]


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
        async with _journey_semaphore():
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
    """True when a shaped journey block carries ANY bookable option. Multi-hop
    same-mode routes (train → train via Delhi — every bhiwani return) nest their
    options under `bucket.legs[].items` with an EMPTY top-level `items`, so the
    flat check alone silently discarded successful searches — the root cause of
    the empty-return-edge bug."""
    if not block:
        return False
    for key in ("flights", "trains", "buses"):
        bucket = block.get(key) or {}
        if bucket.get("items"):
            return True
        if any(isinstance(leg, dict) and leg.get("items") for leg in bucket.get("legs") or []):
            return True
    return False


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

