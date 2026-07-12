"""
Copilot edit engine — turns a natural-language command (or a structured UI
action) into TripState patches.

Flow: command + state summary → edit-intent router (brain tier, structured
output) → typed intent → handler computes patches over data ALREADY in state
(no re-scraping, no regeneration) → patches persisted + returned.

Structured actions from the UI (e.g. tapping a ride on the journey board)
skip the LLM entirely and go straight to the handler — deterministic, instant.

The transport model is LEG-AWARE: a journey is a chain of legs
(auto → train → auto → bus → cab) and each bookable leg (train/bus/flight)
has its own options bucket. Picking a train and picking a bus are choices on
DIFFERENT legs of one route, not alternative routes.

Intents:
  answer_question         — answer anything about the plan, grounded in state.
  select_transport_option — pick a specific ride on one leg (mode + index).
  select_hotel            — pick a different hotel from the fetched candidates.
  search_hotels           — LIVE hotel re-search near a landmark/area with
                            optional canonical filters ("pet-friendly hostels
                            under ₹1500 near Mall Road") → replaces /stay/hotels.
  set_title               — rename the trip.
  unsupported             — graceful "here's what I can do" reply.
"""
import logging
from typing import Any

from app.services.hotels_filters import FACILITY_SYNONYMS, MEAL_SYNONYMS, PROPERTY_TYPES
from app.services.vertex import generate_json

from . import store
from .patch import PatchOp

logger = logging.getLogger(__name__)

_MODE_KEYS = {"flight": "flights", "train": "trains", "bus": "buses"}

# Canonical hotel-filter vocabulary for the search_hotels intent — enums come
# straight from hotels_filters so the router can never invent a key the
# backend drops. Values are sanitized again server-side before use.
_HOTEL_FILTERS_SCHEMA = {
    "type": "OBJECT",
    "nullable": True,
    "properties": {
        "property_types": {
            "type": "ARRAY", "nullable": True,
            "items": {"type": "STRING", "enum": sorted(PROPERTY_TYPES)},
        },
        "price_min": {"type": "NUMBER", "nullable": True},
        "price_max": {"type": "NUMBER", "nullable": True},
        "min_review_score": {"type": "NUMBER", "nullable": True},
        "star_ratings": {"type": "ARRAY", "nullable": True, "items": {"type": "INTEGER"}},
        "facilities": {
            "type": "ARRAY", "nullable": True,
            "items": {"type": "STRING", "enum": sorted(FACILITY_SYNONYMS)},
        },
        "meals": {
            "type": "ARRAY", "nullable": True,
            "items": {"type": "STRING", "enum": list(MEAL_SYNONYMS)},
        },
        "free_cancellation": {"type": "BOOLEAN", "nullable": True},
        "no_prepayment": {"type": "BOOLEAN", "nullable": True},
        "breakfast_included": {"type": "BOOLEAN", "nullable": True},
        "couple_friendly": {"type": "BOOLEAN", "nullable": True},
        "pet_friendly": {"type": "BOOLEAN", "nullable": True},
        "max_distance_km": {"type": "NUMBER", "nullable": True},
        "sort": {
            "type": "STRING", "nullable": True,
            "enum": ["price_asc", "price_desc", "recommended", "rating"],
        },
    },
}

_INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {
            "type": "STRING",
            "enum": [
                "answer_question",
                "select_transport_option",
                "select_hotel",
                "search_hotels",
                "add_activities",
                "add_waypoint",
                "set_title",
                "clarify",
                "unsupported",
            ],
        },
        "clarify_options": {
            "type": "ARRAY", "nullable": True,
            "items": {"type": "STRING"},
        },
        "direction": {"type": "STRING", "enum": ["going", "return"], "nullable": True},
        "mode": {"type": "STRING", "enum": ["flight", "train", "bus"], "nullable": True},
        "option_index": {"type": "INTEGER", "nullable": True},
        "prefer": {
            "type": "STRING", "nullable": True,
            "enum": ["cheapest", "fastest", "earliest", "latest", "most_baggage", "highest_rated"],
        },
        "hotel_index": {"type": "INTEGER", "nullable": True},
        "hotel_query": {"type": "STRING", "nullable": True},
        "hotel_filters": _HOTEL_FILTERS_SCHEMA,
        "day_index": {"type": "INTEGER", "nullable": True},
        "activity_query": {"type": "STRING", "nullable": True},
        "count": {"type": "INTEGER", "nullable": True},
        "waypoint_city": {"type": "STRING", "nullable": True},
        "waypoint_days": {"type": "INTEGER", "nullable": True},
        "title": {"type": "STRING", "nullable": True},
        "reply": {"type": "STRING"},
    },
    "required": ["intent", "reply"],
}


# ── Reading the state ───────────────────────────────────────────────────────────

def _journey(state: dict, direction: str) -> dict:
    transport = state.get("transport") or {}
    key = "going" if direction == "going" else "return_journey"
    return transport.get(key) or {}


def _fare_per_person(mode: str, option: dict) -> float:
    if mode == "train":
        return option.get("cheapest_fare") or 0
    if mode == "bus":
        return option.get("fare_discounted") or option.get("fare_original") or 0
    return option.get("price_total") or 0


