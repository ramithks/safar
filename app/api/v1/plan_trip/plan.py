"""
POST /api/v1/plan — unified trip planner.

Runs plan-journey (transport + hotels) and plan-trip (day-by-day itinerary)
in parallel, then merges into a single response.

plan-journey provides:
  - legs with live bus/train/flight prices
  - destination_hotels (nearby, from EaseMyTrip + Booking.com + Expedia)
  - flight/train alternatives
  - recommendations (best bus/hotel/flight pick)
  - cost_breakdown from live prices

plan-trip provides:
  - day-by-day activity itinerary (Gemini + Google Places hydration)
  - trip_name, destination_description
  - trip_intelligence (pace, budget_fit_score, etc.)
"""
import asyncio
from datetime import datetime, timedelta

from .models import PlanRequest, PlanJourneyRequest, PlanTripRequest
from .journey import run_plan_journey
from .orchestrator import run_plan_trip
from app.core.timing import RequestTrace


async def run_plan(body: PlanRequest, user_id: str) -> dict:
    stay_nights = body.stay_nights if body.stay_nights is not None else max(1, body.days - 1)

    # Compute end_date for plan-trip
    end_date = None
    if body.departure_date:
        try:
            end_date = (
                datetime.fromisoformat(body.departure_date) + timedelta(days=body.days)
            ).strftime("%Y-%m-%d")
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

    # Run both in parallel
    trace = RequestTrace(f"plan_{user_id[:8]}")
    journey_result, trip_result = await asyncio.gather(
        run_plan_journey(journey_req, user_id),
        run_plan_trip(trip_req, user_id, trace=trace),
        return_exceptions=True,
    )

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
        for key in ("journey_summary", "total_distance_km", "total_duration_approx",
                    "total_estimated_cost", "cost_per_person", "cost_for_all_travelers",
                    "cost_breakdown", "best_time_to_depart", "packing_tips",
                    "legs", "flight_alternatives", "train_alternatives",
                    "destination_hotels", "recommendations",
                    "has_real_data", "duration_source",
                    "is_international", "visa_required", "visa_info",
                    "documents_needed", "health_requirements", "currency_info"):
            if key in journey_result:
                merged[key] = journey_result[key]
    else:
        if isinstance(journey_result, Exception):
            merged["journey_error"] = str(journey_result)

    merged["origin"]       = body.origin
    merged["departure_date"] = body.departure_date
    merged["num_days"]     = body.days           # integer — don't overwrite itinerary "days" list
    merged["travelers"]    = body.travelers
    merged["budget"]       = body.budget
    merged["stay_nights"]  = stay_nights

    # Remove activity-level estimated cost from trip_intelligence
    # — use the live total_estimated_cost from journey instead
    if "trip_intelligence" in merged and isinstance(merged["trip_intelligence"], dict):
        merged["trip_intelligence"].pop("total_estimated_cost", None)

    print(
        f"\n[plan] ── {body.origin} → {body.destination} ({body.days}d) ──\n"
        f"  Journey: {'✓' if journey_result and not isinstance(journey_result, Exception) else '✗'} | "
        f"Itinerary: {'✓' if trip_result and not isinstance(trip_result, Exception) else '✗'}"
    )

    return merged
