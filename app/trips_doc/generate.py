"""
Doc-writing trip generation — the same search pipeline the composer runs
(leg stabilization, gateway fallback, per-stop itineraries + stays), but the
output is progressive TripDoc UPDATEs instead of patches/events/streams.

Write points (all idempotent whole-doc UPDATEs):
  planning 10%  → skeleton routed dates
  planning 45%  → transport options landed
  planning 80%  → itineraries + stays landed
  ready   100%  → final doc (weather, destination, carry-forward, budget)

No SSE: the client polls GET while status == "planning". If this process
dies mid-run the doc simply stays at its last written section; a watchdog
status flip is the caller's concern (endpoint marks error on exception).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import sentry_sdk

from app.core.background import spawn_background
from app.db.client import run_db

from app.trip_state import builder as B
from app.trip_state import compose as C
from app.trip_state import graph as G

from . import mytrips, store
from .serialize import _return_deadline, carry_forward_selections, chosen_hotel, doc_from_graph

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initial_doc(spec: dict, title: str | None = None) -> dict:
    """The doc a freshly created trip starts with — enough for the workspace
    to render a header + progress while generation runs."""
    stops = [s.get("city") for s in (spec.get("stops") or []) if s.get("city")]
    route = [spec.get("origin"), *stops, spec.get("destination"), spec.get("origin")]
    return {
        "id": None,  # stamped after insert
        "title": title or f"{spec.get('origin')} → {spec.get('destination')}",
        "status": "planning",
        "progress": 5,
        "spec": dict(spec),
        "route": [c for c in route if c],
        "destination": {"name": spec.get("destination")},
        "itinerary": [],
        "transport": [],
        "weather": {},
        "documents": {},
        "notes": [],
        "budget": {},
        "generated_at": _now(),
        "updated_at": _now(),
    }


GENERATION_TIMEOUT_SECONDS = 600  # observed healthy runs finish in ~5.5 min


def _honest_failure(exc: Exception) -> str:
    """The user-facing reason for a failed generation. No invented causes."""
    msg = str(exc).lower()
    if "free-models" in msg or "429" in msg or "quota" in msg or "rate limit" in msg:
        return ("Free AI plan generation is temporarily unavailable - the daily "
                "free-model quota is exhausted. Please try again later.")
    if "no leg" in msg and "routed" in msg:
        return ("Live transport for this route could not be found right now. "
                "Try different dates or a nearby city.")
    return "Trip planning failed. Please try again."


def _window_flags(graph: G.TripGraph, spec: dict) -> list[str]:
    """Visible constraint check: the requested dates and clock times bind the
    WHOLE trip, return travel included. Surface a plain flag wherever the
    grounded plan cannot meet them — never silently extend the trip or claim
    the window is met."""
    flags: list[str] = []
    dep = spec.get("departure_date")
    if not dep:
        return flags
    try:
        from datetime import date as _date, timedelta
        y, m, d = (int(x) for x in str(dep).split("-"))
        days = max(int(spec.get("days") or 1), 1)
        trip_end = _date(y, m, d) + timedelta(days=days - 1)
    except Exception:  # noqa: BLE001 — unparseable spec dates skip the check
        return flags
    end_time = spec.get("end_time")
    ret = graph.edges[-1] if graph.edges else None
    if ret is None or not ret.date:
        return flags
    window = _return_deadline(spec)
    arrive_by, not_before = window if window else (None, None)
    end_iso = trip_end.isoformat()
    if ret.date > end_iso:
        flags.append(
            f"Your {days}-day window isn't fully met: no live "
            f"{ret.from_city} → {ret.to_city} services were found inside it, so the "
            f"return shown leaves {ret.date}, outside your plan. Adjust the dates or "
            "check operators directly for that day."
        )
        return flags  # outside the window entirely; the clock check no longer applies
    if end_time:
        arr = None
        route = G.active_route(ret, None, spec.get("budget_tier"), arrive_by=arrive_by,
                               not_before=not_before)
        if route and route.hops:
            opt = G.selected_option_for_hop(ret, route, len(route.hops) - 1, None,
                                            spec.get("budget_tier"), arrive_by=arrive_by,
                                            not_before=not_before)
            if opt and opt.arrival:
                arr = str(opt.arrival)
        if arr and len(arr) >= 16:
            arr_date, arr_clock = arr[:10], arr[11:16]
            if arr_date > end_iso or (arr_date == end_iso and arr_clock > end_time):
                flags.append(
                    f"The recommended return reaches {ret.to_city} at {arr_clock} on "
                    f"{arr_date} — after your {end_time} target. Earlier options may be "
                    f"listed; otherwise the {end_time} target isn't met."
                )
    return flags


async def _backfill_sparse_days(stayed: list, flags: list[str],
                                  minimum: int = 3, add: int = 3) -> None:
    """A day the honest filters trimmed to almost nothing is not useful. Add
    REAL nearby places from the stop's OpenStreetMap POI list — never
    invented names, never a fabricated clock: additions are unscheduled
    ('slot them as you like') so no false timing is claimed."""
    from app.services.osm_places import city_pois, backfill_pois
    for node in stayed:
        if not node.days:
            continue
        try:
            pois = await city_pois(node.city or "")
        except Exception:  # noqa: BLE001
            pois = []
        if not pois:
            continue
        for day in node.days:
            acts = day.get("activities") or []
            if len(acts) >= minimum:
                continue
            titles = [str(a.get("title") or "") for a in acts]
            extra = backfill_pois(pois, titles, limit=min(add, minimum - len(acts)))
            if not extra:
                continue
            for poi in extra:
                acts.append({
                    "title": poi["name"],
                    "description": (
                        "Verified nearby place (OpenStreetMap name and location), added "
                        "because this day was light. No time slot assigned — slot it as "
                        "you like; opening hours and details are not independently verified."
                    ),
                    "category": (poi.get("category") or "attraction").replace("_", " ").title(),
                    "start_time": None,
                    "lat": poi["lat"],
                    "lng": poi["lng"],
                    "source": "openstreetmap",
                    "grounded": "server",
                    "backfilled": True,
                    "maps_url": (
                        f"https://www.openstreetmap.org/{poi['osm_type']}/{poi['osm_id']}"
                        if poi.get("osm_type") and poi.get("osm_id")
                        else f"https://www.openstreetmap.org/?mlat={poi['lat']}&mlon={poi['lng']}#map=17/{poi['lat']}/{poi['lng']}"
                    ),
                })
            day["activities"] = acts
            flags.append(
                f"Day {day.get('day') or ''} in {node.city} was light after grounded "
                f"timing was applied, so it now also lists verified nearby places "
                f"({', '.join(p['name'] for p in extra)}) — unscheduled, no invented times."
            )


def _waitlist_flags(graph: G.TripGraph, spec: dict, flags: list[str]) -> None:
    """A waitlisted fare is not a confirmed seat. When the recommended ride on
    a leg carries one, the caveat must be visible next to the pick, not only
    inside provider class data."""
    try:
        _, outbound, inbound = G.ordered_path(graph)
    except Exception:  # noqa: BLE001
        return
    budget = spec.get("budget_tier")
    window = _return_deadline(spec)
    arrive_by, not_before = window if window else (None, None)
    for leg in outbound + ([inbound] if inbound else []):
        is_return = inbound is not None and leg is inbound
        route = G.active_route(leg, None, budget,
                               arrive_by=arrive_by if is_return else None,
                               not_before=not_before if is_return else None)
        if not route or not route.hops:
            continue
        for i, hop in enumerate(route.hops):
            final = i == len(route.hops) - 1
            opt = G.selected_option_for_hop(
                leg, route, i, None, budget,
                arrive_by=arrive_by if (is_return and final) else None,
                not_before=not_before if (is_return and final) else None)
            if opt and opt.availability_note:
                dep = str(opt.departure or "")
                dep_clock = dep[11:16] if len(dep) >= 16 else dep[:5]
                flags.append(
                    f"{hop.from_city} → {hop.to_city}: {opt.operator or opt.mode} "
                    f"({dep_clock}) — {opt.availability_note}"
                )


def _retime_day1_for_arrival(graph: G.TripGraph, spec: dict, flags: list[str]) -> None:
    """Day 1 at the terminal cannot start before the traveller actually
    arrives. Re-time that day's activities from the anchored arrival onward
    and flag it — never present a ready-to-follow morning that the grounded
    transport makes impossible."""
    try:
        _, outbound, _ = G.ordered_path(graph)
    except Exception:  # noqa: BLE001
        return
    if not outbound:
        return
    leg = outbound[-1]
    route = G.active_route(leg, None, spec.get("budget_tier"))
    if not route or not route.hops:
        return
    opt = G.selected_option_for_hop(leg, route, len(route.hops) - 1, None,
                                    spec.get("budget_tier"))
    arr = str(getattr(opt, "arrival", "") or "")
    if len(arr) < 16:
        return
    dst = graph.node(leg.to_node_id)
    if dst is None or not dst.days:
        return

    def _mins(t: str) -> int | None:
        try:
            return int(t[:2]) * 60 + int(t[3:5])
        except Exception:  # noqa: BLE001
            return None

    arr_clock = arr[11:16]
    arr_m = _mins(arr_clock)
    if arr_m is None:
        return
    arr_date = arr[:10]
    leg_date = str(getattr(leg, "date", "") or "")
    overnight = bool(arr_date and leg_date and arr_date > leg_date)
    acts = (dst.days[0] or {}).get("activities") or []

    if overnight:
        # The ride in force lands the morning AFTER day 1 (an explicit
        # overnight pick — the derived recommendation is anchored same-day).
        # Every day-1 destination activity is unreachable on day 1: the
        # traveller is in transit. Move them onto the arrival day, re-timed
        # from the arrival clock, and say exactly what happened.
        if not acts:
            return
        moved, dropped = [], []
        cur = arr_m + _ARRIVAL_TRANSFER_MIN
        for a in acts:
            dur = int(a.get("duration_min") or 90)
            if cur + dur > 21 * 60:
                dropped.append(a)
                continue
            a["start_time"] = f"{cur // 60:02d}:{cur % 60:02d}"
            a["retimed_for_arrival"] = True
            moved.append(a)
            cur += dur + 30
        dst.days[0]["activities"] = []
        if len(dst.days) > 1:
            dst.days[1]["activities"] = moved + (dst.days[1].get("activities") or [])
        else:
            dst.days[0]["activities"] = moved  # no later day to carry them
        names = ", ".join(a.get("title") or "?" for a in dropped)
        flags.append(
            f"Your ride reaches {dst.city} at {arr_clock} on {arr_date} — the morning "
            f"after day 1 — so day 1 at the destination is travel time. Its activities "
            f"were moved to after arrival"
            + (f"; not everything fit before evening and was left out: {names}. " if dropped else ". ")
            + "Times from arrival onward are approximate and start with an estimated "
            "45-minute buffer from the drop point - not a verified transfer."
        )
        return

    early = [a for a in acts
             if (_mins(str(a.get("start_time") or a.get("time") or "")) or 9999) < arr_m]
    if not early:
        return
    cur = arr_m + _ARRIVAL_TRANSFER_MIN
    kept, dropped = [], []
    for a in acts:
        dur = int(a.get("duration_min") or 90)
        if cur + dur > 21 * 60:
            # Don't portray late-night slots as a plan: the day is full.
            dropped.append(a)
            continue
        a["start_time"] = f"{cur // 60:02d}:{cur % 60:02d}"
        a["retimed_for_arrival"] = True
        kept.append(a)
        cur += dur + 30
    dst.days[0]["activities"] = kept
    if dropped:
        # Activities that no longer fit are removed from the day, not pushed
        # past a realistic evening — and the loss is said out loud.
        names = ", ".join(a.get("title") or "?" for a in dropped)
        flags.append(
            f"Day 1 in {dst.city} was re-timed around your {arr_clock} arrival; "
            f"not everything fit before evening, so it no longer includes: {names}. "
            "Times from arrival onward are approximate and start with an estimated "
            "45-minute buffer from the drop point - not a verified transfer."
        )
    else:
        flags.append(
            f"Day 1 in {dst.city} was re-timed around your {arr_clock} arrival — the "
            "original morning slots weren't reachable with the grounded transport. "
            "Times from arrival onward are approximate and start with an estimated "
            "45-minute buffer from the drop point - not a verified transfer."
        )


# Local transfer from the bus/train drop point to the first activity: the
# arrival clock is the vehicle's, not the traveller's feet on the ground.
_ARRIVAL_TRANSFER_MIN = 45
# Assumed overland speed for inter-activity transfers when both ends have
# coordinates; 30 km/h is a conservative hill-road figure, not a promise.
_TRANSFER_KMH = 30.0
_MIN_TRANSFER_MIN = 30


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    r = 6371.0
    dlat, dlng = radians(lat2 - lat1), radians(lng2 - lng1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * r * asin(sqrt(h))


def _act_coords(a: dict) -> tuple[float, float] | None:
    try:
        lat, lng = a.get("lat"), a.get("lng")
        if lat is None and isinstance(a.get("location"), dict):
            lat, lng = a["location"].get("lat"), a["location"].get("lng")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except Exception:  # noqa: BLE001
        return None


def _enforce_transfer_gaps(stayed: list, flags: list[str]) -> None:
    """Consecutive activities need real travel time between them: an arrival
    clock alone doesn't make a 90-minute-drive pair 30 minutes apart. Push
    later activities forward to fit the transfer; what pushes past the
    evening is dropped and named in a flag, never silently squeezed."""
    def _mins(t: str) -> int | None:
        try:
            return int(t[:2]) * 60 + int(t[3:5])
        except Exception:  # noqa: BLE001
            return None

    for node in stayed:
        for day in (getattr(node, "days", None) or []):
            acts = day.get("activities") or []
            if len(acts) < 2:
                continue
            kept, dropped = [], []
            adjusted = False
            prev_end, prev_coords = None, None
            for a in acts:
                start = _mins(str(a.get("start_time") or a.get("time") or ""))
                dur = int(a.get("duration_min") or 90)
                if start is not None and prev_end is not None:
                    coords = _act_coords(a)
                    gap = _MIN_TRANSFER_MIN
                    if coords and prev_coords:
                        km = _haversine_km(prev_coords[0], prev_coords[1], coords[0], coords[1])
                        gap = max(_MIN_TRANSFER_MIN, int(km / _TRANSFER_KMH * 60))
                    if start < prev_end + gap:
                        start = prev_end + gap
                        a["start_time"] = f"{start // 60:02d}:{start % 60:02d}"
                        a["transfer_adjusted"] = True
                        adjusted = True
                if start is not None and start + dur > 21 * 60:
                    dropped.append(a)
                    continue
                kept.append(a)
                if start is not None:
                    prev_end = start + dur
                    prev_coords = _act_coords(a) or prev_coords
            if dropped:
                day["activities"] = kept
                names = ", ".join(x.get("title") or "?" for x in dropped)
                flags.append(
                    f"Day {day.get('day')}: estimated travel time between stops didn't "
                    f"leave room for everything before evening, so it no longer "
                    f"includes: {names}."
                )
            if adjusted:
                # The gaps are straight-line estimates, not road evidence.
                flags.append(
                    f"Day {day.get('day')} times include ESTIMATED transfer gaps "
                    "(straight-line distance at ~30 km/h, not measured road time) - "
                    "real travel, especially on hill roads, can take longer. Treat "
                    "the clock times as rough estimates, not a feasible-route promise."
                )


def _retime_final_day_for_departure(graph: G.TripGraph, spec: dict, flags: list[str]) -> None:
    """The last day cannot schedule past the grounded ride home. Activities
    ending after the return departure (minus an ESTIMATED transfer to the
    boarding point when it isn't the stay city) are removed and named —
    never left as a plan the traveller can't both do and catch.""" 
    try:
        _, _, inbound = G.ordered_path(graph)
    except Exception:  # noqa: BLE001
        return
    if inbound is None:
        return
    window = _return_deadline(spec)
    arrive_by, not_before = window if window else (None, None)
    route = G.active_route(inbound, None, spec.get("budget_tier"), arrive_by=arrive_by,
                           not_before=not_before)
    if not route or not route.hops:
        return
    hop = route.hops[-1]
    opt = G.selected_option_for_hop(inbound, route, len(route.hops) - 1, None,
                                    spec.get("budget_tier"), arrive_by=arrive_by,
                                    not_before=not_before)
    if not opt or not opt.departure:
        return
    dst = graph.node(inbound.from_node_id)
    if dst is None or not dst.days:
        return
    dep_dt = G._dt_with_date(str(opt.departure), hop.date or inbound.date)
    if dep_dt is None:
        return
    transfer = 60 if (hop.from_city or "") != (dst.city or "") else 30
    cutoff = dep_dt.timestamp() / 60 - transfer
    dep_clock = f"{dep_dt.hour:02d}:{dep_dt.minute:02d}"

    def _mins(t: str) -> int | None:
        try:
            return int(t[:2]) * 60 + int(t[3:5])
        except Exception:  # noqa: BLE001
            return None

    dep_base = dep_dt.replace(hour=0, minute=0).timestamp() / 60
    day = dst.days[-1]
    acts = day.get("activities") or []
    if not acts:
        return
    kept, dropped = [], []
    for a in acts:
        start = _mins(str(a.get("start_time") or a.get("time") or ""))
        if start is None:
            kept.append(a)
            continue
        end = dep_base + start + int(a.get("duration_min") or 90)
        (dropped if end > cutoff else kept).append(a)
    if not dropped:
        return
    day["activities"] = kept
    names = ", ".join(a.get("title") or "?" for a in dropped)
    flags.append(
        f"Your grounded return leaves {hop.from_city} at {dep_clock}; the last day in "
        f"{dst.city} no longer includes: {names} — they ended after the departure minus "
        f"an ESTIMATED {transfer}-minute transfer to the boarding point (not a measured "
        "road time). Earlier slots are unchanged."
    )


async def run_generation(trip_id: str, spec: dict, user_id: str,
                         prior_doc: dict | None = None) -> None:
    """Entry point: admit through the global generation queue, then build."""
    from app.core.generation_queue import generation_slot, has_free_slot

    if not has_free_slot():
        try:
            await store.set_status(trip_id, "planning", progress=2)
        except Exception:  # noqa: BLE001 — a queue notice must never break generation
            pass
    async with generation_slot(trip_id):
        try:
            # Hard cap: a hung provider await must never leave the doc spinning
            # at "planning" forever (the stuck-at-10% bug). Normal successful
            # runs finish well under this.
            await asyncio.wait_for(
                _generate(trip_id, spec, user_id, prior_doc),
                timeout=GENERATION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("[trip-doc] generation timed out for %s", trip_id)
            sentry_sdk.capture_message(f"trip-doc generation timeout {trip_id}")
            try:
                await store.set_status(
                    trip_id, "error", progress=100,
                    error="Plan generation took too long on the free deployment and was stopped. Please try again.")
            except Exception:  # noqa: BLE001
                logger.error("[trip-doc] could not mark %s as error", trip_id)
        except Exception as exc:  # noqa: BLE001 — the doc must reach a terminal status
            logger.error("[trip-doc] generation failed for %s: %s", trip_id, exc, exc_info=True)
            sentry_sdk.capture_exception(exc)
            try:
                await store.set_status(trip_id, "error", progress=100,
                                       error=_honest_failure(exc))
            except Exception:  # noqa: BLE001
                logger.error("[trip-doc] could not mark %s as error", trip_id)


async def _generate(trip_id: str, spec: dict, user_id: str,
                    prior_doc: dict | None) -> None:
    from app.services.weather import get_trip_weather

    # A recovered run must keep its recovery counter through every milestone
    # rewrite, or a repeatedly-orphaned trip would loop forever instead of
    # reaching MAX_RECOVERY_ATTEMPTS and a terminal error.
    recovery_marker = dict((prior_doc or {}).get("recovery") or {})

    def _keep_recovery(doc: dict) -> dict:
        if recovery_marker:
            doc["recovery"] = dict(recovery_marker)
        return doc

    started_at = _now()
    travelers = max(int(spec.get("travelers") or 1), 1)
    budget = spec.get("budget_tier") or spec.get("budget") or "mid"

    async def _weather() -> dict:
        try:
            w = await get_trip_weather(spec.get("destination"),
                                       spec.get("departure_date"),
                                       days=int(spec.get("days") or 3))
            return w if w.get("daily") else {}
        except Exception as exc:  # noqa: BLE001 — weather is a nice-to-have
            logger.warning("[trip-doc] weather fetch failed for %s: %s", trip_id, exc)
            return {}

    weather_task = asyncio.create_task(_weather())

    # ── skeleton + leg search (builder's stabilization loop) ────────────────
    graph = C.build_skeleton(spec)
    B._resequence_dates(graph, spec.get("departure_date"))
    work_state = {"request": {
        "origin": spec.get("origin"), "destination": spec.get("destination"),
        "departure_date": spec.get("departure_date"), "days": spec.get("days") or 3,
        "travelers": travelers, "budget_tier": budget,
        "interests": spec.get("interests") or [], "stops": spec.get("stops") or [],
        "comments": spec.get("comments") or "",
        "start_time": spec.get("start_time"), "end_time": spec.get("end_time"),
    }}
    await store.put_doc(trip_id, _keep_recovery(doc_from_graph(
        trip_id, spec, graph, status="planning", progress=10)))

    notes = await B._sync_legs_after_change(work_state, graph, user_id)

    def _has_empty_hop(leg: G.TripLeg) -> bool:
        """A routed chain with a hop nobody can book (the planner searched that
        exact segment and found nothing) is as unusable as no route at all."""
        return bool(leg.routes) and any(
            not hop.options for hop in (leg.routes[0].hops or [])
        )

    # Gateway fallback for anything unroutable — including chains with a dead
    # hop (e.g. a via-Ambala plan whose Bhiwani→Ambala segment has no trains).
    for leg in graph.edges:
        if leg.status == "no_route" or not leg.routes or _has_empty_hop(leg):
            note = await C._route_via_best_gateway(
                leg, travelers, budget, user_id,
                stay_nights=(graph.node(leg.to_node_id) or G.TripStop(id="", city="")).nights,
            )
            if note:
                notes.append(note)
    B._resequence_dates(graph, spec.get("departure_date"))

    if not any(e.routes for e in graph.edges):
        raise RuntimeError(f"no leg of {graph.journey_summary()} could be routed")

    await store.put_doc(trip_id, _keep_recovery(doc_from_graph(
        trip_id, spec, graph, status="planning", progress=45, notes=notes)))

    # ── itineraries + stays for every city the traveller sleeps in ──────────
    stayed = [n for n in graph.nodes if n.role != "origin" and (n.nights or 0) > 0]
    terminal_trip = await _fill_itineraries_and_stays(
        graph, stayed, spec, travelers, budget, user_id)

    await store.put_doc(trip_id, _keep_recovery(doc_from_graph(
        trip_id, spec, graph, status="planning", progress=80, notes=notes)))

    await B._geocode_nodes(graph)
    weather = await _wait_weather(weather_task)
    window_flags = _window_flags(graph, spec)
    _retime_day1_for_arrival(graph, spec, window_flags)
    _retime_final_day_for_departure(graph, spec, window_flags)
    _enforce_transfer_gaps(stayed, window_flags)
    _waitlist_flags(graph, spec, window_flags)
    await _backfill_sparse_days(stayed, window_flags)
    for _ms in ((terminal_trip.get("trip_quality") or {}).get("must_stops") or []):
        if _ms.get("injected") or _ms.get("feasibility") == "unverified":
            window_flags.append(
                f"Requested stop at {_ms.get('place')}: included in the plan as a "
                "requested waypoint. Its placement and timing are tentative — the "
                "detour, opening hours and how long it adds are not verified, so "
                "treat that part of the schedule as a plan to confirm, not a timetable."
            )

    # ── final doc ────────────────────────────────────────────────────────────
    title = (terminal_trip.get("trip_name")
             or f"{spec.get('origin')} → {spec.get('destination')}")
    doc = _keep_recovery(doc_from_graph(
        trip_id, spec, graph,
        title=title, status="ready", progress=100,
        destination={
            "name": spec.get("destination"),
            "description": terminal_trip.get("destination_description"),
            "cover_photo_ref": terminal_trip.get("cover_photo_ref"),
        },
        weather=weather, notes=notes,
        generation={"engine": "tripdoc-v1",
                    "spec_version": int(spec.get("version") or 1),
                    "started_at": started_at, "finished_at": _now(),
                    "status": "complete"},
    ))
    if terminal_trip.get("trip_intelligence"):
        doc["intelligence"] = terminal_trip["trip_intelligence"]
    if window_flags:
        # User-facing constraint flags: rendered as visible banners in the UI.
        doc["flags"] = window_flags
        doc["notes"] = list(doc.get("notes") or []) + window_flags
    dropped = carry_forward_selections(prior_doc, doc)
    if dropped:
        doc["notes"] = list(doc.get("notes") or []) + \
            ["couldn't keep your picks on: " + ", ".join(dropped)]
    await store.put_doc(trip_id, doc)
    logger.info("[trip-doc] %s built: %s | %d legs | %d stayed stops",
                trip_id, " → ".join(doc.get("route") or []),
                len(doc.get("transport") or []), len(stayed))

    # ── best-effort tail: mirror, referral, memory ───────────────────────────
    await mytrips.upsert_my_trip(trip_id, user_id, doc)
    _award_referrer(trip_id, user_id)
    await _distill(user_id, doc)


async def _fill_itineraries_and_stays(graph: G.TripGraph, stayed: list[G.TripStop],
                                      spec: dict, travelers: int, budget: str,
                                      user_id: str) -> dict:
    """Per-stop day plans + hotel searches (parallel) — same pipeline the
    composer ran. Returns the terminal stop's plan (title/description source)."""
    from app.api.v1.plan_trip.models import PlanTripRequest
    from app.api.v1.plan_trip.orchestrator import run_plan_trip

    first_stop_id = stayed[0].id if stayed else None

    async def _itinerary_for(node: G.TripStop) -> dict | None:
        days = (node.nights or 1) + (1 if node.role == "terminal" else 0)
        try:
            return await run_plan_trip(
                PlanTripRequest(
                    destination=node.city, days=min(days, 7), budget=budget,
                    travelers=travelers, interests=spec.get("interests") or [],
                    notes=spec.get("comments") or None,
                    start_date=node.arrival_date, end_date=node.departure_date,
                    # Clock times bind where they actually happen: the start
                    # time on the first stop's day 1, the end time on the
                    # terminal stop's last day.
                    start_time=(spec.get("start_time") if node.id == first_stop_id else None),
                    end_time=(spec.get("end_time") if node.role == "terminal" else None),
                    skip_user_trips=True,
                ),
                user_id, profile={},
            )
        except Exception as exc:  # noqa: BLE001 — a stop without an itinerary is degraded, not fatal
            logger.warning("[trip-doc] itinerary for %s failed: %s", node.city, exc)
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

    for node in stayed:  # stays the itinerary pipeline didn't cover
        if not node.stay.search_results:
            hotels = await B._fetch_stop_hotels(node, travelers, budget)
            B._attach_stay(node, hotels, node.nights or 1)

    # The recommendation agent picks each stop's stay and says WHY — the
    # ai_reason/is_recommended markers the hotel cards render. Best-effort,
    # concurrent across stops; on any miss the cheapest-first fallback holds.
    await asyncio.gather(*(
        _recommend_stop_stay(n, travelers, budget) for n in stayed
        if n.stay.search_results
    ))
    return terminal_trip


async def _recommend_stop_stay(node: G.TripStop, travelers: int, budget: str) -> None:
    from app.agents.recommendation_agent import recommend_hotels

    pool = [h for h in node.stay.search_results if isinstance(h, dict)]
    if len(pool) < 2:
        return
    try:
        res = await recommend_hotels(pool, {
            "destination": node.city,
            "checkin": node.arrival_date,
            "checkout": node.departure_date,
            "adults": travelers,
            "nights": node.nights or 1,
            "budget": budget,
        })
        idx = (res or {}).get("recommended_index")
        if not isinstance(idx, int) or not (0 <= idx < len(pool)):
            return
        for h in pool:
            h["is_recommended"] = False
        chosen = pool[idx]
        chosen["is_recommended"] = True
        chosen["ai_reason"] = res.get("reason")
        chosen["ai_pros"] = res.get("pros")
        chosen["ai_best_for"] = res.get("best_for")
        ru = res.get("runner_up_index")
        if isinstance(ru, int) and 0 <= ru < len(pool) and ru != idx:
            pool[ru]["ai_reason"] = res.get("runner_up_reason")
    except Exception as exc:  # noqa: BLE001 — advisory only
        logger.warning("[trip-doc] stay recommendation for %s failed: %s", node.city, exc)


async def _wait_weather(task: asyncio.Task) -> dict:
    try:
        return await asyncio.wait_for(task, timeout=10)
    except Exception:  # noqa: BLE001 — optional section
        return {}


def _award_referrer(trip_id: str, user_id: str) -> None:
    """First completed trip pays the user's referrer (idempotent ledger)."""
    from app.api.v1.referral.service import award_referrer_on_first_action

    spawn_background(
        run_db(lambda: award_referrer_on_first_action(user_id, "trip")),
        name=f"referral-award:{trip_id}",
    )


async def _distill(user_id: str, doc: dict) -> None:
    """Feed the finished trip to per-user memory via a legacy-shaped adapter.
    Best-effort — memory is advisory."""
    try:
        from app.services.user_memory import distill_from_trip

        spec = doc.get("spec") or {}
        terminal = next((s for s in reversed(doc.get("itinerary") or [])
                         if s.get("role") == "terminal"), None)
        adapter = {
            "request": {
                "origin": spec.get("origin"), "destination": spec.get("destination"),
                "departure_date": spec.get("departure_date"), "days": spec.get("days"),
                "travelers": spec.get("travelers"),
                "budget_tier": spec.get("budget_tier") or "mid",
                "interests": spec.get("interests") or [],
            },
            "stay": {"selected": chosen_hotel(terminal) if terminal else None},
            "itinerary": {"days": [d for s in doc.get("itinerary") or []
                                   for d in (s.get("days") or [])]},
            "costs": doc.get("budget") or {},
        }
        await distill_from_trip(user_id, adapter)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[trip-doc] memory distill failed: %s", exc)


# ── durable recovery (zero-spend, DB-backed) ─────────────────────────────────
# Generation checkpoints are persisted as whole-doc milestones in trip_states
# (spec + route at 10%, transport at 45%, itineraries+stays at 80%), so the DB
# always holds everything a restart needs. The reconciler adopts trips whose
# generation died with a previous process (restart, deploy, free-tier sleep)
# and restarts them from the persisted spec.
#
# Duplicate-safety: the 600s cap above means a LIVE generation always reaches
# a terminal write within 10 minutes of starting, and its first doc write
# happens at start. A planning row with no write for STALE_ORPHAN_SECONDS
# (> cap + margin) is therefore provably not owned by any live process,
# including a briefly-overlapping old instance mid-deploy. Only such rows are
# adopted. Queue-wait pathological depth (>2 full-cap jobs ahead) is the one
# theoretical false-positive and is bounded by the per-user create rate limit.
STALE_ORPHAN_SECONDS = 780          # 13 min > 600s cap + milestone-write margin
MAX_RECOVERY_ATTEMPTS = 2           # then a terminal honest error


def _stale_planning_rows_sync(cutoff_iso: str) -> list[dict]:
    from app.db.client import get_db
    res = (get_db().table("trip_states").select("id, user_id, doc, updated_at")
           .eq("status", "planning").lt("updated_at", cutoff_iso).execute())
    return res.data or []


def _claim_row_sync(trip_id: str, stale_updated_at: str, doc: dict,
                    recovery: dict) -> bool:
    """Atomic adoption claim: ONE conditional UPDATE carrying the attempt
    increment and a fresh updated_at, gated on the row still being exactly
    the stale planning row we read. During a rolling deploy two instances can
    read the same stale row; the loser's WHERE clause no longer matches once
    the winner's write lands, so it updates nothing and spawns nothing.
    Returns True only for the winner."""
    from app.db.client import get_db
    new_doc = dict(doc)
    new_doc["recovery"] = recovery
    new_doc["updated_at"] = _now()
    res = (get_db().table("trip_states")
           .update({"doc": new_doc, "updated_at": _now()})
           .eq("id", trip_id).eq("status", "planning")
           .eq("updated_at", stale_updated_at).execute())
    return bool(res.data)


async def reconcile_orphaned_trips() -> dict:
    """Adopt provably-dead planning trips: restart from the persisted spec, or
    after MAX_RECOVERY_ATTEMPTS mark a terminal error. Never touches a row a
    live process could still own (see STALE_ORPHAN_SECONDS)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=STALE_ORPHAN_SECONDS)).isoformat()
    try:
        rows = await run_db(_stale_planning_rows_sync, cutoff)
    except Exception:  # noqa: BLE001 - a reconciler pass must never crash the app
        logger.error("[trip-doc] reconciler query failed", exc_info=True)
        return {"error": "query failed"}
    restarted, failed = 0, 0
    for row in rows:
        trip_id, doc = row["id"], (row.get("doc") or {})
        spec = doc.get("spec") or {}
        recovery = dict(doc.get("recovery") or {})
        attempts = int(recovery.get("count") or 0)
        if not spec.get("origin") or not spec.get("destination") or attempts >= MAX_RECOVERY_ATTEMPTS:
            try:
                await store.set_status(
                    trip_id, "error", progress=100,
                    error=("This plan was interrupted when the server restarted "
                           "(the free host sleeps and redeploys) and could not "
                           "be recovered. Your details are kept - start a new "
                           "trip with the same details to try again."))
                failed += 1
            except Exception:  # noqa: BLE001
                logger.error("[trip-doc] reconciler could not fail %s", trip_id)
            continue
        recovery["count"] = attempts + 1
        recovery["last_at"] = _now()
        try:
            won = await run_db(_claim_row_sync, trip_id, row.get("updated_at"),
                               doc, recovery)
            if not won:
                logger.info("[trip-doc] %s adoption claim lost to a concurrent "
                            "reconciler - leaving it to the winner", trip_id)
                continue
            claimed_doc = {**doc, "recovery": recovery}
            spawn_background(run_generation(trip_id, spec, str(row.get("user_id") or ""), claimed_doc),
                             name=f"trip_doc_gen:{trip_id}:recovery{recovery['count']}")
            restarted += 1
            logger.info("[trip-doc] %s adopted for recovery attempt %d", trip_id, recovery["count"])
        except Exception:  # noqa: BLE001
            logger.error("[trip-doc] reconciler could not adopt %s", trip_id, exc_info=True)
    if restarted or failed:
        logger.info("[trip-doc] reconciler: %d restarted, %d terminally failed", restarted, failed)
    return {"restarted": restarted, "failed": failed}


async def reconciler_loop(interval_seconds: int = 300) -> None:
    """Periodic pass so orphans younger than the boot-time staleness threshold
    are still recovered later, and orphans created by sleep-while-idle are
    recovered as soon as any traffic wakes the instance."""
    while True:
        try:
            await reconcile_orphaned_trips()
        except Exception:  # noqa: BLE001
            logger.error("[trip-doc] reconciler pass failed", exc_info=True)
        await asyncio.sleep(interval_seconds)