def _option_name(mode: str, option: dict) -> str:
    return (
        option.get("train_name")
        or option.get("operator")
        or option.get("airline")
        or option.get("name")
        or mode
    )


def _clock(value: Any) -> str | None:
    """'2026-07-17 23:15:00' → '23:15'; '04:20' stays as-is."""
    if not isinstance(value, str) or not value:
        return None
    tail = value.split(" ")[-1]
    return tail[:5] if ":" in tail else value


def _option_times(option: dict) -> str:
    dep = _clock(option.get("departure_time") or option.get("departure"))
    arr = _clock(option.get("arrival_time") or option.get("arrival"))
    if dep and arr:
        return f"{dep}→{arr}"
    return dep or "?"


# ── Grounding summary for the intent router ─────────────────────────────────────

def _route_lines(state: dict, direction: str) -> list[str]:
    j = _journey(state, direction)
    legs = j.get("legs") or []
    if not legs:
        return []
    out = [f"{direction.upper()} route (ONE journey, legs in order):"]
    for i, leg in enumerate(legs):
        mode = (leg.get("mode") or "?").lower()
        out.append(f"  leg {i + 1} [{mode}] {leg.get('from')} → {leg.get('to')}")
    return out


def _baggage_kg(option: dict) -> float | None:
    for k in ("baggage_kg", "checked_baggage_kg", "free_baggage_kg"):
        v = option.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return v
    return None


def _extra_detail(mode: str, option: dict) -> str:
    """Axis-specific detail the router uses to honour 'more luggage', 'best
    rated', etc. — appended to each numbered option line."""
    bits: list[str] = []
    if mode == "flight":
        bag = _baggage_kg(option)
        bits.append(f"baggage {bag:g}kg" if bag else "baggage n/a")
        if option.get("stops") is not None:
            bits.append("non-stop" if not option.get("stops") else f"{option['stops']} stop")
    rating = option.get("rating") or option.get("review_score")
    if isinstance(rating, (int, float)) and rating > 0:
        bits.append(f"rated {rating:g}")
    return (" · " + " · ".join(bits)) if bits else ""


def _option_lines(state: dict, direction: str, per_mode: int = 10) -> list[str]:
    j = _journey(state, direction)
    out: list[str] = []
    for mode, key in _MODE_KEYS.items():
        items = (j.get(key) or {}).get("items") or []
        if not items:
            continue
        out.append(f"{mode} options for the {direction} journey (index: details):")
        for i, it in enumerate(items[:per_mode]):
            fare = _fare_per_person(mode, it)
            star = "  ← current pick" if it.get("is_recommended") else ""
            out.append(
                f"  [{i}] {_option_name(mode, it)} · {_option_times(it)} · "
                f"₹{round(fare)}/person{_extra_detail(mode, it)}{star}"
            )
        if len(items) > per_mode:
            out.append(f"  …and {len(items) - per_mode} more (indices up to {len(items) - 1})")
    return out


def _state_summary(state: dict) -> str:
    req = state.get("request") or {}
    costs = state.get("costs") or {}
    breakdown = costs.get("breakdown") or {}
    hotels = (state.get("stay") or {}).get("hotels") or []
    selected_hotel = ((state.get("stay") or {}).get("selected") or {}).get("name")
    days = (state.get("itinerary") or {}).get("days") or []

    lines = [
        f"Trip: {req.get('origin')} → {req.get('destination')}, "
        f"{req.get('days')} days ({req.get('departure_date')} to {req.get('return_date') or '?'}), "
        f"{req.get('travelers')} travelers, budget tier {req.get('budget_tier')}.",
    ]
    lines += _route_lines(state, "going")
    lines += _route_lines(state, "return")
    lines += _option_lines(state, "going")
    lines += _option_lines(state, "return", per_mode=5)

    if costs:
        lines.append(
            f"Costs: total ₹{costs.get('total')} (₹{costs.get('per_person')}/person) — "
            + ", ".join(f"{k} ₹{v}" for k, v in breakdown.items() if v)
        )
    if selected_hotel:
        lines.append(f"Selected hotel: {selected_hotel}")
    if hotels:
        lines.append("Hotel candidates (index: name — ₹/night):")
        for i, h in enumerate(hotels[:10]):
            price = h.get("price_per_night") or h.get("price") or "?"
            lines.append(f"  [{i}] {h.get('name')} — {price}")
    if days:
        lines.append("Itinerary:")
        for i, d in enumerate(days):
            title = d.get("title") or d.get("theme") or ""
            n = len(d.get("activities") or d.get("items") or [])
            lines.append(f"  Day {i + 1}: {title} ({n} activities)")

    weather = state.get("weather") or {}
    if weather.get("daily"):
        header = (
            "Weather at the destination RIGHT NOW (trip is too far out for a forecast)"
            if weather.get("is_preview")
            else "Weather forecast for the trip dates"
        )
        lines.append(f"{header} — {weather.get('summary')}:")
        for d in weather["daily"][:8]:
            lines.append(
                f"  {d.get('date')}: {d.get('label')}, "
                f"{d.get('temp_min')}–{d.get('temp_max')}°C, rain {d.get('precip_prob')}%"
            )
    return "\n".join(lines)


