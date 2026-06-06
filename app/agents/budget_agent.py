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


def _cheapest_from_going(going: dict) -> tuple[str | None, int, dict | None]:
    """
    Scan all transport blocks in the going journey and return
    (mode, cheapest_price_per_person, item) across flights/trains/buses.
    """
    best_price = 999_999
    best_mode: str | None = None
    best_item: dict | None = None

    for f in (going.get("flights") or {}).get("items") or []:
        p = f.get("price_total") or 0
        if 0 < p < best_price:
            best_price, best_mode, best_item = p, "flight", f

    for t in (going.get("trains") or {}).get("items") or []:
        p = t.get("cheapest_fare") or 0
        if 0 < p < best_price:
            best_price, best_mode, best_item = p, "train", t

    for b in (going.get("buses") or {}).get("items") or []:
        p = b.get("fare_discounted") or b.get("fare_original") or 0
        if 0 < p < best_price:
            best_price, best_mode, best_item = p, "bus", b

    return best_mode, (best_price if best_mode else 0), best_item


def _current_transport_cost_pp(going: dict) -> int:
    """Extract per-person cost of the currently recommended transport."""
    rec_flight = (going.get("flights") or {}).get("recommended") or {}
    if rec_flight.get("price_total"):
        return rec_flight["price_total"]

    rec_train = (going.get("trains") or {}).get("recommended") or {}
    if rec_train.get("cheapest_fare"):
        return rec_train["cheapest_fare"]

    rec_bus = (going.get("buses") or {}).get("recommended") or {}
    fare = rec_bus.get("fare_discounted") or rec_bus.get("fare_original") or 0
    return fare


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
        current_pp = _current_transport_cost_pp(going)
        alt_mode, alt_price_pp, alt_item = _cheapest_from_going(going)

        # Only suggest a switch when the alternative is cheaper than current
        if alt_item and alt_mode and alt_price_pp < current_pp:
            saving_pp = current_pp - alt_price_pp
            saving_total = saving_pp * travelers
            suggestion = {
                "type": "switch_transport",
                "to_mode": alt_mode,
                "option": alt_item,
                "saving_per_person": saving_pp,
                "saving_total": saving_total,
                "message": (
                    f"Switch to {alt_mode} ({symbol}{alt_price_pp:,}/person) "
                    f"instead of current transport ({symbol}{current_pp:,}/person). "
                    f"Saves {symbol}{saving_total:,} total."
                ),
            }
            status["suggestions"].append(suggestion)

            # Promote to intervention when it covers ≥60% of overspend
            if saving_total >= overspend * 0.60:
                status["intervention"] = suggestion
                logger.info(
                    "[budget_agent] %s→ Over budget by %s%s — recommending %s, saves %s%s",
                    merged.get("destination", "?"),
                    symbol, f"{overspend:,}",
                    alt_mode,
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
