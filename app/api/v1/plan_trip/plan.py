"""
POST /api/v1/plan — unified trip planner.

Runs plan-journey (transport + hotels) and plan-trip (day-by-day itinerary)
in parallel, then merges into a single response.

plan-journey provides:
  - legs with live bus/train/flight prices
  - destination_hotels (nearby, from EaseMyTrip + Booking.com)
  - flight/train alternatives
  - recommendations (best bus/hotel/flight pick)
  - cost_breakdown from live prices

plan-trip provides:
  - day-by-day activity itinerary (Gemini + Google Places hydration)
  - trip_name, destination_description
  - trip_intelligence (pace, budget_fit_score, etc.)
"""
import asyncio
import logging
from datetime import datetime, timedelta

from .models import PlanRequest, PlanJourneyRequest, PlanTripRequest
from .journey import run_plan_journey
from .orchestrator import run_plan_trip
from .budget import compute_detailed_budget
from app.core.timing import RequestTrace
from app.db.client import get_db

logger = logging.getLogger(__name__)


def _collect_bus_items(legs: list[dict], rec: dict | None) -> dict:
    """Aggregate all bus results from bus-mode legs that have live data."""
    items: list[dict] = []
    for leg in legs:
        if leg.get("mode", "").lower() == "bus" and leg.get("has_live_data"):
            items.extend((leg.get("live_data") or {}).get("buses") or [])
    return {"items": items, "recommended": rec}


def _collect_train_items(legs: list[dict], rec: dict | None) -> dict | None:
    """
    Aggregate per-leg train results. Returns None if no train data at all.

    Includes both legs Gemini planned as "train" (live_data) and legs planned
    as bus/flight that also got a train alternative search (train_alternative)
    — so e.g. a Delhi→Guwahati flight leg still surfaces its train options
    instead of only the Bhiwani→Delhi segment.
    """
    seg: list[dict] = []
    for leg in legs:
        mode = leg.get("mode", "").lower()
        is_train_leg = mode in ("train", "railway")

        if is_train_leg and leg.get("has_live_data"):
            ld = leg.get("live_data") or {}
            sp = leg.get("search_params") or {}
            items = ld.get("trains") or []
            if items:
                seg.append({
                    "from": leg.get("from"),
                    "to": leg.get("to"),
                    "from_code": sp.get("from_station_code") or ld.get("from_code"),
                    "to_code": sp.get("to_station_code") or ld.get("to_code"),
                    "items": items,
                })
        elif not is_train_leg:
            alt = leg.get("train_alternative") or {}
            items = alt.get("trains") or []
            if items:
                seg.append({
                    "from": leg.get("from"),
                    "to": leg.get("to"),
                    "from_code": alt.get("from_code"),
                    "to_code": alt.get("to_code"),
                    "items": items,
                    "is_alternative": True,
                    "planned_mode": mode,
                })
    if not seg:
        return None
    if len(seg) == 1:
        return {"items": seg[0]["items"], "recommended": rec,
                "from_code": seg[0]["from_code"], "to_code": seg[0]["to_code"]}
    return {"legs": seg, "items": [], "recommended": rec,
            "from_code": seg[0]["from_code"], "to_code": seg[-1]["to_code"]}


def _collect_flight_items(legs: list[dict], rec: dict | None) -> dict | None:
    """Aggregate per-leg flight results. Returns None if no flight legs."""
    items: list[dict] = []
    for leg in legs:
        if leg.get("mode", "").lower() == "flight" and leg.get("has_live_data"):
            items.extend((leg.get("live_data") or {}).get("flights") or [])
    if not items and not rec:
        return None
    return {"items": items, "recommended": rec}


def _shape_journey(j: dict, include_hotels: bool = False) -> dict:
    """
    Reshape a run_plan_journey() result into the standardized going/return block.
    Only includes per-leg live data — no cross-mode "alternatives" that bypass Gemini's routing.
    """
    legs = j.get("legs") or []
    recs = j.get("recommendations") or {}

    bus_block    = _collect_bus_items(legs, recs.get("bus"))
    trains_block = _collect_train_items(legs, recs.get("train"))
    flights_block = _collect_flight_items(legs, recs.get("flight"))

    block: dict = {
        "legs": legs,
        "summary": {
            "duration": j.get("total_duration_approx"),
            "has_real_data": j.get("has_real_data", False),
        },
    }

    # Only include transport blocks that actually carry data — an empty
    # buses/trains/flights block is noise the frontend has to skip past.
    if bus_block.get("items") or bus_block.get("recommended"):
        block["buses"] = bus_block
    if trains_block:
        block["trains"] = trains_block
    if flights_block:
        block["flights"] = flights_block

    journey_options = j.get("journey_options") or {}
    if journey_options:
        block["journey_options"] = journey_options

    # Trade-off narrative (Score→Reason→Recommend) — surface it on the block so it
    # reaches the /plan response, not just the raw journey result.
    reasoning = j.get("journey_reasoning")
    if reasoning:
        block["reasoning"] = reasoning

    # Fold the all-train bundle in only when it's a feasible end-to-end option.
    train_journey = j.get("train_journey")
    if train_journey and train_journey.get("feasible"):
        block["train_journey"] = train_journey

    if include_hotels:
        dh = j.get("destination_hotels") or {}
        hotel_items = dh.get("hotels") or []
        if hotel_items or dh.get("top_pick") or recs.get("hotel"):
            block["hotels"] = {
                "destination": dh.get("destination"),
                "checkin": dh.get("checkin"),
                "checkout": dh.get("checkout"),
                "nights": dh.get("nights"),
                "source": dh.get("source"),
                "items": hotel_items,
                "recommended": dh.get("top_pick") or recs.get("hotel"),
            }

    return block