def _recent_history(trip_id: str, limit: int = 6) -> str:
    """Last few copilot turns for this trip → a compact transcript the router
    uses for continuity ('make it cheaper', 'add more there'). Reads the durable
    chat log, which records both the user's messages and the assistant replies."""
    try:
        messages = store.get_chat(trip_id, limit=limit * 2)
    except Exception:  # noqa: BLE001 — history is a nice-to-have, never block an edit
        return ""
    turns: list[str] = []
    for m in messages:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        who = "User" if m.get("role") == "user" else "Copilot"
        turns.append(f"{who}: {content}")
    return "\n".join(turns)


async def route_intent(message: str, state: dict, history: str = "", memory: str = "") -> dict:
    prompt = (
        "You are the copilot for a travel-planning workspace. Classify the "
        "user's message into one intent and write the reply.\n\n"
        + (f"WHAT WE KNOW ABOUT THIS TRAVELLER (from past trips — use it to make "
           f"replies and picks feel personal, but never contradict what they ask "
           f"for now):\n{memory}\n\n" if memory else "")
        + (f"RECENT CONVERSATION (oldest first, for continuity — resolve "
           f"follow-ups like 'a cheaper one' or 'add more there' against it):\n"
           f"{history}\n\n" if history else "")
        +
        "CRITICAL context: the trip below may be a MULTI-LEG route — e.g. a "
        "train leg followed by a bus leg. Those are parts of ONE journey, not "
        "alternative routes. 'Switch to the train' therefore usually makes no "
        "sense; the user picks a specific option WITHIN a leg instead.\n\n"
        "Intents:\n"
        "- answer_question: any question about the trip, route, timings, costs, "
        "places, weather, or logistics. Answer it directly from the summary "
        "(2-4 sentences, specific numbers/names, no fluff). Use general travel "
        "knowledge where the summary is silent, and say so.\n"
        "- select_transport_option: the user wants a specific ride on a leg "
        "(e.g. 'take the 11pm Volvo', 'a cheaper bus', 'the earlier train', 'a "
        "flight with more luggage'). Set direction (going/return), mode, and "
        "option_index from the numbered lists. Pick the option that best matches "
        "what they asked for — the option lines include fare, timing, rating and "
        "(for flights) baggage allowance, so honour 'more luggage/baggage' by "
        "choosing the highest-baggage flight, 'cheaper' by fare, etc. Also set "
        "`prefer` to the axis they cared about. The reply will be replaced by "
        "the handler, keep it short.\n"
        "- select_hotel: pick a different hotel by index from the candidates.\n"
        "- search_hotels: the user wants hotels the list doesn't cover — near a "
        "landmark/area ('hotels near Mall Road', 'stays close to the beach'), "
        "with specific constraints ('hostels under 1500 with wifi', 'a 5-star "
        "resort with a pool'), or just fresh options ('show me more hotels'). "
        "Set hotel_query to the landmark/area phrase only (e.g. 'Mall Road'), "
        "or empty for a general re-search. Put every constraint the user "
        "stated into hotel_filters — set ONLY what they asked for, never guess: "
        "property_types (hostel/homestay/guesthouse/hotel/resort/apartment/"
        "villa/vacation_home/bnb); price_min/price_max in ₹ per night ('under "
        "2000' → price_max 2000); min_review_score on a 0-10 scale ('good' 7, "
        "'very good' 8, 'wonderful/top rated' 9); star_ratings ('4 star or "
        "better' → [4,5]); facilities (canonical keys, e.g. pool, wifi, "
        "parking, spa, gym, beachfront, sea_view, mountain_view, kitchen, "
        "balcony, air_conditioning, bonfire, hot_tub, wheelchair_accessible, "
        "twin_beds, double_bed); meals (breakfast_included/half_board/"
        "full_board/all_inclusive); booleans free_cancellation, no_prepayment, "
        "breakfast_included, couple_friendly, pet_friendly; max_distance_km; "
        "sort ('cheapest first' → price_asc, 'best rated' → rating). This "
        "triggers a LIVE search — the reply will be replaced by the handler, "
        "keep it short.\n"
        "- add_activities: the user wants MORE spots/places in the itinerary "
        "('add more spots', 'more things to do on day 2', 'add some cafes', "
        "'this day feels empty'). Set day_index (0-based) if they named a "
        "specific day, else leave null. Set activity_query to the KIND of spot "
        "they want ('cafes', 'waterfalls', 'adventure', 'nightlife') or empty "
        "for general additions. Set count if they gave a number. The reply is "
        "replaced by the handler — keep it short.\n"
        "- add_waypoint: the user wants to add a STOPOVER CITY between origin and "
        "destination ('add Nainital in between', 'can we stop in Jaipur on the "
        "way', 'include Rishikesh en route'). Set waypoint_city to that city, and "
        "waypoint_days to how long to spend there. IMPORTANT: if the user named a "
        "city but did NOT say how many days (and it isn't clear from the recent "
        "conversation), do NOT guess — use the `clarify` intent to ask 'How many "
        "days would you like in {city}?' with clarify_options ['1 day','2 days',"
        "'3 days']. Only route to add_waypoint once the number of days is known "
        "(e.g. they answer '2 days' or originally said 'stop in Jaipur for 2 "
        "days'). This re-routes the trip and builds an itinerary for the stop — "
        "the reply is replaced by the handler, keep it short.\n"
        "- set_title: rename the trip.\n"
        "- clarify: the request is genuinely ambiguous or underspecified and "
        "guessing would risk the wrong change — you can't tell which leg/day/"
        "hotel they mean, the ask is contradictory, or it's too vague to act on "
        "('make it better', 'change the hotel' with several equally-likely reads, "
        "'fix the timings'). Ask ONE short, friendly clarifying question in "
        "`reply` and put 2-3 concrete choices in `clarify_options` (short phrases "
        "the user can tap, e.g. ['The way there', 'The way back'] or ['Cheaper "
        "stay', 'Closer to the beach', 'Higher rated']). Prefer acting when the "
        "intent is reasonably clear — only clarify when a wrong guess would "
        "actually cost the user.\n"
        "- unsupported: ONLY edits that truly can't be done (change the main "
        "destination or dates, regenerate the whole itinerary from scratch). "
        "Reply warmly with what IS possible right now: answer questions, swap "
        "rides on any leg, search/swap hotels, add spots or a stopover city, "
        "rename the trip.\n\n"
        "Examples:\n"
        "- 'find me a better/cheaper bus' → select_transport_option on the bus "
        "leg: scan the numbered list and pick the strongest option that differs "
        "from the current pick (weigh fare, rating, sensible timings).\n"
        "- 'take the 11 pm volvo' / 'the earlier train' → select_transport_option "
        "with that option's index.\n"
        "- 'is the bus overnight?' / 'what's the cheapest way there?' → "
        "answer_question, citing concrete options and fares.\n"
        "- 'will it rain?' / 'what should I pack?' → answer_question, grounded "
        "in the weather block when present.\n"
        "- 'show me hotels near Mall Road' → search_hotels, hotel_query 'Mall "
        "Road'. 'Any hotels closer to the lake?' → search_hotels, hotel_query "
        "'the lake'.\n"
        "- 'pet-friendly resorts under 8000 with a pool' → search_hotels, "
        "hotel_filters {property_types:['resort'], price_max:8000, "
        "pet_friendly:true, facilities:['pool']}.\n"
        "- 'a top-rated hostel near Anjuna with free cancellation' → "
        "search_hotels, hotel_query 'Anjuna', hotel_filters "
        "{property_types:['hostel'], min_review_score:9, free_cancellation:true}.\n"
        "- 'add more spots to day 2' → add_activities, day_index 1. 'add a few "
        "cafes' → add_activities, activity_query 'cafes'. 'this itinerary is "
        "thin, add more' → add_activities.\n"
        "- 'add Nainital in between' (no duration given) → clarify, reply 'How "
        "many days would you like in Nainital?', clarify_options ['1 day','2 "
        "days','3 days']. Then '2 days' (with Nainital in the recent "
        "conversation) → add_waypoint, waypoint_city 'Nainital', waypoint_days 2. "
        "'stop in Rishikesh for 2 days on the way' → add_waypoint directly, "
        "waypoint_city 'Rishikesh', waypoint_days 2.\n"
        "- 'a flight with more baggage allowance' → select_transport_option, "
        "mode flight, prefer most_baggage, option_index of the highest-baggage "
        "flight in the list.\n"
        "select_transport_option MUST include direction, mode and option_index; "
        "if you can't determine them, use answer_question and tell the user "
        "which numbered option you'd suggest instead.\n\n"
        f"TRIP SUMMARY:\n{_state_summary(state)}\n\n"
        f"USER MESSAGE: {message}"
    )
    return await generate_json(
        prompt, tier="brain", response_schema=_INTENT_SCHEMA,
        temperature=0.2, max_output_tokens=512, timeout=30.0,
    )


