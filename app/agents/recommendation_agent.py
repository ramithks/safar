"""
Recommendation agent.

Given the raw flight/hotel results from the quick-search aggregators, this asks
Gemini to act as a travel booking assistant: pick the single best option for the
traveller, explain *why* in plain language, list a few concrete pros, and name a
runner-up. Powers the /search/flights/smart and /search/hotels/smart endpoints.

The agent is advisory only — it never invents options. It chooses an index from
the candidates we pass in, and we map that back to the real result server-side.
On any model/parse failure it returns None so the caller can fall back to the
deterministic (cheapest/best-by-sort) recommendation.
"""
import json
import logging

from app.services.vertex import vertex_generate, extract_json

logger = logging.getLogger(__name__)

# Cap candidates sent to the model — keeps the prompt small and latency low.
# Candidates are already sorted (cheapest/best first) so the prefix is the
# meaningful shortlist; the model picks within it and the index maps 1:1 back
# onto the full result list (a prefix preserves indices).
MAX_CANDIDATES = 8


def _compact_flight(f: dict) -> dict:
    return {
        "airline": f.get("airline"),
        "flight_number": f.get("flight_number"),
        "price": f.get("price_total"),
        "duration_min": f.get("duration_min"),
        "stops": f.get("stops", 0),
        "departure_time": f.get("departure_time"),
        "arrival_time": f.get("arrival_time"),
        "cabin_class": f.get("cabin_class"),
        # Richer signals the agent can reason over (provider-dependent; None when absent).
        "baggage_kg": f.get("baggage_kg"),
        "seats_left": f.get("seats_available"),       # scarcity → urgency
        "refundable": f.get("is_refundable"),
        "from_city": f.get("from_city"),
        "to_city": f.get("to_city"),
    }


def _compact_hotel(h: dict) -> dict:
    return {
        "name": h.get("name"),
        "price_per_night": h.get("price_per_night") or h.get("estimated_price_per_night"),
        "review_score": h.get("review_score") or h.get("ta_rating") or h.get("google_rating"),
        "star_rating": h.get("star_rating"),
        "review_count": h.get("review_count"),
        "review_label": h.get("review_label"),     # e.g. "Very Good"
        "property_type": h.get("property_type"),
        "free_cancellation": h.get("free_cancellation"),
        # Proximity — how close the stay is to the destination centre / landmark.
        "distance_km": h.get("distance_km"),
        "distance_note": h.get("distance_from_dest") or h.get("distance_from_centre"),
        "breakfast_included": bool(h.get("is_breakfast_included") or h.get("meal_plan")),
        "highlights": h.get("highlights"),          # e.g. "Couple Friendly"
        "amenities": (h.get("amenities") or [])[:8],
        "discount": h.get("discount"),
        "area": h.get("address") or h.get("city"),
    }


_SCHEMA_HINT = (
    'Respond with STRICT JSON only, no markdown, in this exact shape:\n'
    '{\n'
    '  "recommended_index": <int — index of the single best option>,\n'
    '  "reason": "<1-2 friendly sentences: concretely why this is the best pick for this traveller>",\n'
    '  "pros": ["<short phrase>", "<short phrase>", "<short phrase>"],\n'
    '  "best_for": "<who/what it suits, e.g. \'budget travellers\' or \'fastest arrival\'>",\n'
    '  "runner_up_index": <int or null>,\n'
    '  "runner_up_reason": "<short phrase, or empty string>"\n'
    '}\n'
)


async def _recommend(
    kind: str,
    candidates: list[dict],
    context: dict,
    intro: str,
) -> dict | None:
    """Shared LLM call for flights/hotels. Returns parsed recommendation or None."""
    if not candidates:
        return None

    numbered = [{"index": i, **c} for i, c in enumerate(candidates)]
    prompt = (
        f"You are TravBuddy's travel booking assistant. {intro}\n\n"
        f"Trip context: {json.dumps(context, ensure_ascii=False)}\n\n"
        f"Available {kind} options (0-indexed, already roughly sorted by value):\n"
        f"{json.dumps(numbered, ensure_ascii=False)}\n\n"
        "Pick the SINGLE best option for this specific traveller, balancing price, "
        "convenience, timing, and quality — not just the cheapest. Weigh the richer "
        "signals when present: for hotels prefer ones closer to the centre (distance_km), "
        "with strong review_score/review_count, free_cancellation, breakfast, and "
        "matching highlights/amenities; for flights weigh stops, baggage_kg, seats_left "
        "(scarcity), and refundable. Be concrete and reference real numbers from the "
        "option (price, duration, stops, rating, distance, baggage).\n\n"
        f"{_SCHEMA_HINT}"
    )

    try:
        raw = await vertex_generate(
            prompt,
            response_mime_type="application/json",
            temperature=0.3,
            max_output_tokens=512,
            timeout=30.0,
        )
        data = extract_json(raw)
    except Exception as e:  # noqa: BLE001 — advisory layer, never fatal
        logger.warning("[recommend] %s recommendation failed: %s", kind, str(e)[:160])
        return None

    idx = data.get("recommended_index")
    if not isinstance(idx, int) or not (0 <= idx < len(candidates)):
        logger.warning("[recommend] %s returned out-of-range index %r", kind, idx)
        return None

    ru = data.get("runner_up_index")
    if not (isinstance(ru, int) and 0 <= ru < len(candidates) and ru != idx):
        ru = None

    return {
        "recommended_index": idx,
        "reason": (data.get("reason") or "").strip(),
        "pros": [p for p in (data.get("pros") or []) if isinstance(p, str)][:3],
        "best_for": (data.get("best_for") or "").strip(),
        "runner_up_index": ru,
        "runner_up_reason": (data.get("runner_up_reason") or "").strip() if ru is not None else "",
    }


async def recommend_flights(flights: list[dict], context: dict) -> dict | None:
    """Ask the agent which flight to book and why. `flights` must be the sorted list."""
    candidates = [_compact_flight(f) for f in flights[:MAX_CANDIDATES]]
    return await _recommend(
        "flight",
        candidates,
        context,
        intro="A traveller is choosing a one-way flight.",
    )


async def recommend_hotels(hotels: list[dict], context: dict) -> dict | None:
    """Ask the agent which hotel to book and why. `hotels` must be the sorted list."""
    candidates = [_compact_hotel(h) for h in hotels[:MAX_CANDIDATES]]
    return await _recommend(
        "hotel",
        candidates,
        context,
        intro="A traveller is choosing where to stay.",
    )