async def run_plan(body: PlanRequest, user_id: str) -> dict:
    stay_nights = body.stay_nights if body.stay_nights is not None else max(1, body.days - 1)

    end_date = None
    return_date = None
    if body.departure_date:
        try:
            dep = datetime.fromisoformat(body.departure_date)
            end_date    = (dep + timedelta(days=body.days)).strftime("%Y-%m-%d")
            return_date = end_date
        except Exception:
            pass

    journey_req = PlanJourneyRequest(
        origin=body.origin,
        destination=body.destination,
        transport_modes=body.transport_modes,
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

    # Return journey: origin/destination swapped, no hotel needed
    return_journey_req = PlanJourneyRequest(
        origin=body.destination,
        destination=body.origin,
        transport_modes=body.transport_modes,
        travelers=body.travelers,
        budget=body.budget,
        departure_date=return_date,
        stay_nights=0,
    ) if return_date else None

    # Run all in parallel
    trace = RequestTrace(f"plan_{user_id[:8]}")
    coros = [
        run_plan_journey(journey_req, user_id),
        run_plan_trip(trip_req, user_id, trace=trace),
    ]
    if return_journey_req:
        coros.append(run_plan_journey(return_journey_req, user_id))

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*coros, return_exceptions=True),
            timeout=180.0,
        )
    except asyncio.TimeoutError:
        logger.error(f"[plan] Timeout for {body.origin} → {body.destination}")
        results = [TimeoutError("Plan timed out")] * len(coros)
    journey_result = results[0]
    trip_result = results[1]
    return_journey_result = results[2] if return_journey_req else None

    # ── Merge results ──────────────────────────────────────────────────────────
    merged: dict = {}

    # Itinerary fields from plan-trip
    if trip_result and not isinstance(trip_result, Exception):
        for key in ("trip_name", "destination", "destination_description",
                    "destination_photo_ref", "image", "days",
                    "trip_intelligence", "currency"):
            if key in trip_result:
                merged[key] = trip_result[key]
    else:
        merged["destination"] = body.destination
        if isinstance(trip_result, Exception):
            merged["itinerary_error"] = str(trip_result)

    # Journey fields from plan-journey
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

    merged["origin"]         = body.origin
    merged["departure_date"] = body.departure_date
    merged["return_date"]    = return_date
    merged["num_days"]       = body.days
    merged["travelers"]      = body.travelers
    merged["budget"]         = body.budget
    merged["stay_nights"]    = stay_nights

    # Return journey transport (destination → origin)
    if return_journey_result and not isinstance(return_journey_result, Exception):
        merged["return"] = _shape_journey(return_journey_result, include_hotels=False)
    elif isinstance(return_journey_result, Exception):
        merged["return_error"] = str(return_journey_result)

    if "trip_intelligence" in merged and isinstance(merged["trip_intelligence"], dict):
        merged["trip_intelligence"].pop("total_estimated_cost", None)

    # ── Detailed budget breakdown ──────────────────────────────────────────────
    j_ok = journey_result and not isinstance(journey_result, Exception)
    t_ok = trip_result and not isinstance(trip_result, Exception)
    currency_code = (trip_result.get("currency") or "₹") if t_ok else "₹"
    # Map symbol back to code for budget engine
    _sym_to_code = {"₹": "inr", "$": "usd", "€": "eur", "£": "gbp", "¥": "jpy",
                    "A$": "aud", "AED": "aed", "S$": "sgd"}
    currency_code_str = _sym_to_code.get(currency_code, "inr")
    merged["budget_breakdown"] = compute_detailed_budget(
        journey_result=journey_result if j_ok else None,
        itinerary_result=trip_result if t_ok else None,
        budget=body.budget,
        travelers=body.travelers,
        stay_nights=stay_nights,
        currency_code=currency_code_str,
    )

    # ── Budget Agent: validate cost vs ceiling, surface cheaper alternatives ──
    from app.agents.budget_agent import budget_agent_check
    from app.api.v1.plan_trip.constraints import validate_trip

    merged = budget_agent_check(merged, body.budget, body.travelers, currency_code_str)

    # ── Constraint Solver: check timing, hotel, return conflicts ──────────────
    merged = validate_trip(merged, body.budget, currency_code_str, body.travelers)

    logger.info(
        "[plan] %s → %s (%dd) | Journey:%s Itinerary:%s Return:%s | Budget:%s Constraints:%s",
        body.origin, body.destination, body.days,
        "✓" if journey_result and not isinstance(journey_result, Exception) else "✗",
        "✓" if trip_result and not isinstance(trip_result, Exception) else "✗",
        "✓" if return_journey_result and not isinstance(return_journey_result, Exception)
        else ("n/a" if not return_journey_req else "✗"),
        "✓" if (merged.get("budget_status") or {}).get("fits_budget") else "⚠",
        "✓" if (merged.get("trip_validation") or {}).get("valid") else "⚠",
    )

    # ── Auto-save to trips table (fire-and-forget) ────────────────────────────
    asyncio.create_task(_save_trip(user_id, body, merged, end_date))

    return merged