def _live_total(state: dict) -> int:
    """
    Live-priced total, mirroring what the workspace UI shows: the picked ride
    on every bookable leg (both directions) × travelers, plus the selected
    hotel × nights. Estimated food/activities/misc are deliberately excluded.
    """
    req = state.get("request") or {}
    travelers = max(int(req.get("travelers") or 1), 1)
    nights = max(int(req.get("stay_nights") or 1), 1)

    journeys = [_journey(state, "going"), _journey(state, "return")]
    journeys += (state.get("transport") or {}).get("onward_segments") or []
    per_person = 0.0
    for j in journeys:
        if not j:
            continue
        leg_modes = [
            (leg.get("mode") or "").lower()
            for leg in (j.get("legs") or [])
            if (leg.get("mode") or "").lower() in _MODE_KEYS
        ]
        # No legs metadata → buckets are alternatives; count only the first
        # bucket that has a recommendation.
        modes = list(dict.fromkeys(leg_modes)) or [
            m for m in _MODE_KEYS if (j.get(_MODE_KEYS[m]) or {}).get("recommended")
        ][:1]
        for mode in modes:
            rec = (j.get(_MODE_KEYS[mode]) or {}).get("recommended")
            if rec:
                per_person += _fare_per_person(mode, rec) or 0

    total = per_person * travelers
    hotel = (state.get("stay") or {}).get("selected") or {}
    price = hotel.get("price_per_night") or hotel.get("price")
    if isinstance(price, (int, float)) and price > 0:
        total += price * nights
    return round(total)


