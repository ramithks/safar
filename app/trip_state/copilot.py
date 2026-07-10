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
  set_title               — rename the trip.
  unsupported             — graceful "here's what I can do" reply.
"""
import logging
from typing import Any

from app.services.vertex import generate_json

from . import store
from .patch import PatchOp

logger = logging.getLogger(__name__)

_MODE_KEYS = {"flight": "flights", "train": "trains", "bus": "buses"}

_INTENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "intent": {
            "type": "STRING",
            "enum": [
                "answer_question",
                "select_transport_option",
                "select_hotel",
                "set_title",
                "unsupported",
            ],
        },
        "direction": {"type": "STRING", "enum": ["going", "return"], "nullable": True},
        "mode": {"type": "STRING", "enum": ["flight", "train", "bus"], "nullable": True},
        "option_index": {"type": "INTEGER", "nullable": True},
        "hotel_index": {"type": "INTEGER", "nullable": True},
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
                f"  [{i}] {_option_name(mode, it)} · {_option_times(it)} · ₹{round(fare)}/person{star}"
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
    return "\n".join(lines)


async def route_intent(message: str, state: dict) -> dict:
    prompt = (
        "You are the copilot for a travel-planning workspace. Classify the "
        "user's message into one intent and write the reply.\n\n"
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
        "(e.g. 'take the 11pm Volvo', 'a cheaper bus', 'the earlier train'). "
        "Set direction (going/return), mode, and option_index from the numbered "
        "lists. Pick the option that best matches what they asked for. The "
        "reply will be replaced by the handler, keep it short.\n"
        "- select_hotel: pick a different hotel by index from the candidates.\n"
        "- set_title: rename the trip.\n"
        "- unsupported: ONLY structural edits that truly can't be done "
        "(change destination/dates, add days, regenerate the itinerary). "
        "Reply warmly with what IS possible right now: answer questions, swap "
        "rides on any leg, swap the hotel, rename the trip.\n\n"
        "Examples:\n"
        "- 'find me a better/cheaper bus' → select_transport_option on the bus "
        "leg: scan the numbered list and pick the strongest option that differs "
        "from the current pick (weigh fare, rating, sensible timings).\n"
        "- 'take the 11 pm volvo' / 'the earlier train' → select_transport_option "
        "with that option's index.\n"
        "- 'is the bus overnight?' / 'what's the cheapest way there?' → "
        "answer_question, citing concrete options and fares.\n"
        "- 'add one more day in Manali' → unsupported.\n"
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
        if new_costs.get("total"):
            reply += f" Trip total is now ₹{new_costs['total']:,}."
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
        if new_costs.get("total"):
            reply += f" Trip total is now ₹{new_costs['total']:,}."
    return patches, reply


# ── Entry point ─────────────────────────────────────────────────────────────────

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


async def apply_edit(
    trip_id: str, message: str, state: dict, action: dict | None = None,
) -> dict:
    """Route + apply an edit. Returns {reply, patches, applied}.

    `action` is a structured UI command that bypasses the LLM router:
      {"type": "select_transport_option", "direction", "mode", "index"}
    """
    patches: list[PatchOp] = []
    reply = ""
    kind = None

    if action and action.get("type") == "select_transport_option":
        kind = "select_transport_option"
        patches, reply = handle_select_option(
            state,
            str(action.get("direction") or "going"),
            str(action.get("mode") or ""),
            int(action.get("index") or 0),
        )
    else:
        intent = await route_intent(message, state)
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
        elif kind == "set_title" and intent.get("title"):
            patches = [PatchOp(op="set", path="/meta/title", value=intent["title"])]
            reply = f"Renamed your trip to “{intent['title']}”."

    if patches:
        import asyncio

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: _persist_and_broadcast(trip_id, patches, reply, message)
        )

    return {
        "reply": reply,
        "intent": kind,
        "applied": bool(patches),
        "patches": [p.model_dump(mode="json") for p in patches],
    }
