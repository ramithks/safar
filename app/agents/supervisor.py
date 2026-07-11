"""
Supervisor Agent — orchestrates Journey + Itinerary agents and applies
budget interventions before returning to the client.

Current run_plan():
  run_plan_journey ─┐
  run_plan_trip    ─┤ parallel → merge → return
  return_journey   ─┘

Supervisor Agent:
  run_plan_journey ─┐
  run_plan_trip    ─┤ parallel → merge
  return_journey   ─┘
                       ↓
                  BudgetAgent.check()
                       ↓ if over budget + fix found
                  apply_intervention()   ← swaps transport mode, updates costs
                       ↓
                  validate_trip()        ← timing / hotel / return checks
                       ↓
                  return merged

The key behaviour: if the generated trip is over budget AND a cheaper
transport option was already fetched, the supervisor swaps the recommendation
and adjusts cost figures rather than silently returning an unaffordable plan.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from uuid import uuid4

from .budget_agent import budget_agent_check
from app.api.v1.plan_trip.constraints import validate_trip
from app.api.v1.plan_trip.models import PlanRequest, PlanJourneyRequest, PlanTripRequest
from app.api.v1.plan_trip.journey import run_plan_journey
from app.api.v1.plan_trip.orchestrator import run_plan_trip
from app.api.v1.plan_trip.budget import compute_detailed_budget
from app.api.v1.plan_trip.helpers import CURRENCY_CONFIG, fetch_user_profile
from app.core.timing import RequestTrace
from app.core.background import spawn_background
from app.db.client import get_db, run_db, run_db_resilient

logger = logging.getLogger(__name__)

_SYM_TO_CODE = {
    "₹": "inr", "$": "usd", "€": "eur", "£": "gbp",
    "¥": "jpy", "A$": "aud", "AED": "aed", "S$": "sgd",
}


# ── Shape helpers (copied from plan.py to keep supervisor self-contained) ──────

def _collect_bus_items(legs: list[dict], rec: dict | None) -> dict:
    items: list[dict] = []
    for leg in legs:
        if leg.get("mode", "").lower() == "bus" and leg.get("has_live_data"):
            items.extend((leg.get("live_data") or {}).get("buses") or [])
    return {"items": items, "recommended": rec}


def _shape_journey(j: dict, include_hotels: bool = False) -> dict:
    from app.api.v1.plan_trip.plan import _shape_journey as _orig_shape
    return _orig_shape(j, include_hotels=include_hotels)


# ── Budget intervention: swap transport recommendation + update costs ──────────

def _apply_intervention(merged: dict, intervention: dict, currency_code: str) -> dict:
    """
    Swap the going journey's recommended transport to the cheaper mode
    suggested by the Budget Agent, then update cost figures.

    Mutates merged in-place. Returns merged.
    """
    to_mode = intervention.get("to_mode")
    option  = intervention.get("option") or {}
    saving  = intervention.get("saving_total") or 0
    msg     = intervention.get("message") or ""

    going = merged.get("going") or {}

    # Mark old recommended as no longer recommended
    for mode_key in ("flights", "trains", "buses"):
        rec = (going.get(mode_key) or {}).get("recommended")
        if rec:
            rec["is_recommended"] = False

    # Set new recommended
    if to_mode == "train":
        option["is_recommended"] = True
        trains_block = going.get("trains") or {}
        trains_block["recommended"] = option
        # Also inject into items if not already present
        items = trains_block.get("items") or []
        if option not in items:
            items.insert(0, option)
        trains_block["items"] = items
        going["trains"] = trains_block

    elif to_mode == "bus":
        option["is_recommended"] = True
        buses_block = going.get("buses") or {}
        buses_block["recommended"] = option
        items = buses_block.get("items") or []
        if option not in items:
            items.insert(0, option)
        buses_block["items"] = items
        going["buses"] = buses_block

    elif to_mode == "flight":
        option["is_recommended"] = True
        flights_block = going.get("flights") or {}
        flights_block["recommended"] = option
        going["flights"] = flights_block

    elif to_mode == "train_journey":
        # Complete all-train door-to-door route replaces the planned transport.
        option["is_recommended"] = True
        going["train_journey"] = option

    merged["going"] = going

    # Update budget_breakdown transport cost to reflect the new mode
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]

    breakdown = merged.get("budget_breakdown") or {}
    old_transport = breakdown.get("transport") or 0
    travelers = merged.get("travelers") or 1

    if to_mode == "train":
        new_pp = option.get("cheapest_fare") or 0
    elif to_mode == "bus":
        new_pp = option.get("fare_discounted") or option.get("fare_original") or 0
    elif to_mode == "flight":
        new_pp = option.get("price_total") or 0
    elif to_mode == "train_journey":
        # Bundle fare already sums every intercity segment by train.
        new_pp = option.get("total_fare_per_person") or 0
    else:
        new_pp = 0

    new_transport_total = new_pp * travelers
    if new_transport_total > 0:
        # Recompute the whole breakdown from the new transport so every bucket
        # stays consistent — misc is a 10% buffer on transport+stay, so it must
        # be recomputed too (not left at the old, higher flight-based value).
        stay = breakdown.get("stay") or 0
        food = breakdown.get("food") or 0
        acts = breakdown.get("activities") or 0
        breakdown["transport"]  = new_transport_total
        breakdown["misc"]       = round((new_transport_total + stay) * 0.10)
        breakdown["total"]      = new_transport_total + stay + food + acts + breakdown["misc"]
        breakdown["per_person"] = breakdown["total"] // max(travelers, 1)
        merged["budget_breakdown"] = breakdown

        # Refresh budget_status so total_generated / fits_budget / overspend
        # reflect the post-intervention total instead of the stale pre-switch one.
        bs = merged.get("budget_status") or {}
        if bs:
            total_ceiling = bs.get("total_ceiling") or 0
            new_total = breakdown["total"]
            bs["total_generated"] = new_total
            bs["fits_budget"]     = new_total <= total_ceiling
            bs["overspend"]       = max(0, new_total - total_ceiling)
            # The pre-switch suggestions referenced the old (higher) total — keep
            # only the applied switch and regenerate reduce_scope against the new
            # total so no suggestion contradicts the headline figure.
            kept = [
                s for s in (bs.get("suggestions") or [])
                if s.get("type") in ("switch_transport", "switch_to_train_journey")
            ]
            if total_ceiling and new_total > total_ceiling * 1.5:
                pct = int((new_total / total_ceiling - 1) * 100)
                kept.append({
                    "type": "reduce_scope",
                    "message": (
                        f"Trip at {symbol}{new_total:,} is {pct}% over budget. "
                        f"Consider fewer days or reducing party size."
                    ),
                })
            bs["suggestions"] = kept
            merged["budget_status"] = bs

    _label = "an all-train route" if to_mode == "train_journey" else to_mode
    merged["supervisor_action"] = {
        "type": "budget_intervention",
        "applied": True,
        "switched_to": to_mode,
        "saving_total": saving,
        "message": msg,
        "note": (
            f"Trip was over budget. Supervisor switched the recommended transport to "
            f"{_label} to save {symbol}{saving:,}."
        ),
    }

    logger.info(
        "[supervisor] Intervention applied: switched to %s, saves %s%s",
        to_mode, symbol, f"{saving:,}",
    )
    return merged


def _assemble_costs(merged: dict) -> dict:
    """
    Collapse the four overlapping cost blocks (cost_breakdown, budget_breakdown,
    budget_status, supervisor_action) into a single canonical `costs` block, then
    drop the originals. One place for every cost number — and they all agree
    because they're derived from the same reconciled budget_breakdown.
    """
    bb = merged.pop("budget_breakdown", {}) or {}
    bs = merged.pop("budget_status", {}) or {}
    sa = merged.pop("supervisor_action", None)
    cb = merged.pop("cost_breakdown", {}) or {}

    costs = {
        "currency": bb.get("currency") or bs.get("currency"),
        "currency_code": bb.get("currency_code"),
        "total": bb.get("total", 0),
        "per_person": bb.get("per_person", 0),
        "travelers": bb.get("travelers"),
        "nights": bb.get("nights"),
        "hotel_per_night": bb.get("hotel_per_night"),
        "breakdown": {
            "transport":  bb.get("transport", 0),
            "stay":       bb.get("stay", 0),
            "food":       bb.get("food", 0),
            "activities": bb.get("activities", 0),
            "misc":       bb.get("misc", 0),
        },
        "budget": {
            "tier":               merged.get("budget"),
            "ceiling_per_person": bs.get("ceiling_per_person"),
            "ceiling_total":      bs.get("total_ceiling"),
            "fits":               bs.get("fits_budget", True),
            "overspend":          bs.get("overspend", 0),
        },
        "suggestions": bs.get("suggestions") or [],
        "intervention": sa,
        # Per-leg live fare detail (e.g. leg4_flight, hotel_total) — kept for drill-down.
        "route_breakdown": cb.get("breakdown") or {},
        "source": bb.get("source", "live+estimated"),
    }
    merged["costs"] = costs
    return merged


# ── Main supervisor entry point ────────────────────────────────────────────────

async def _phase_wrap(tag: str, coro, on_phase):
    """
    Await an agent coroutine and fire the on_phase callback the moment it
    finishes — this is what lets the streaming pipeline emit transport/itinerary
    patches while the other agents are still running. Exceptions are returned
    (not raised) to mirror gather(return_exceptions=True).
    """
    try:
        res = await coro
    except Exception as e:  # noqa: BLE001 — mirrored into results like gather does
        if on_phase:
            try:
                await on_phase(tag, None, e)
            except Exception as cb_err:  # noqa: BLE001 — callback must never kill the plan
                logger.warning("[supervisor] on_phase(%s) failed: %s", tag, cb_err)
        return e
    if on_phase:
        try:
            await on_phase(tag, res, None)
        except Exception as cb_err:  # noqa: BLE001
            logger.warning("[supervisor] on_phase(%s) failed: %s", tag, cb_err)
    return res


async def supervisor_run(body: PlanRequest, user_id: str, on_phase=None) -> dict:
    """
    Full plan orchestration with budget supervision.

    Drop-in replacement for run_plan() in plan.py.
    Adds a budget-intervention layer between merge and return.

    on_phase: optional async callback (tag, result, error) fired as each agent
    completes — tags: "journey", "itinerary", "return_journey". Used by the
    Trip State streaming pipeline; blocking callers just omit it.
    """
    stay_nights = max(1, body.days - 1)   # e.g. 4-day trip → 3 hotel nights

    end_date = None
    return_date = None
    if body.departure_date:
        try:
            dep = datetime.fromisoformat(body.departure_date)
            end_date   = (dep + timedelta(days=body.days)).strftime("%Y-%m-%d")
            return_date = end_date   # return on the last day of the trip
        except Exception:
            pass

    journey_req = PlanJourneyRequest(
        origin=body.origin,
        destination=body.destination,
        transport_modes=[],          # always let Gemini pick the best mode
        travelers=body.travelers,
        budget=body.budget,
        departure_date=body.departure_date,
        stay_nights=stay_nights,
    )
    trip_req = PlanTripRequest(
        destination=body.destination,
        days=body.days,
        budget=body.budget,
        travelers=body.travelers,
        interests=body.interests,
        start_date=body.departure_date,
        end_date=end_date,
    )
    return_journey_req = PlanJourneyRequest(
        origin=body.destination,
        destination=body.origin,
        transport_modes=[],
        travelers=body.travelers,
        budget=body.budget,
        departure_date=return_date,
        stay_nights=0,
    ) if return_date else None

    # ── Phase 1: Fetch shared profile once, then run all agents in parallel ──────
    trace = RequestTrace(f"supervisor_{user_id[:8]}")
    # get_db() inside the DB executor thread — clients must not cross threads.
    shared_profile = await run_db(lambda: fetch_user_profile(get_db(), user_id))

    coros = [
        _phase_wrap("journey", run_plan_journey(journey_req, user_id, profile=shared_profile), on_phase),
        _phase_wrap("itinerary", run_plan_trip(trip_req, user_id, trace=trace, profile=shared_profile), on_phase),
    ]
    if return_journey_req:
        coros.append(_phase_wrap(
            "return_journey",
            run_plan_journey(return_journey_req, user_id, profile=shared_profile),
            on_phase,
        ))

    logger.info(
        "[supervisor] %s → %s (%dd, %s budget, %d travelers) — launching agents",
        body.origin, body.destination, body.days, body.budget, body.travelers,
    )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*coros, return_exceptions=True),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        logger.error("[supervisor] Timeout for %s → %s", body.origin, body.destination)
        results = [TimeoutError("Plan timed out")] * len(coros)

    journey_result       = results[0]
    trip_result          = results[1]
    return_journey_result = results[2] if return_journey_req else None

    # ── Phase 2: Merge ────────────────────────────────────────────────────────
    merged: dict = {}

    if trip_result and not isinstance(trip_result, Exception):
        for key in ("trip_name", "destination", "destination_description",
                    "cover_photo_ref", "days",
                    "trip_intelligence", "currency"):
            if key in trip_result:
                merged[key] = trip_result[key]
    else:
        merged["destination"] = body.destination
        if isinstance(trip_result, Exception):
            merged["itinerary_error"] = str(trip_result)

    if journey_result and not isinstance(journey_result, Exception):
        merged["going"] = _shape_journey(journey_result, include_hotels=True)
        for key in ("journey_summary", "total_distance_km", "total_duration_approx",
                    "cost_breakdown", "best_time_to_depart", "packing_tips",
                    "is_international", "visa_required", "visa_info",
                    "documents_needed", "health_requirements", "currency_info"):
            if key in journey_result:
                merged[key] = journey_result[key]
    else:
        if isinstance(journey_result, Exception):
            merged["journey_error"] = str(journey_result)

    merged.update({
        "origin": body.origin,
        "departure_date": body.departure_date,
        "return_date": return_date,
        "num_days": body.days,
        "travelers": body.travelers,
        "budget": body.budget,
        "stay_nights": stay_nights,
    })

    if return_journey_result and not isinstance(return_journey_result, Exception):
        merged["return"] = _shape_journey(return_journey_result, include_hotels=False)
    elif isinstance(return_journey_result, Exception):
        merged["return_error"] = str(return_journey_result)

    if "trip_intelligence" in merged and isinstance(merged["trip_intelligence"], dict):
        merged["trip_intelligence"].pop("total_estimated_cost", None)

    currency_symbol = (trip_result.get("currency") or "₹") if (
        trip_result and not isinstance(trip_result, Exception)
    ) else "₹"
    currency_code = _SYM_TO_CODE.get(currency_symbol, "inr")

    j_ok = journey_result and not isinstance(journey_result, Exception)
    t_ok = trip_result and not isinstance(trip_result, Exception)

    merged["budget_breakdown"] = compute_detailed_budget(
        journey_result=journey_result if j_ok else None,
        itinerary_result=trip_result if t_ok else None,
        budget=body.budget,
        travelers=body.travelers,
        stay_nights=stay_nights,
        currency_code=currency_code,
    )

    # ── Phase 3: Budget Agent ─────────────────────────────────────────────────
    merged = budget_agent_check(merged, body.budget, body.travelers, currency_code)

    budget_status = merged.get("budget_status") or {}
    intervention  = budget_status.get("intervention")

    if intervention:
        logger.info(
            "[supervisor] Budget over by %s — applying %s intervention",
            budget_status.get("overspend", 0),
            intervention.get("to_mode"),
        )
        merged = _apply_intervention(merged, intervention, currency_code)
    elif not budget_status.get("fits_budget"):
        logger.info(
            "[supervisor] Over budget by %s, no auto-fix available",
            budget_status.get("overspend", 0),
        )

    # ── Phase 3.5: Collapse all cost blocks into one canonical `costs` ────────
    merged = _assemble_costs(merged)

    # ── Phase 4: Constraint Solver ────────────────────────────────────────────
    merged = validate_trip(merged, body.budget, currency_code, body.travelers)

    logger.info(
        "[supervisor] %s → %s | Journey:%s Itinerary:%s | Budget:%s | Constraints:%s | Intervention:%s",
        body.origin, body.destination,
        "✓" if j_ok else "✗",
        "✓" if t_ok else "✗",
        "✓" if budget_status.get("fits_budget") else "⚠",
        "✓" if (merged.get("trip_validation") or {}).get("valid") else "⚠",
        "applied" if intervention else "none",
    )

    # ── Phase 5: DB writes — save trip synchronously to get trip_id ─────────────
    try:
        itinerary_data = {
            k: merged[k]
            for k in ("trip_name", "destination", "destination_description",
                      "cover_photo_ref", "days",
                      "trip_intelligence", "currency")
            if k in merged
        }

        # Client-generated id → idempotent upsert, so the resilient retry can
        # re-send through a TLS-corruption storm without duplicating the trip.
        trip_id = str(uuid4())
        await run_db_resilient(lambda: (
            get_db().table("trips").upsert({
                "id": trip_id,
                "user_id": user_id,
                "title": merged.get("trip_name") or body.destination,
                "destination": body.destination,
                "start_date": body.departure_date or None,
                "end_date": end_date or None,
                "budget": None,
                "currency": merged.get("currency") or "INR",
                "itinerary_data": itinerary_data,
                "is_public": False,
            }, on_conflict="id").execute()
        ), label="supervisor.trips")

        merged["trip_id"] = trip_id
        spawn_background(
            _save_trip_journey(user_id, body, merged, end_date, trip_id),
            name=f"save_trip_journey:{trip_id}",
        )

    except Exception as e:
        logger.warning("[supervisor] Failed to auto-save trip: %s", e)

    return merged


async def _save_trip_journey(user_id: str, body: PlanRequest, merged: dict, end_date: str | None, trip_id: str) -> None:
    try:
        def _to_numeric(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        # The response now carries a single `costs` block. Decompose it back into
        # the existing trip_journey columns so no DB migration is required.
        costs = merged.get("costs") or {}
        costs_budget = costs.get("budget") or {}
        budget_breakdown = {
            **(costs.get("breakdown") or {}),
            "total": costs.get("total"),
            "per_person": costs.get("per_person"),
            "currency": costs.get("currency"),
        }
        budget_status = {
            **costs_budget,
            "suggestions": costs.get("suggestions") or [],
            "intervention": costs.get("intervention"),
        }

        journey_id = str(uuid4())
        await run_db_resilient(lambda: (
            get_db().table("trip_journey").upsert({
                "id": journey_id,
                "trip_id": trip_id,
                "user_id": user_id,
                "origin": body.origin,
                "destination": body.destination,
                "departure_date": body.departure_date or None,
                "return_date": merged.get("return_date") or None,
                "num_days": body.days,
                "travelers": body.travelers,
                "budget": body.budget,
                "stay_nights": merged.get("stay_nights"),
                "going": merged.get("going") or {},
                "return_journey": merged.get("return") or {},
                "journey_summary": merged.get("journey_summary"),
                "total_distance_km": _to_numeric(merged.get("total_distance_km")),
                "total_duration_approx": merged.get("total_duration_approx"),
                "total_estimated_cost": _to_numeric(costs.get("total")),
                "cost_per_person": _to_numeric(costs.get("per_person")),
                "cost_for_all_travelers": _to_numeric(costs.get("total")),
                "cost_breakdown": costs,
                "budget_breakdown": budget_breakdown,
                "best_time_to_depart": merged.get("best_time_to_depart"),
                "packing_tips": merged.get("packing_tips") or [],
                "is_international": bool(merged.get("is_international", False)),
                "visa_required": bool(merged.get("visa_required", False)),
                "visa_info": merged.get("visa_info"),
                "documents_needed": merged.get("documents_needed") or [],
                "health_requirements": merged.get("health_requirements") or [],
                "currency_info": merged.get("currency_info") or {},
                "supervisor_action": costs.get("intervention"),
                "budget_status": budget_status,
                "trip_validation": merged.get("trip_validation"),
            }, on_conflict="id").execute()
        ), label="supervisor.trip_journey")

    except Exception as e:
        logger.warning("[supervisor] Failed to auto-save trip: %s", e)