# ── Cost recompute ──────────────────────────────────────────────────────────────

def _recompute_costs(state: dict) -> dict:
    """
    Rebuild the costs block from the currently-picked options.

    Transport = sum of the recommended fare on every bookable leg of the GOING
    route × travelers (matches how the supervisor prices the trip). Also
    refreshes costs.route_breakdown's legN_<mode> entries.
    """
    costs = dict(state.get("costs") or {})
    req = state.get("request") or {}
    travelers = req.get("travelers") or 1

    going = _journey(state, "going")
    legs = going.get("legs") or []
    per_person = 0.0
    leg_fares: dict[str, int] = {}
    seen_modes: set[str] = set()

    for i, leg in enumerate(legs):
        mode = (leg.get("mode") or "").lower()
        if mode not in _MODE_KEYS or mode in seen_modes:
            continue
        rec = (going.get(_MODE_KEYS[mode]) or {}).get("recommended")
        if not rec:
            continue
        fare = _fare_per_person(mode, rec) or 0
        per_person += fare
        leg_fares[f"leg{i + 1}_{mode}"] = round(fare)
        seen_modes.add(mode)

    # Waypoint stopovers add onward segments (city→destination) that are part of
    # the outbound cost. Each segment is priced like its own mini-journey.
    for s_idx, seg in enumerate((state.get("transport") or {}).get("onward_segments") or []):
        seg_modes: set[str] = set()
        for i, leg in enumerate(seg.get("legs") or []):
            mode = (leg.get("mode") or "").lower()
            if mode not in _MODE_KEYS or mode in seg_modes:
                continue
            rec = (seg.get(_MODE_KEYS[mode]) or {}).get("recommended")
            if not rec:
                continue
            fare = _fare_per_person(mode, rec) or 0
            per_person += fare
            leg_fares[f"onward{s_idx + 1}_leg{i + 1}_{mode}"] = round(fare)
            seg_modes.add(mode)

    if not legs:  # single-hop journey: use whichever bucket holds the pick
        for mode, key in _MODE_KEYS.items():
            rec = (going.get(key) or {}).get("recommended")
            if rec and rec.get("is_recommended"):
                per_person = _fare_per_person(mode, rec) or 0
                break

    breakdown = dict(costs.get("breakdown") or {})
    breakdown["transport"] = round(per_person * travelers)
    breakdown["misc"] = round((breakdown.get("transport", 0) + breakdown.get("stay", 0)) * 0.10)
    total = sum(breakdown.get(k, 0) for k in ("transport", "stay", "food", "activities", "misc"))

    route_breakdown = dict(costs.get("route_breakdown") or {})
    for k in [k for k in route_breakdown if k.startswith("leg")]:
        route_breakdown.pop(k)
    route_breakdown.update(leg_fares)

    costs.update({
        "breakdown": breakdown,
        "route_breakdown": route_breakdown,
        "total": total,
        "per_person": total // max(travelers, 1),
    })
    return costs


def _recompute_stay_costs(costs: dict, travelers: int, stay_total: float) -> dict:
    out = dict(costs)
    breakdown = dict(out.get("breakdown") or {})
    breakdown["stay"] = round(stay_total)
    breakdown["misc"] = round((breakdown.get("transport", 0) + breakdown.get("stay", 0)) * 0.10)
    total = sum(breakdown.get(k, 0) for k in ("transport", "stay", "food", "activities", "misc"))
    out.update({
        "breakdown": breakdown,
        "total": total,
        "per_person": total // max(travelers, 1),
    })
    return out


# ── Handlers — pure functions state → patches ──────────────────────────────────

def handle_select_option(
    state: dict, direction: str, mode: str, index: int,
) -> tuple[list[PatchOp], str]:
    """Pick a specific ride on one leg; re-star items and reprice the trip."""
    if mode not in _MODE_KEYS:
        return [], "I don't recognise that transport mode."
    direction = "going" if direction != "return" else "return"
    j_key = "going" if direction == "going" else "return_journey"
    key = _MODE_KEYS[mode]

    journey = dict(_journey(state, direction))
    block = dict(journey.get(key) or {})
    items = [dict(it) for it in (block.get("items") or [])]
    if not items:
        return [], f"I don't have live {mode} options for that leg yet."
    if not (0 <= index < len(items)):
        return [], f"That {mode} isn't in the list — pick one of the {len(items)} shown."

    for i, it in enumerate(items):
        it["is_recommended"] = i == index
    chosen = items[index]
    block["items"] = items
    block["recommended"] = chosen
    journey[key] = block
    state.setdefault("transport", {})[j_key] = journey  # so cost recompute sees the pick

    patches = [PatchOp(op="set", path=f"/transport/{j_key}/{key}", value=block)]

    fare = round(_fare_per_person(mode, chosen) or 0)
    bits = [_option_times(chosen)]
    if fare:
        bits.append(f"₹{fare}/person")
    detail = " · ".join(b for b in bits if b)

    reply = (
        f"Locked in {_option_name(mode, chosen)} ({detail}) for the "
        f"{'way there' if direction == 'going' else 'way back'}."
    )
    if direction == "going":
        new_costs = _recompute_costs(state)
        patches.append(PatchOp(op="set", path="/costs", value=new_costs))
    live = _live_total(state)
    if live:
        reply += f" Rides + stay now ₹{live:,} at live prices."
    return patches, reply


