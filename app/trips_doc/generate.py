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
from .serialize import carry_forward_selections, chosen_hotel, doc_from_graph

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
            await _generate(trip_id, spec, user_id, prior_doc)
        except Exception as exc:  # noqa: BLE001 — the doc must reach a terminal status
            logger.error("[trip-doc] generation failed for %s: %s", trip_id, exc, exc_info=True)
            sentry_sdk.capture_exception(exc)
            try:
                await store.set_status(trip_id, "error", progress=100)
            except Exception:  # noqa: BLE001
                logger.error("[trip-doc] could not mark %s as error", trip_id)


async def _generate(trip_id: str, spec: dict, user_id: str,
                    prior_doc: dict | None) -> None:
    from app.services.weather import get_trip_weather

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
    }}
    await store.put_doc(trip_id, doc_from_graph(
        trip_id, spec, graph, status="planning", progress=10))

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

    await store.put_doc(trip_id, doc_from_graph(
        trip_id, spec, graph, status="planning", progress=45, notes=notes))

    # ── itineraries + stays for every city the traveller sleeps in ──────────
    stayed = [n for n in graph.nodes if n.role != "origin" and (n.nights or 0) > 0]
    terminal_trip = await _fill_itineraries_and_stays(
        graph, stayed, spec, travelers, budget, user_id)

    await store.put_doc(trip_id, doc_from_graph(
        trip_id, spec, graph, status="planning", progress=80, notes=notes))

    await B._geocode_nodes(graph)
    weather = await _wait_weather(weather_task)

    # ── final doc ────────────────────────────────────────────────────────────
    title = (terminal_trip.get("trip_name")
             or f"{spec.get('origin')} → {spec.get('destination')}")
    doc = doc_from_graph(
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
    )
    if terminal_trip.get("trip_intelligence"):
        doc["intelligence"] = terminal_trip["trip_intelligence"]
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

    async def _itinerary_for(node: G.TripStop) -> dict | None:
        days = (node.nights or 1) + (1 if node.role == "terminal" else 0)
        try:
            return await run_plan_trip(
                PlanTripRequest(
                    destination=node.city, days=min(days, 7), budget=budget,
                    travelers=travelers, interests=spec.get("interests") or [],
                    start_date=node.arrival_date, end_date=node.departure_date,
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
