"""
Budget Agent — validates total trip cost against the user's budget ceiling
and surfaces the cheapest viable alternative when the plan is over budget.

Called after run_plan() merges journey + itinerary results.

What it does:
  1. Computes the budget ceiling for the requested tier (low/mid/high).
  2. Compares against budget_breakdown.total from the merged result.
  3. If over budget: finds the cheapest mode already fetched (flights/trains/buses)
     and surfaces an "intervention" the frontend can highlight.
  4. Adds budget_status to merged — never removes data, never blocks the response.

Example:
  budget=low, ceiling=₹5,000/person, 2 travelers → total_ceiling=₹10,000
  Generated trip=₹18,000 (flight ₹12k + hotel ₹6k)
  Agent finds train at ₹900 → suggests switching, saves ₹11,100 → fits budget
"""
import logging
from app.api.v1.plan_trip.helpers import CURRENCY_CONFIG

logger = logging.getLogger(__name__)


def _planned_transport_pp(merged: dict) -> int:
    """
    Per-person transport cost of the *complete* planned journey — the sum of the
    cheapest fare on every intercity leg (cost_breakdown.transport_per_person).

    This is the only honest "current transport" figure for a multi-leg journey:
    e.g. Bhiwani→Delhi train (₹150) + Delhi→Goa flight (₹26,532) = ₹26,682. The
    old code returned a single leg's fare, which let the budget agent compare a
    cheap feeder against the whole journey.
    """
    cb = merged.get("cost_breakdown") or {}
    tp = cb.get("transport_per_person")
    if tp:
        return tp

    # Fallback for legacy single-leg journeys with no cost_breakdown.
    going = merged.get("going") or {}
    rec_flight = (going.get("flights") or {}).get("recommended") or {}
    if rec_flight.get("price_total"):
        return rec_flight["price_total"]
    rec_train = (going.get("trains") or {}).get("recommended") or {}
    if rec_train.get("cheapest_fare"):
        return rec_train["cheapest_fare"]
    rec_bus = (going.get("buses") or {}).get("recommended") or {}
    return rec_bus.get("fare_discounted") or rec_bus.get("fare_original") or 0


def _train_journey_alternative(going: dict, current_pp: int) -> tuple[int, dict] | None:
    """
    The only valid cheaper alternative is a *complete* door-to-door journey, not a
    single leg. We use the all-train bundle (every intercity segment by train),
    which is already stitched and feasibility-checked upstream.

    Returns (per_person_fare, bundle) when the bundle is feasible and genuinely
    cheaper than the planned journey; otherwise None (so no fake swap is offered).
    """
    bundle = going.get("train_journey") or {}
    if not bundle.get("feasible"):
        return None
    pp = bundle.get("total_fare_per_person") or 0
    if pp <= 0 or pp >= current_pp:
        return None
    return pp, bundle


def budget_agent_check(
    merged: dict,
    budget: str,
    travelers: int,
    currency_code: str,
) -> dict:
    """
    Validate merged trip cost; attach budget_status block.
    Mutates merged in-place. Always returns merged (never raises).
    """
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]
    safe_budget = budget if budget in ("low", "mid", "high") else "mid"
    ceiling_pp = ci["ceiling"].get(safe_budget, ci["ceiling"]["mid"])
    total_ceiling = ceiling_pp * travelers

    breakdown = merged.get("budget_breakdown") or {}
    total = breakdown.get("total") or 0

    status: dict = {
        "ceiling_per_person": ceiling_pp,
        "total_ceiling": total_ceiling,
        "total_generated": total,
        "currency": symbol,
        "fits_budget": total <= total_ceiling,
        "overspend": max(0, total - total_ceiling),
        "suggestions": [],
        "intervention": None,
    }

    if total > total_ceiling:
        overspend = total - total_ceiling
        going = merged.get("going") or {}
        current_pp = _planned_transport_pp(merged)
        alt = _train_journey_alternative(going, current_pp)

        # Only suggest a switch to a complete cheaper journey (all-train bundle),
        # never a single feeder leg. saving = full planned transport − all-train.
        if alt:
            alt_pp, bundle = alt
            saving_pp = current_pp - alt_pp
            saving_total = saving_pp * travelers
            suggestion = {
                "type": "switch_to_train_journey",
                "to_mode": "train_journey",
                "option": bundle,
                "saving_per_person": saving_pp,
                "saving_total": saving_total,
                "message": (
                    f"Take the all-train route ({symbol}{alt_pp:,}/person) instead of the "
                    f"current {symbol}{current_pp:,}/person journey — saves {symbol}{saving_total:,} total."
                ),
            }
            status["suggestions"].append(suggestion)

            # Promote to intervention when it covers ≥60% of overspend
            if saving_total >= overspend * 0.60:
                status["intervention"] = suggestion
                logger.info(
                    "[budget_agent] %s→ Over budget by %s%s — recommending all-train route, saves %s%s",
                    merged.get("destination", "?"),
                    symbol, f"{overspend:,}",
                    symbol, f"{saving_total:,}",
                )

        # Generic reduce-scope suggestion when heavily over budget
        if total > total_ceiling * 1.5:
            status["suggestions"].append({
                "type": "reduce_scope",
                "message": (
                    f"Trip at {symbol}{total:,} is "
                    f"{int((total / total_ceiling - 1) * 100)}% over {safe_budget} budget. "
                    f"Consider fewer days or reducing party size."
                ),
            })

    if not status["fits_budget"] and not status["intervention"]:
        logger.info(
            "[budget_agent] %s→ Over budget by %s%s — no viable auto-fix",
            merged.get("destination", "?"),
            symbol, f"{status['overspend']:,}",
        )

    merged["budget_status"] = status
    return merged