def handle_select_flight_journey(state: dict) -> tuple[list[PatchOp], str]:
    """Switch the outbound to the direct-flight alternative the journey builder
    attached (`going.flight_journey`). Rebuilds `going` as a flight-only journey
    (so all existing cost/selection logic just works) and stashes the ground
    route so the user can switch back."""
    going = dict(_journey(state, "going"))
    fj = going.get("flight_journey") or {}
    items = fj.get("items") or []
    if not items:
        return [], "I don't have a direct flight option for this trip yet."
    if going.get("ground_route_backup"):
        return [], "You're already on the direct flight. Want me to switch back to the train + bus route?"

    rec = fj.get("recommended") or items[0]
    new_going = {
        "legs": [{"mode": "flight", "from": fj.get("from"), "to": fj.get("to")}],
        "flights": {"items": items, "recommended": rec},
        "summary": {"duration": fj.get("total_duration_display"), "has_real_data": True},
        "flight_journey": fj,                       # keep so the card can still render
        "ground_route_backup": {k: v for k, v in going.items() if k != "ground_route_backup"},
    }
    state.setdefault("transport", {})["going"] = new_going
    patches = [PatchOp(op="set", path="/transport/going", value=new_going)]
    new_costs = _recompute_costs(state)
    patches.append(PatchOp(op="set", path="/costs", value=new_costs))

    fare = round(_fare_per_person("flight", rec) or 0)
    reply = (
        f"Switched to the direct flight — {_option_name('flight', rec)} "
        f"({_option_times(rec)}, ₹{fare}/person)."
    )
    live = _live_total(state)
    if live:
        reply += f" Rides + stay now ₹{live:,} at live prices."
    return patches, reply


def handle_restore_ground_route(state: dict) -> tuple[list[PatchOp], str]:
    """Revert a flight-swap back to the originally-planned train/bus route."""
    going = dict(_journey(state, "going"))
    backup = going.get("ground_route_backup")
    if not backup:
        return [], "You're already on the train + bus route."
    restored = dict(backup)
    restored["flight_journey"] = going.get("flight_journey") or backup.get("flight_journey")
    state.setdefault("transport", {})["going"] = restored
    patches = [PatchOp(op="set", path="/transport/going", value=restored)]
    new_costs = _recompute_costs(state)
    patches.append(PatchOp(op="set", path="/costs", value=new_costs))
    reply = "Switched back to the train + bus route."
    live = _live_total(state)
    if live:
        reply += f" Rides + stay now ₹{live:,} at live prices."
    return patches, reply


def handle_select_hotel(state: dict, index: int) -> tuple[list[PatchOp], str]:
    hotels = (state.get("stay") or {}).get("hotels") or []
    if not (0 <= index < len(hotels)):
        return [], "I couldn't find that hotel in your options."
    hotel: dict[str, Any] = hotels[index]

    req = state.get("request") or {}
    travelers = req.get("travelers") or 1
    nights = req.get("stay_nights") or 1
    price = hotel.get("price_per_night") or hotel.get("price")
    patches = [PatchOp(op="set", path="/stay/selected", value=hotel)]
    reply = f"Done — {hotel.get('name')} is now your hotel."
    if isinstance(price, (int, float)) and price > 0:
        new_costs = _recompute_stay_costs(state.get("costs") or {}, travelers, price * nights)
        patches.append(PatchOp(op="set", path="/costs", value=new_costs))
        state.setdefault("stay", {})["selected"] = hotel  # so the live total sees the swap
        live = _live_total(state)
        if live:
            reply += f" Rides + stay now ₹{live:,} at live prices."
    return patches, reply


# ── Rich cards for the chat (carousels) ─────────────────────────────────────────

def _card_image(item: dict) -> str | None:
    photos = item.get("photos")
    first_photo = photos[0] if isinstance(photos, list) and photos else None
    return item.get("photo_url") or first_photo or item.get("image") or None