def _to_numeric(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _hotel_master_rows(merged: dict, fallback_destination: str) -> list[dict]:
    """Map the live scraper hotels in going.hotels.items to hotels-table rows,
    deduped by (source, hotel_id)."""
    hotels_block = (merged.get("going") or {}).get("hotels") or {}
    destination = hotels_block.get("destination") or fallback_destination
    now = datetime.utcnow().isoformat()

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for h in hotels_block.get("items") or []:
        source, hotel_id, name = h.get("source"), h.get("hotel_id"), h.get("name")
        if not source or not hotel_id or not name:
            continue
        key = (str(source), str(hotel_id))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "source": str(source),
            "source_hotel_id": str(hotel_id),
            "name": name,
            "address": h.get("address") or None,
            "city": h.get("city") or destination,
            "destination": destination,
            "lat": _to_numeric(h.get("lat")),
            "lon": _to_numeric(h.get("lon")),
            "star_rating": _to_numeric(h.get("star_rating")),
            "review_score": _to_numeric(h.get("review_score")),
            "review_count": _to_int(h.get("review_count")),
            "review_label": h.get("review_label") or None,
            "property_type": h.get("property_type") or None,
            "amenities": h.get("amenities") if isinstance(h.get("amenities"), (list, dict)) else None,
            "photos": h.get("photos") if isinstance(h.get("photos"), (list, dict)) else None,
            "booking_url": h.get("booking_url") or None,
            "map_url": h.get("map_url") or None,
            "description": h.get("description") or None,
            "quality_score": _to_numeric(h.get("quality_score")),
            "last_seen_at": now,
            "updated_at": now,
        })
    return rows


async def _save_trip(user_id: str, body: PlanRequest, merged: dict, end_date: str | None) -> None:
    try:
        db = get_db()
        loop = asyncio.get_event_loop()

        # Slim itinerary_data: only the AI-generated day-by-day plan, not the full blob
        itinerary_data = {
            k: merged[k]
            for k in ("trip_name", "destination", "destination_description",
                      "destination_photo_ref", "image", "days",
                      "trip_intelligence", "currency")
            if k in merged
        }

        trip_row = await loop.run_in_executor(None, lambda: (
            db.table("trips").insert({
                "user_id": user_id,
                "title": merged.get("trip_name") or body.destination,
                "destination": body.destination,
                "start_date": body.departure_date or None,
                "end_date": end_date or None,
                "budget": None,
                "currency": merged.get("currency") or "INR",
                "itinerary_data": itinerary_data,
                "is_public": False,
            }).execute()
        ))

        trip_id = (trip_row.data or [{}])[0].get("id")
        if not trip_id:
            logger.warning("[plan] Auto-save: trips insert returned no id")
            return

        await loop.run_in_executor(None, lambda: (
            db.table("trip_journey").insert({
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
                "total_estimated_cost": _to_numeric(merged.get("total_estimated_cost")),
                "cost_per_person": _to_numeric(merged.get("cost_per_person")),
                "cost_for_all_travelers": _to_numeric(merged.get("cost_for_all_travelers")),
                "cost_breakdown": merged.get("cost_breakdown") or {},
                "budget_breakdown": merged.get("budget_breakdown") or {},
                "best_time_to_depart": merged.get("best_time_to_depart"),
                "packing_tips": merged.get("packing_tips") or [],
                "is_international": bool(merged.get("is_international", False)),
                "visa_required": bool(merged.get("visa_required", False)),
                "visa_info": merged.get("visa_info"),
                "documents_needed": merged.get("documents_needed") or [],
                "health_requirements": merged.get("health_requirements") or [],
                "currency_info": merged.get("currency_info") or {},
            }).execute()
        ))

        # Grow the hotels master table with whatever the live scrapers returned.
        # Prices stay in trip_journey (date-specific); this only stores identity.
        try:
            hotel_rows = _hotel_master_rows(merged, body.destination)
            if hotel_rows:
                await loop.run_in_executor(None, lambda: (
                    db.table("hotels")
                    .upsert(hotel_rows, on_conflict="source,source_hotel_id")
                    .execute()
                ))
                logger.info("[plan] Upserted %d hotels into master table", len(hotel_rows))
        except Exception as e:
            logger.warning("[plan] Hotel master upsert failed: %s", e)

    except Exception as e:
        logger.warning("[plan] Failed to auto-save trip: %s", e)
