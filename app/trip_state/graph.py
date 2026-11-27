"""
Trip graph — the canonical trip domain model.

A trip is a directed graph: nodes are stops (cities you spend time in, plus the
origin and terminal), edges are legs (how you get from one node to the next).
A plain one-destination trip is just a 2-node graph — this is not a mode or a
flag, it is THE model. See docs/trip_graph.md.

Two hard rules this module enforces:

  1. Recommendation is DERIVED, never persisted. Edges store only the underlying
     search results (`RouteHop.options`) and the user's explicit `selection`.
     `rank_routes` / `rank_options` compute the suggestion every time it's asked
     for. Nothing here writes an `is_recommended` flag into the canonical graph.

  2. The legacy `transport.going`/`return_journey`/`onward_segments` blocks and
     the flat `itinerary.days` are COMPATIBILITY PROJECTIONS regenerated from the
     graph by `project()`. The graph is the source of truth; those are a mirror
     for the current frontend until it reads the graph directly.

Everything here is pure (state in → values out); persistence + patch emission
live in the copilot/builder handlers.
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.services import geo

logger = logging.getLogger(__name__)

# Budget tier → the axis the ranking engine optimises for when the user has NOT
# made an explicit pick. Mirrors budget_to_sort() in services/flights.py.
_BUDGET_PREFER = {"low": "cheapest", "mid": "balanced", "high": "fastest"}

_MODES = ("flight", "train", "bus")

# Cap stored options per mode per hop. Providers return 40+ items each; storing
# every raw payload across a 6-leg trip makes the snapshot row enormous — big
# enough that persisting it through a flaky TLS window keeps failing. 15 keeps
# every option the UI/agent actually offers while bounding the row size.
MAX_OPTIONS_PER_MODE = 15


# ── Domain objects ──────────────────────────────────────────────────────────────
# All permissive (extra="allow") so provider-shaped fields ride along untouched;
# we only name the fields the ranking/projection/cost code actually reads.

class TransportOption(BaseModel):
    """One bookable ride on one hop, normalized across modes. `raw` keeps the
    provider-shaped original verbatim so the projection can hand the frontend the
    exact item shape it already renders."""
    model_config = ConfigDict(extra="allow")

    id: str
    mode: str                                   # flight | train | bus
    operator: Optional[str] = None
    departure: Optional[str] = None
    arrival: Optional[str] = None
    duration_min: Optional[int] = None
    fare_per_person: float = 0
    currency: str = "INR"
    # Seat availability, denormalized at build time from the provider item so the
    # sold-out check no longer needs `raw` — the first step to dropping `raw` from
    # the stored graph. Older options (pre-denorm) leave it None and _sold_out
    # falls back to computing it from `raw`.
    available: Optional[bool] = None
    # Permanent search-store reference (search_results.id) — the v4 typed link.
    # Stamped by the store-first write at generation time; the read path
    # hydrates `raw` back by this id (exact, no fuzzy matching).
    result_id: Optional[str] = None
    raw: dict = Field(default_factory=dict)


class RouteHop(BaseModel):
    """A single physical hop (from_city → to_city) with every live option found
    for it, across all modes. A direct route has one hop; a gateway route (C→D→A)
    has two."""
    model_config = ConfigDict(extra="allow")

    from_city: str
    to_city: str
    date: Optional[str] = None
    options: list[TransportOption] = Field(default_factory=list)


class RouteCandidate(BaseModel):
    """One way to make a leg: `direct` (one hop) or `via_gateway` (two hops
    through a city you don't stay in). Both direct and gateway candidates coexist
    on the same edge so the ranking engine can compare them."""
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str = "direct"                        # direct | via_gateway
    gateway_city: Optional[str] = None
    hops: list[RouteHop] = Field(default_factory=list)


class LegSelection(BaseModel):
    """The user's explicit choice on a leg — the only booking decision persisted.
    One option id per hop of the chosen route."""
    route_id: str
    per_hop_option_ids: list[Optional[str]] = Field(default_factory=list)


class TripLeg(BaseModel):
    """Edge: how you move between two nodes. Stores search results + selection,
    never a recommendation."""
    model_config = ConfigDict(extra="allow")

    id: str
    from_node_id: str
    to_node_id: str
    from_city: str
    to_city: str
    date: Optional[str] = None
    routes: list[RouteCandidate] = Field(default_factory=list)
    selection: Optional[LegSelection] = None
    status: str = "routed"                      # routed | no_route
    bookings: list = Field(default_factory=list)   # reserved
    expenses: list = Field(default_factory=list)   # reserved


class Stay(BaseModel):
    """Underlying hotel search + the selected hotel id. Recommended stay is
    derived, mirroring how transport recommendation works."""
    model_config = ConfigDict(extra="allow")

    search_results: list[dict] = Field(default_factory=list)
    selected_id: Optional[str] = None


class TripStop(BaseModel):
    """Node: a city you spend time in (or the origin / terminal). Owns its days
    and its stay, and reserves ownership of bookings/documents/expenses."""
    model_config = ConfigDict(extra="allow")

    id: str
    city: str
    normalized_city: Optional[str] = None
    role: str = "stop"                          # origin | stop | terminal
    coordinates: Optional[dict] = None          # {lat, lng}, geocoded + cached
    arrival_date: Optional[str] = None
    departure_date: Optional[str] = None
    nights: int = 0
    days: list[dict] = Field(default_factory=list)   # canonical Stop → Day → Activity
    stay: Stay = Field(default_factory=Stay)
    bookings: list = Field(default_factory=list)     # reserved
    documents: list = Field(default_factory=list)    # reserved
    expenses: list = Field(default_factory=list)     # reserved


class TripGraph(BaseModel):
    model_config = ConfigDict(extra="allow")

    nodes: list[TripStop] = Field(default_factory=list)
    edges: list[TripLeg] = Field(default_factory=list)

    # ── node/edge lookup helpers ──
    def node(self, node_id: str) -> Optional[TripStop]:
        return next((n for n in self.nodes if n.id == node_id), None)

    def node_by_city(self, city: str) -> Optional[TripStop]:
        key = geo.normalize(city)
        return next((n for n in self.nodes if (n.normalized_city or geo.normalize(n.city)) == key), None)

    def journey_summary(self) -> str:
        return " → ".join(n.city for n in self.nodes)


# ── Fare / field extraction (mirrors copilot._fare_per_person & _option_*) ───────

def _fare(mode: str, item: dict) -> float:
    if mode == "train":
        return item.get("cheapest_fare") or 0
    if mode == "bus":
        return item.get("fare_discounted") or item.get("fare_original") or 0
    return item.get("price_total") or 0          # flight


def _name(mode: str, item: dict) -> str:
    return (
        item.get("train_name") or item.get("operator") or item.get("airline")
        or item.get("name") or mode
    )


def option_from_item(mode: str, item: dict, oid: str) -> TransportOption:
    """Normalize a provider-shaped transport item into a TransportOption, keeping
    the original under `raw` so the projection can re-emit it unchanged — minus
    `is_recommended`: recommendation is derived (or user selection), NEVER data
    the canonical graph stores."""
    raw = {k: v for k, v in item.items() if k != "is_recommended"}
    return TransportOption(
        id=oid,
        mode=mode,
        operator=_name(mode, item),
        departure=item.get("departure_time") or item.get("departure"),
        arrival=item.get("arrival_time") or item.get("arrival"),
        duration_min=item.get("duration_min"),
        fare_per_person=_fare(mode, item),
        available=option_is_available(mode, item),
        raw=raw,
    )


def hop_from_journey_block(block: dict, from_city: str, to_city: str, date: str | None) -> RouteHop:
    """Build a RouteHop from a legacy/shaped journey block (the output of
    `_shape_journey` / `run_plan_journey`), pulling every mode's items into one
    option pool. This is the bridge the builder uses to turn a live search into
    graph data."""
    options: list[TransportOption] = []
    for mode in _MODES:
        bucket = (block.get({"flight": "flights", "train": "trains", "bus": "buses"}[mode]) or {})
        items = (bucket.get("items") or [])[:MAX_OPTIONS_PER_MODE]
        for i, it in enumerate(items):
            options.append(option_from_item(mode, it, f"{mode}-{i}"))
    return RouteHop(from_city=from_city, to_city=to_city, date=date, options=options)


# ── Availability (seats) ─────────────────────────────────────────────────────────

def option_is_available(mode: str, item: dict) -> bool:
    """Whether an option still has bookable seats, judged from provider fields
    (trains: available_classes; buses/flights: seat counts). Unknown data means
    available — an option is only demoted on positive evidence it's full."""
    item = item or {}
    if mode == "train":
        saw_info = False
        for c in item.get("available_classes") or []:
            seats = c.get("available_seats")
            if isinstance(seats, (int, float)):
                saw_info = True
                if seats > 0:
                    return True
            avail = str(c.get("availability") or c.get("display") or "").upper()
            if avail:
                saw_info = True
                if avail.startswith(("AVAILABLE", "AVL")):
                    return True
        # Classes that carry NO availability info (just fares) say nothing about
        # seats — only an all-WL/REGRET train is treated as full.
        return not saw_info
    seats = item.get("available_seats", item.get("seats_available"))
    if isinstance(seats, (int, float)):
        return seats > 0
    return True


_HEAVY_HOTEL_KEYS = ("photos", "amenities", "description", "rooms", "reviews",
                     "raw", "room_options")


def thin_graph_dump(dump: dict) -> dict:
    """A copy of a serialized graph with the heavy provider payloads removed:
    each transport option's `raw` and each hotel's photo/amenity blobs. Those live
    in the search store (populated by sync_trip_to_store) and are served by /options
    + /results, so the persisted snapshot keeps only the thin fields that cost,
    ranking, and the (skinny) projection need. The in-memory graph stays fat for the
    duration of an edit so sync can still mirror the raw payloads to the store."""
    d = deepcopy(dump)
    for e in d.get("edges") or []:
        for r in e.get("routes") or []:
            for hop in r.get("hops") or []:
                for o in hop.get("options") or []:
                    if isinstance(o, dict):
                        o.pop("raw", None)
    for n in d.get("nodes") or []:
        for hotel in ((n.get("stay") or {}).get("search_results")) or []:
            if isinstance(hotel, dict):
                for k in _HEAVY_HOTEL_KEYS:
                    hotel.pop(k, None)
    return d


def _sold_out(o: TransportOption) -> int:
    avail = o.available
    if avail is None:  # pre-denorm option — fall back to the provider payload
        avail = option_is_available(o.mode, o.raw or {})
    return 0 if avail else 1


# ── Travel-time arithmetic ───────────────────────────────────────────────────────

def _parse_dt(s: str | None) -> Optional[datetime]:
    """Full datetime when the string carries a date ('2026-07-19 20:35', ISO)."""
    if not isinstance(s, str) or len(s) < 10 or s[4:5] != "-":
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T")[:19])
    except ValueError:
        return None


def _parse_clock(s: str | None) -> Optional[int]:
    """Minutes-since-midnight from a time-of-day string ('20:35', '…T20:35')."""
    if not isinstance(s, str):
        return None
    tail = s.split(" ")[-1].split("T")[-1]
    parts = tail.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1][:2])
    except ValueError:
        return None