def _hotel_cards(state: dict, limit: int = 8) -> list[dict]:
    """Compact, tappable hotel cards for the chat carousel — mirrors what the
    Stays tab shows, so the user can pick a stay right from the conversation."""
    stay = state.get("stay") or {}
    hotels = stay.get("hotels") or []
    selected_name = (stay.get("selected") or {}).get("name")
    items: list[dict] = []
    for i, h in enumerate(hotels[:limit]):
        price = h.get("price_per_night") or h.get("price")
        items.append({
            "index": i,
            "title": h.get("name"),
            "subtitle": h.get("address") or h.get("city") or h.get("area"),
            "image": _card_image(h),
            "price": f"₹{round(price):,}/night" if isinstance(price, (int, float)) and price > 0 else None,
            "rating": h.get("review_score") or h.get("star_rating"),
            "badges": [t for t in (h.get("why_tags") or []) if isinstance(t, str)][:2],
            "selected": bool(selected_name) and h.get("name") == selected_name,
            "action": {"type": "select_hotel", "hotel_index": i},
        })
    if not items:
        return []
    return [{"kind": "hotel", "title": f"{len(hotels)} stays to choose from", "items": items}]


def _build_cards(kind: str | None, state: dict) -> list[dict]:
    """Structured, tappable result cards the chat renders as carousels. Returns
    [] when the intent doesn't have visual results worth surfacing."""
    if kind in ("search_hotels", "select_hotel"):
        return _hotel_cards(state)
    return []


# ── Entry point ─────────────────────────────────────────────────────────────────

async def _record_edit_signals(user_id: str, kind: str | None, intent: dict) -> None:
    """Turn a concrete copilot edit into durable preference signals. Best-effort;
    swallows everything so it can never affect the edit."""
    signals: dict[str, Any] = {}
    filters = intent.get("hotel_filters") or {}
    if kind == "search_hotels":
        if filters.get("property_types"):
            signals["hotel_property_types"] = filters["property_types"]
        if filters.get("facilities"):
            signals["amenities"] = filters["facilities"]
    elif kind == "select_transport_option" and intent.get("prefer"):
        signals["transport_pref"] = intent["prefer"]
    elif kind == "add_activities" and (intent.get("activity_query") or "").strip():
        signals["interests"] = [intent["activity_query"].strip()]

    if not signals:
        return
    try:
        from app.services.user_memory import record_signals

        await record_signals(user_id, signals)
    except Exception as exc:  # noqa: BLE001 — never affect the edit
        logger.warning("[trip-state] signal capture failed: %s", exc)


async def _persist_chat(
    trip_id: str, message: str, reply: str, kind: str | None, meta: dict | None = None,
) -> None:
    """Record the turn in the durable chat log so it survives a refresh. Both
    the user message (when present — structured UI actions carry none) and the
    assistant reply are stored. Best-effort: a chat-log write must never fail an
    edit, so transport failures are swallowed (the state edit already succeeded
    and was broadcast)."""
    from app.db.client import run_db

    def _write() -> None:
        if message:
            store.append_chat(trip_id, "user", message)
        if reply:
            store.append_chat(trip_id, "assistant", reply, intent=kind, meta=meta)

    try:
        await run_db(_write)
    except Exception as exc:  # noqa: BLE001 — a chat-log write must never block the edit
        logger.warning("[trip-state] chat-log write failed for %s: %s", trip_id, exc)


def _persist_and_broadcast(trip_id: str, patches: list[PatchOp], reply: str, message: str) -> None:
    from .generator import publish_event

    store.append_events(trip_id, "user", patches, phase="copilot_edit", reasoning=message)
    publish_event(trip_id, {
        "phase": "copilot_edit",
        "label": reply,
        "progress": 100,
        "patches": [p.model_dump(mode="json") for p in patches],
        "reasoning": message,
    })


def _auto_select_top_hotel(
    state: dict, patches: list[PatchOp], reply: str,
) -> tuple[list[PatchOp], str]:
    """When a hotel search was driven by a real constraint (cheapest, under ₹X,
    with a pool…), the list is already ranked for that constraint, so lock the
    top result in as the stay — that's the "edit that in stay" the user expects,
    not just a refreshed list. Costs follow the swap."""
    hotels = next(
        (p.value for p in patches if p.path == "/stay/hotels" and p.value), None
    )
    if not hotels:
        return patches, reply
    top = hotels[0]
    req = state.get("request") or {}
    travelers = req.get("travelers") or 1
    nights = req.get("stay_nights") or 1
    price = top.get("price_per_night") or top.get("price")
    patches = list(patches) + [PatchOp(op="set", path="/stay/selected", value=top)]
    state.setdefault("stay", {})["selected"] = top
    name = top.get("name") or "the top match"
    reply = f"{reply} I've set {name} as your stay — swap to any of the others in the Stays tab."
    if isinstance(price, (int, float)) and price > 0:
        new_costs = _recompute_stay_costs(state.get("costs") or {}, travelers, price * nights)
        patches.append(PatchOp(op="set", path="/costs", value=new_costs))
        state["costs"] = new_costs
    return patches, reply


