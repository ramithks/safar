"""
Route Agent — makes transport recommendations based on real live prices.

Problem solved:
  Gemini previously planned the route (and committed to a mode) BEFORE
  seeing actual prices. So for Delhi→Goa it might pick "flight" at ₹12,000
  when a train exists for ₹900 and the user's budget is "low".

Solution:
  After all live searches complete, this agent receives ALL real prices
  (flights, trains, buses) and asks Gemini to pick the best strategy.
  Gemini now sees: "Flight ₹12k, Train ₹900, budget ceiling ₹5k" and
  correctly recommends the train.

Integration: called at the end of run_plan_journey() after live data is in.
"""
import logging
from app.services.vertex import brain as vertex_brain, extract_json
from app.api.v1.plan_trip.helpers import CURRENCY_CONFIG, with_retry

logger = logging.getLogger(__name__)

_DECISION_PROMPT = """\
You are a Route Optimization Agent. Your job is to recommend the best transport \
strategy based on REAL live prices — not estimates.

Route: {origin} → {destination}
Budget tier: {budget} ({symbol})
Budget ceiling: {symbol}{ceiling}/person
Travelers: {travelers}

REAL TRANSPORT OPTIONS (live data):
{options_text}

TASK:
1. Compare all options against the budget ceiling of {symbol}{ceiling}/person.
2. Pick the best mode: prioritize budget fit first, then comfort, then speed.
3. If even the cheapest option is over budget, flag it with budget_warning.
4. If a mode has no live data, do not recommend it.

Return ONLY valid JSON (no markdown):
{{
  "recommended_mode": "flight|train|bus|null",
  "reasoning": "2-3 sentences: specific prices, why this mode wins, trade-offs",
  "budget_warning": null,
  "total_cost_per_person": 0,
  "fits_budget": true
}}"""


def _format_flights(flights: list[dict], symbol: str) -> str:
    if not flights:
        return ""
    lines = ["✈ FLIGHTS (live):"]
    for f in flights[:3]:
        price = f.get("price_total") or 0
        dur = f.get("duration_min") or 0
        h, m = divmod(dur, 60)
        airline = f.get("airline") or "Flight"
        fn = f.get("flight_number") or ""
        dur_str = f"{h}h {m}m" if m else f"{h}h"
        lines.append(f"  {airline} {fn}: {symbol}{price:,}/person ({dur_str})")
    return "\n".join(lines)


def _format_trains(trains: list[dict], symbol: str) -> str:
    if not trains:
        return ""
    lines = ["🚂 TRAINS (live):"]
    for t in trains[:3]:
        price = t.get("cheapest_fare") or 0
        dur = t.get("duration_min") or 0
        h, m = divmod(dur, 60)
        name = t.get("train_name") or "Train"
        num = t.get("train_number") or ""
        dur_str = f"{h}h {m}m" if m else f"{h}h"
        lines.append(f"  {name} ({num}): {symbol}{price:,}/person ({dur_str})")
    return "\n".join(lines)


def _format_buses(buses: list[dict], symbol: str) -> str:
    if not buses:
        return ""
    lines = ["🚌 BUSES (live):"]
    for b in buses[:3]:
        price = b.get("fare_discounted") or b.get("fare_original") or 0
        operator = b.get("operator") or "Bus"
        bus_type = b.get("bus_type") or ""
        lines.append(f"  {operator} {bus_type}: {symbol}{price:,}/person".strip())
    return "\n".join(lines)


def _fallback_decision(
    flights: list[dict],
    trains: list[dict],
    buses: list[dict],
    ceiling: int,
    symbol: str,
    budget: str,
) -> dict:
    """Cheapest-wins fallback when the Gemini call fails."""
    candidates: list[tuple[int, str, dict]] = []
    for f in flights[:1]:
        candidates.append((f.get("price_total") or 999_999, "flight", f))
    for t in trains[:1]:
        candidates.append((t.get("cheapest_fare") or 999_999, "train", t))
    for b in buses[:1]:
        fare = b.get("fare_discounted") or b.get("fare_original") or 999_999
        candidates.append((fare, "bus", b))

    if not candidates:
        return {"recommended_mode": None, "reasoning": "No live data", "fits_budget": True, "total_cost_per_person": 0}

    best_price, best_mode, _ = min(candidates, key=lambda x: x[0])
    fits = best_price <= ceiling
    return {
        "recommended_mode": best_mode,
        "reasoning": f"Picked {best_mode} at {symbol}{best_price:,}/person (cheapest available option).",
        "fits_budget": fits,
        "total_cost_per_person": best_price,
        "budget_warning": (
            None if fits else
            f"{best_mode.capitalize()} at {symbol}{best_price:,} exceeds {budget} ceiling {symbol}{ceiling:,}/person"
        ),
    }


async def route_agent_decide(
    origin: str,
    destination: str,
    budget: str,
    travelers: int,
    currency_code: str,
    flights: list[dict],
    trains: list[dict],
    buses: list[dict],
) -> dict:
    """
    Given real live prices for all modes, ask Gemini to pick the best strategy.

    Returns a decision dict:
      {recommended_mode, reasoning, budget_warning, total_cost_per_person, fits_budget}
    """
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]
    ceiling = ci["ceiling"].get(budget, ci["ceiling"]["mid"])

    if not flights and not trains and not buses:
        return {"recommended_mode": None, "reasoning": "No live data available", "fits_budget": True, "total_cost_per_person": 0}

    option_parts = [
        p for p in [
            _format_flights(flights, symbol),
            _format_trains(trains, symbol),
            _format_buses(buses, symbol),
        ] if p
    ]
    options_text = "\n\n".join(option_parts) or "No live options found."

    prompt = _DECISION_PROMPT.format(
        origin=origin,
        destination=destination,
        budget=budget,
        symbol=symbol,
        ceiling=f"{ceiling:,}",
        travelers=travelers,
        options_text=options_text,
    )

    try:
        raw = await with_retry(
            vertex_brain, prompt,
            response_mime_type="application/json",
            timeout=30.0,
            fallback=None,
        )
        if raw:
            decision = extract_json(raw)
            logger.info(
                "[route_agent] %s → %s: recommends %s (fits_budget=%s)",
                origin, destination,
                decision.get("recommended_mode"),
                decision.get("fits_budget"),
            )
            return decision
    except Exception as e:
        logger.warning("[route_agent] Decision call failed, using fallback: %s", e)

    return _fallback_decision(flights, trains, buses, ceiling, symbol, budget)