def shift_date(date_str: str | None, n: int) -> str | None:
    if not date_str:
        return None
    try:
        return (datetime.strptime(date_str[:10], "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def option_day_span(o: TransportOption) -> int:
    """Calendar days this ride crosses (0 = same-day, 1 = overnight, 2 = a two-
    day train). Prefers real departure/arrival dates; falls back to departure
    clock + duration to count midnight crossings."""
    dep, arr = _parse_dt(o.departure), _parse_dt(o.arrival)
    if dep and arr and arr >= dep:
        return (arr.date() - dep.date()).days
    mins = int(o.duration_min or 0)
    if mins <= 0:
        return 0
    start = _parse_clock(o.departure)
    if start is not None:
        return (start + mins) // 1440
    return mins // 1440


def leg_travel_days(leg: TripLeg, prefer: str | None = None, budget: str | None = None) -> int:
    """Days between departing on this leg and arriving at its destination, using
    the selected/recommended ride per hop. This is what lets the date cascade
    place a two-day journey's arrival two days later instead of same-day."""
    route = active_route(leg, prefer, budget)
    if not route:
        return 0
    total = 0
    for i in range(len(route.hops)):
        opt = selected_option_for_hop(leg, route, i, prefer, budget)
        if opt:
            span = option_day_span(opt)
            sel = leg.selection
            user_picked = bool(
                sel and sel.route_id == route.id and i < len(sel.per_hop_option_ids)
            )
            if span > 0 and not user_picked:
                # An overnight *default* silently stretches the whole trip past
                # the requested day count. When the hop has any same-day
                # arrival, anchor the date cascade on it; picking the overnight
                # ride explicitly re-sequences from the traveller's own choice.
                hop = route.hops[i]
                if any(option_day_span(o) == 0 for o in (hop.options or [])):
                    span = 0
            total += span
    return total


# ── Ranking engine — DERIVED recommendation (never persisted) ────────────────────

def _prefer_for(prefer: str | None, budget: str | None) -> str:
    if prefer in ("cheapest", "fastest", "balanced", "earliest"):
        return prefer
    return _BUDGET_PREFER.get((budget or "mid").lower(), "balanced")


def _mode_rank(mode: str) -> int:
    # Tie-breaker when metrics are equal: flights feel like the premium default.
    return {"flight": 0, "train": 1, "bus": 2}.get(mode, 3)


def _dt_with_date(s: str | None, fallback_date: str | None) -> Optional[datetime]:
    """Full datetime, or clock time on the fallback date (transport searches
    return '12:00' clock-only strings on dated hops)."""
    dt = _parse_dt(s)
    if dt is not None:
        return dt
    if fallback_date and len(fallback_date) >= 10:
        clock = _parse_clock(s)
        if clock is not None:
            try:
                return datetime.fromisoformat(fallback_date[:10]) + timedelta(minutes=clock)
            except ValueError:
                return None
    return None


def rank_options(options: list[TransportOption], prefer: str | None = None,
                 budget: str | None = None, arrive_by: Optional[datetime] = None,
                 hop_date: str | None = None) -> list[TransportOption]:
    """Order the options on a hop by the chosen axis. Pure — computes the
    suggestion, stores nothing. Options with no bookable seats left sort after
    every available one regardless of axis — a sold-out ride is never the
    recommendation."""
    axis = _prefer_for(prefer, budget)

    # Same-day anchor, aligned with leg_travel_days: when a hop has any
    # same-day arrival, the recommendation must not silently be an overnight
    # ride that lands the morning AFTER the itinerary's day 1 (cheapest-first
    # did exactly that). An explicit user pick still wins via leg.selection —
    # this only orders the derived recommendation.
    def _overnight(o: TransportOption) -> int:
        if not any(option_day_span(x) == 0 for x in options):
            return 0
        return 0 if option_day_span(o) == 0 else 1

    def _window(o: TransportOption) -> tuple:
        # With arrive_by (the trip's end clock on its last day, for the return
        # leg): anything landing after the deadline sorts last, and among the
        # in-window options the LATEST departure wins, so the last day stays
        # usable instead of being sacrificed to a dawn train that's merely
        # cheapest. Options with unverifiable times are never preferred.
        if arrive_by is None:
            return (0, 0)
        on_date = hop_date or arrive_by.date().isoformat()
        arr = _dt_with_date(o.arrival, on_date)
        after = 1 if (arr is None or arr > arrive_by) else 0
        dep = _dt_with_date(o.departure, on_date)
        dep_min = (dep.timestamp() / 60) if dep else 0
        return (after, -dep_min)

    def cost_key(o: TransportOption) -> tuple:
        return (_sold_out(o), _overnight(o), *_window(o), o.fare_per_person or 1e12, o.duration_min or 1e12, _mode_rank(o.mode))

    def time_key(o: TransportOption) -> tuple:
        return (_sold_out(o), _overnight(o), *_window(o), o.duration_min or 1e12, o.fare_per_person or 1e12, _mode_rank(o.mode))

    def bal_key(o: TransportOption) -> tuple:
        # Balanced: normalise fare & duration into a blended score.
        fares = [x.fare_per_person for x in options if x.fare_per_person] or [1]
        durs = [x.duration_min for x in options if x.duration_min] or [1]
        f = (o.fare_per_person or max(fares)) / max(fares)
        d = (o.duration_min or max(durs)) / max(durs)
        return (_sold_out(o), _overnight(o), *_window(o), 0.6 * f + 0.4 * d, _mode_rank(o.mode))

    key = {"cheapest": cost_key, "fastest": time_key, "earliest": time_key}.get(axis, bal_key)
    return sorted(options, key=key)


def recommended_option(hop: RouteHop, prefer: str | None = None,
                       budget: str | None = None,
                       arrive_by: Optional[datetime] = None) -> Optional[TransportOption]:
    ranked = rank_options(hop.options, prefer, budget, arrive_by=arrive_by,
                          hop_date=hop.date)
    return ranked[0] if ranked else None


def _route_metrics(route: RouteCandidate, prefer: str | None, budget: str | None,
                   arrive_by: Optional[datetime] = None) -> tuple[float, int]:
    """(total_fare_per_person, total_duration_min) using the recommended option on
    each hop — the cost of taking this candidate at the chosen preference."""
    total_fare = 0.0
    total_dur = 0
    for hop in route.hops:
        best = recommended_option(hop, prefer, budget, arrive_by=arrive_by)
        if not best:
            return (1e12, 1_000_000)             # a hop with no option → infeasible
        total_fare += best.fare_per_person or 0
        total_dur += best.duration_min or 0
    return (total_fare, total_dur)


def rank_routes(leg: TripLeg, prefer: str | None = None,
                budget: str | None = None,
                arrive_by: Optional[datetime] = None) -> list[RouteCandidate]:
    """Order a leg's candidate routes (direct + gateway) by the chosen axis, so a
    cheaper/faster multi-leg route can be surfaced even when a direct one exists."""
    axis = _prefer_for(prefer, budget)

    def key(route: RouteCandidate) -> tuple:
        fare, dur = _route_metrics(route, prefer, budget, arrive_by)
        direct_first = 0 if route.kind == "direct" else 1
        if axis == "cheapest":
            return (fare, dur, direct_first)
        if axis in ("fastest", "earliest"):
            return (dur, fare, direct_first)
        # balanced: prefer direct unless a gateway route is materially better —
        # handled by the caller comparing metrics; here just sort by blended cost.
        return (fare + dur * 5, direct_first)     # ~₹5/min time value

    return sorted(leg.routes, key=key)


def selected_option_for_hop(leg: TripLeg, route: RouteCandidate, hop_index: int,
                            prefer: str | None, budget: str | None,
                            arrive_by: Optional[datetime] = None) -> Optional[TransportOption]:
    """The user's picked option for this hop if they chose one on this route,
    else the derived recommendation."""
    hop = route.hops[hop_index] if hop_index < len(route.hops) else None
    if not hop:
        return None
    sel = leg.selection
    if sel and sel.route_id == route.id and hop_index < len(sel.per_hop_option_ids):
        oid = sel.per_hop_option_ids[hop_index]
        picked = next((o for o in hop.options if o.id == oid), None)
        if picked:
            return picked
    return recommended_option(hop, prefer, budget, arrive_by=arrive_by)


def active_route(leg: TripLeg, prefer: str | None, budget: str | None,
                 arrive_by: Optional[datetime] = None) -> Optional[RouteCandidate]:
    """The route in force: the selected one, else the top-ranked derived pick."""
    if leg.selection:
        chosen = next((r for r in leg.routes if r.id == leg.selection.route_id), None)
        if chosen:
            return chosen
    ranked = rank_routes(leg, prefer, budget, arrive_by)
    return ranked[0] if ranked else None


# ── Cost from graph (derived) ────────────────────────────────────────────────────

def leg_fare_per_person(leg: TripLeg, prefer: str | None = None,
                        budget: str | None = None) -> float:
    route = active_route(leg, prefer, budget)
    if not route:
        return 0
    total = 0.0
    for i in range(len(route.hops)):
        opt = selected_option_for_hop(leg, route, i, prefer, budget)
        total += (opt.fare_per_person if opt else 0) or 0
    return total


def stay_price_for_stop(stop: TripStop) -> float:
    """Selected stay's nightly price, else the cheapest search result — derived."""
    results = stop.stay.search_results or []
    chosen = None
    if stop.stay.selected_id:
        chosen = next(
            (h for h in results if str(h.get("id") or h.get("hotel_id") or h.get("name")) == stop.stay.selected_id),
            None,
        )
    if chosen is None and results:
        chosen = min(
            results,
            key=lambda h: (h.get("price_per_night") or h.get("price") or 1e12),
        )
    if not chosen:
        return 0
    price = chosen.get("price_per_night") or chosen.get("price") or 0
    return price if isinstance(price, (int, float)) else 0


def graph_costs(graph: TripGraph, travelers: int, budget: str | None = None,
                base: dict | None = None) -> dict:
    """Rebuild the costs block from the graph: transport = sum of every leg's
    fare × travelers; stay = sum of each stop's nightly price × its nights. Food/
    activities are carried over from `base` if present (estimated elsewhere)."""
    travelers = max(int(travelers or 1), 1)
    costs = dict(base or {})
    breakdown = dict(costs.get("breakdown") or {})

    per_person = sum(leg_fare_per_person(leg, budget=budget) for leg in graph.edges)
    stay_total = sum(stay_price_for_stop(n) * max(int(n.nights or 0), 0) for n in graph.nodes)

    breakdown["transport"] = round(per_person * travelers)
    breakdown["stay"] = round(stay_total)
    breakdown["misc"] = round((breakdown.get("transport", 0) + breakdown.get("stay", 0)) * 0.10)
    total = sum(breakdown.get(k, 0) for k in ("transport", "stay", "food", "activities", "misc"))

    costs.update({
        "breakdown": breakdown,
        "total": total,
        "per_person": total // travelers,
    })
    return costs


# ── Legacy → graph derivation (lazy migration) ───────────────────────────────────

def _stop_id(i: int) -> str:
    return f"stop-{i}"


def _leg_id(i: int) -> str:
    return f"leg-{i}"


def _node_from_city(i: int, city: str, role: str) -> TripStop:
    return TripStop(id=_stop_id(i), city=city, normalized_city=geo.normalize(city), role=role)


_BUCKET_KEY = {"flight": "flights", "train": "trains", "bus": "buses"}


def _normalize_mode(raw: str | None) -> str | None:
    m = (raw or "").lower()
    if "flight" in m or "plane" in m or "air" in m:
        return "flight"
    if "train" in m or "rail" in m:
        return "train"
    if "bus" in m:
        return "bus"
    return None


def _leg_chain_hops(block: dict, from_city: str, to_city: str,
                    date: str | None) -> tuple[list[RouteHop], list[Optional[str]]]:
    """Split a legacy journey block into one RouteHop PER BOOKABLE LEG of its
    `legs` chain — the only faithful mapping when a route has two legs of the
    SAME mode (Bengaluru→Ahmedabad train + Ahmedabad→Udaipur train), which the
    mode-keyed bucket shape cannot represent (its top-level items collapse to
    empty and the real options hide in legs[i].live_data / bucket.legs).

    Options for each hop come from, in order:
      1. that leg's own `live_data.<bucket>` (the planner's per-leg results),
      2. the mode bucket's `legs` segments in chain order (the
         _collect_*_items multi-segment shape),
      3. the top-level bucket items (single bookable leg — classic shape).

    Returns (hops, picked_option_id_per_hop) — the legacy `is_recommended` flag
    on each hop's items, captured here because option_from_item strips it.
    """
    legs_meta = block.get("legs") or []
    bookable = [(m, l) for m, l in
                ((_normalize_mode(l.get("mode")), l) for l in legs_meta) if m]

    def build_hop(mode_items: list[tuple[str, dict]], f: str, t: str) -> tuple[RouteHop, Optional[str]]:
        options: list[TransportOption] = []
        picked: Optional[str] = None
        per_mode_idx: dict[str, int] = {}
        for mode, it in mode_items:
            i = per_mode_idx.get(mode, 0)
            if i >= MAX_OPTIONS_PER_MODE and not it.get("is_recommended"):
                continue  # bound the stored row; a user pick always survives
            per_mode_idx[mode] = i + 1
            oid = f"{mode}-{i}"
            options.append(option_from_item(mode, it, oid))
            if it.get("is_recommended") and picked is None:
                picked = oid
        return RouteHop(from_city=f, to_city=t, date=date, options=options), picked

    if not bookable:
        items = [(m, it) for m in _MODES
                 for it in ((block.get(_BUCKET_KEY[m]) or {}).get("items") or [])]
        hop, picked = build_hop(items, from_city, to_city)
        return [hop], [picked]

    # Track consumption of bucket segments so two same-mode legs each get theirs.
    seg_cursor: dict[str, int] = {}

    def items_for(mode: str, leg: dict) -> list[dict]:
        live = leg.get("live_data") or {}
        if _BUCKET_KEY[mode] in live:
            # Present-but-EMPTY live data is authoritative: the planner searched
            # this exact segment and found nothing. Falling through to the flat
            # bucket here handed a hop its SIBLING's options (Bhiwani→Ambala
            # showing Ambala→Chandigarh trains) — an honest empty hop instead
            # lets the gateway fallback reroute the leg.
            return list(live.get(_BUCKET_KEY[mode]) or [])
        bucket = block.get(_BUCKET_KEY[mode]) or {}
        segments = bucket.get("legs") or []
        if segments:
            k = seg_cursor.get(mode, 0)
            if k < len(segments):
                seg_cursor[mode] = k + 1
                return list(segments[k].get("items") or [])
            return []
        return list(bucket.get("items") or [])

    hops: list[RouteHop] = []
    picks: list[Optional[str]] = []
    for mode, leg in bookable:
        hop, picked = build_hop(
            [(mode, it) for it in items_for(mode, leg)],
            leg.get("from") or from_city,
            leg.get("to") or to_city,
        )
        hops.append(hop)
        picks.append(picked)

    # If NO hop found options anywhere, fall back to the pooled single hop so a
    # buckets-only block (no per-leg data) still routes.
    if not any(h.options for h in hops):
        items = [(m, it) for m in _MODES
                 for it in ((block.get(_BUCKET_KEY[m]) or {}).get("items") or [])]
        hop, picked = build_hop(items, from_city, to_city)
        return [hop], [picked]
    return hops, picks


def _direct_route_from_block(rid: str, block: dict, from_city: str, to_city: str,
                             date: str | None) -> tuple[RouteCandidate, LegSelection | None]:
    """Legacy block → (route, carried-over selection). The legacy is_recommended
    flags become the leg's selection — USER-CHOICE data converted once at derive
    time; the canonical graph still never stores a recommendation."""
    block = block or {}
    hops, picks = _leg_chain_hops(block, from_city, to_city, date)
    route = RouteCandidate(id=rid, kind="direct", hops=hops)
    # Preserve the original leg chain (incl. auto/cab transfers and tips) so the
    # projection can re-emit it — minus the bulky per-leg live_data, which now
    # lives once as the hops' options instead of being stored twice.
    if block.get("legs"):
        route.legs_meta = [  # extra="allow"
            {k: v for k, v in l.items() if k not in ("live_data", "train_alternative")}
            for l in block["legs"]
        ]
    selection = (
        LegSelection(route_id=rid, per_hop_option_ids=picks)
        if any(picks) else None
    )
    return route, selection


def derive_graph_from_legacy(state: dict) -> TripGraph:
    """Build the canonical graph from a legacy single-destination/waypoint state.
    Best-effort: maps origin → …waypoints… → destination into nodes, and
    going/onward_segments/return_journey into edges carrying the raw mode items.
    Runs once, lazily, the first time a trip is loaded without a graph."""
    req = state.get("request") or {}
    transport = state.get("transport") or {}
    origin = req.get("origin")
    destination = req.get("destination")

    graph = TripGraph()

    # Only derive from legacy when there is REAL routing data to migrate. A fresh
    # builder trip (request.origin/destination set but no journey blocks yet) must
    # start as an empty graph so the builder can lay down its own origin node from
    # the first leg — otherwise we'd fabricate a phantom origin→destination edge.
    has_legacy = bool(
        transport.get("going") or transport.get("return_journey")
        or transport.get("onward_segments") or transport.get("outbound_chain")
    )
    if not has_legacy:
        return graph

    chain = transport.get("outbound_chain") or []
    if not chain:
        waypoints = transport.get("waypoints") or req.get("waypoints") or []
        chain = [c for c in [origin, *waypoints, destination] if c]
    if not chain:
        return graph

    # ── Nodes (outbound cities). Return does not add a node unless it's a new city.
    for i, city in enumerate(chain):
        role = "origin" if i == 0 else ("terminal" if i == len(chain) - 1 else "stop")
        graph.nodes.append(_node_from_city(i, city, role))

    # Attach the flat itinerary to the destination node (best-effort; the builder
    # owns proper per-stop day splitting going forward).
    days = (state.get("itinerary") or {}).get("days") or []
    if days and len(graph.nodes) >= 2:
        graph.nodes[-1].days = list(days)
        graph.nodes[-1].nights = max(int(req.get("stay_nights") or 0), 0)

    # Attach hotels to the destination node's stay.
    hotels = (state.get("stay") or {}).get("hotels") or []
    if hotels and graph.nodes:
        sel = (state.get("stay") or {}).get("selected") or {}
        graph.nodes[-1].stay = Stay(
            search_results=list(hotels),
            selected_id=str(sel.get("id") or sel.get("hotel_id") or sel.get("name")) if sel else None,
        )

    # ── Outbound edges: going = node0→node1; onward_segments fill the rest.
    dep = req.get("departure_date")
    outbound_blocks = [transport.get("going") or {}]
    outbound_blocks += list(transport.get("onward_segments") or [])
    for i in range(len(graph.nodes) - 1):
        a, b = graph.nodes[i], graph.nodes[i + 1]
        block = outbound_blocks[i] if i < len(outbound_blocks) else {}
        route, selection = _direct_route_from_block(
            f"{_leg_id(i)}-direct", block, a.city, b.city, dep,
        )
        graph.edges.append(TripLeg(
            id=_leg_id(i), from_node_id=a.id, to_node_id=b.id,
            from_city=a.city, to_city=b.city, date=dep,
            routes=[route], selection=selection,
        ))

    # ── Return edge: terminal → origin (if a return journey exists).
    ret = transport.get("return_journey") or {}
    if ret and len(graph.nodes) >= 2:
        term, org = graph.nodes[-1], graph.nodes[0]
        rid = _leg_id(len(graph.edges))
        route, selection = _direct_route_from_block(
            f"{rid}-direct", ret, term.city, org.city, req.get("return_date"),
        )
        graph.edges.append(TripLeg(
            id=rid, from_node_id=term.id, to_node_id=org.id,
            from_city=term.city, to_city=org.city, date=req.get("return_date"),
            routes=[route], selection=selection,
        ))

    return graph


def ensure_graph(state: dict) -> TripGraph:
    """Return the trip's canonical graph, lazily deriving it from legacy fields on
    first access. Does not mutate `state` — the caller decides when to persist."""
    raw = state.get("graph")
    if raw:
        return TripGraph.model_validate(raw)
    return derive_graph_from_legacy(state)


# ── Path walk: nodes/edges in travel order (order-independent) ────────────────────

def _origin_node(graph: TripGraph) -> Optional[TripStop]:
    if not graph.nodes:
        return None
    return next((n for n in graph.nodes if n.role == "origin"), graph.nodes[0])


def ordered_path(graph: TripGraph) -> tuple[list[TripStop], list[TripLeg], Optional[TripLeg]]:
    """Resolve the trip's actual travel order by WALKING outbound edges from the
    origin, rather than trusting node/edge insertion order (which a mid-trip
    insert scrambles). Returns:

      (nodes_in_travel_order, outbound_edges_in_path_order, return_edge)

    `nodes_in_travel_order` = origin → each reached stop, then any orphan nodes
    (unreachable from origin — e.g. a stop whose inbound leg was just displaced)
    appended in their original order so nothing is silently dropped. The return
    edge is the one landing back on the origin. This is the single source of
    order for the projection, cost, and date sequencing — so a trip whose graph
    predates the ordered builder still renders correctly on read."""
    origin = _origin_node(graph)
    if origin is None:
        return [], [], None

    out_by_from: dict[str, list[TripLeg]] = {}
    inbound: Optional[TripLeg] = None
    for e in graph.edges:
        if e.to_node_id == origin.id:
            inbound = inbound or e
        else:
            out_by_from.setdefault(e.from_node_id, []).append(e)

    path_nodes = [origin]
    outbound: list[TripLeg] = []
    seen = {origin.id}
    cur = origin
    while True:
        nxt = next((e for e in out_by_from.get(cur.id, []) if e.to_node_id not in seen), None)
        if not nxt:
            break
        node = graph.node(nxt.to_node_id)
        if node is None:
            break
        path_nodes.append(node)
        outbound.append(nxt)
        seen.add(node.id)
        cur = node

    ordered = list(path_nodes) + [n for n in graph.nodes if n.id not in seen]
    return ordered, outbound, inbound


def order_nodes_along_path(graph: TripGraph) -> None:
    """Re-sort `graph.nodes` into travel order in place. Call after any mutation
    so journey_summary, date sequencing, and the flat itinerary all follow the
    real route, not the order stops happened to be added."""
    graph.nodes = ordered_path(graph)[0]