async def apply_edit(
    trip_id: str, message: str, state: dict, action: dict | None = None,
    user_id: str = "",
) -> dict:
    """Route + apply an edit. Returns {reply, intent, applied, patches, clarify_options}.

    `action` is a structured UI command that bypasses the LLM router (deterministic,
    instant — the user driving the workspace directly):
      {"type": "select_transport_option", "direction", "mode", "index"}
      {"type": "select_hotel", "hotel_index"}
    """
    patches: list[PatchOp] = []
    reply = ""
    kind = None
    clarify_options: list[str] = []

    if action and action.get("type") == "select_transport_option":
        kind = "select_transport_option"
        patches, reply = handle_select_option(
            state,
            str(action.get("direction") or "going"),
            str(action.get("mode") or ""),
            int(action.get("index") or 0),
        )
    elif action and action.get("type") == "select_hotel":
        # User picked a stay directly from the Stays tab — deterministic, no LLM.
        kind = "select_hotel"
        patches, reply = handle_select_hotel(
            state, int(action.get("hotel_index") or action.get("index") or 0),
        )
    elif action and action.get("type") == "select_flight_journey":
        kind = "select_flight_journey"
        patches, reply = handle_select_flight_journey(state)
    elif action and action.get("type") == "restore_ground_route":
        kind = "restore_ground_route"
        patches, reply = handle_restore_ground_route(state)
    else:
        from app.services.user_memory import get_memory, memory_grounding

        history = _recent_history(trip_id)
        memory = memory_grounding(await get_memory(user_id)) if user_id else ""
        intent = await route_intent(message, state, history=history, memory=memory)
        kind = intent.get("intent")
        reply = intent.get("reply") or ""

        if kind == "select_transport_option" and intent.get("mode") is not None:
            patches, handler_reply = handle_select_option(
                state,
                intent.get("direction") or "going",
                intent["mode"],
                int(intent.get("option_index") or 0),
            )
            reply = handler_reply or reply
        elif kind == "select_hotel" and intent.get("hotel_index") is not None:
            patches, handler_reply = handle_select_hotel(state, int(intent["hotel_index"]))
            reply = handler_reply or reply
        elif kind == "search_hotels":
            from .hotel_search import handle_search_hotels

            patches, handler_reply = await handle_search_hotels(
                state,
                (intent.get("hotel_query") or "").strip(),
                raw_filters=intent.get("hotel_filters"),
            )
            reply = handler_reply or reply
            # A constrained search ("cheapest", "under 1000", "with a pool")
            # should also update the picked stay, not just the candidate list.
            if patches and intent.get("hotel_filters"):
                patches, reply = _auto_select_top_hotel(state, patches, reply)
        elif kind == "add_activities":
            from .structural import handle_add_activities

            day_index = intent.get("day_index")
            patches, handler_reply = await handle_add_activities(
                state,
                int(day_index) if day_index is not None else None,
                (intent.get("activity_query") or "").strip(),
                intent.get("count"),
            )
            reply = handler_reply or reply
        elif kind == "add_waypoint" and intent.get("waypoint_city"):
            from .structural import handle_add_waypoint

            patches, handler_reply = await handle_add_waypoint(
                state, user_id,
                intent["waypoint_city"],
                intent.get("waypoint_days"),
            )
            reply = handler_reply or reply
        elif kind == "set_title" and intent.get("title"):
            patches = [PatchOp(op="set", path="/meta/title", value=intent["title"])]
            reply = f"Renamed your trip to “{intent['title']}”."
        elif kind == "clarify":
            # Ambiguous request — the copilot asks instead of guessing. No patches;
            # the UI renders clarify_options as quick-reply chips.
            clarify_options = [
                o for o in (intent.get("clarify_options") or []) if isinstance(o, str) and o.strip()
            ][:3]

        # Learn from concrete edits so future trips/sessions start warmer.
        if user_id and patches:
            await _record_edit_signals(user_id, kind, intent)

    if patches:
        import httpx

        from app.db.client import run_db

        from . import flush

        def _broadcast_only() -> None:
            from .generator import publish_event

            publish_event(trip_id, {
                "phase": "copilot_edit",
                "label": reply,
                "progress": 100,
                "patches": [p.model_dump(mode="json") for p in patches],
                "reasoning": message,
            })

        if flush.has_pending(trip_id):
            # Earlier patches for this trip are still queued — writing now
            # would land BEFORE them and get overwritten. Join the queue.
            flush.enqueue(trip_id, "user", patches, phase="copilot_edit", reasoning=message)
            _broadcast_only()
        else:
            try:
                await run_db(lambda: _persist_and_broadcast(trip_id, patches, reply, message))
            except httpx.TransportError:
                # DB unreachable (transient TLS-corruption windows — see
                # db/client.py). The edit itself succeeded: broadcast it now so
                # live viewers update, and let the background flusher make the
                # snapshot converge. Failing the request would tell the user
                # their edit was lost when it wasn't.
                flush.enqueue(trip_id, "user", patches, phase="copilot_edit", reasoning=message)
                _broadcast_only()

    # Record the turn in the durable chat transcript (every turn, incl. pure
    # Q&A / clarify that produced no patches) so the thread survives a refresh.
    await _persist_chat(
        trip_id, message, reply, kind,
        meta={"clarify_options": clarify_options} if clarify_options else None,
    )

    return {
        "reply": reply,
        "intent": kind,
        "applied": bool(patches),
        "patches": [p.model_dump(mode="json") for p in patches],
        "clarify_options": clarify_options,
        "cards": _build_cards(kind, state),
    }
