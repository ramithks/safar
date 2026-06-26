"""
POST /api/v1/plan-journey — door-to-door multi-leg journey planner.

Phase 1 — Gemini plans the full route (legs, station codes, visa info).
Phase 2a — Batch pre-resolve all transport identifiers in parallel:
           RedBus city IDs (bus legs), station codes (train legs), IATA codes (flight legs).
Phase 2b — Build search tasks using pre-resolved IDs (no re-resolution inside search calls).
Phase 2c — Run all searches + flight alternatives + hotels in parallel.
"""
import asyncio
import logging
import urllib.parse
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.db.client import get_db
from app.services.vertex import vertex_generate, extract_json
from app.services.transport import TransportService
from app.services.hotels import HotelService
from app.services.flights import FlightService, budget_to_sort, sort_flights
from app.services.railway import RailwayService, city_to_station_code, resolve_station_async

from .models import PlanJourneyRequest
from .helpers import (
    CURRENCY_CONFIG,
    fetch_user_profile,
    rank_hotels,
    with_retry,
    clean_city,
)
from .validation import validate_route

logger = logging.getLogger(__name__)

_transport = TransportService()
_hotels    = HotelService()
_flights   = FlightService()
_railway   = RailwayService()


# ── Gemini route planner ──────────────────────────────────────────────────────

async def _gemini_plan_route(body: PlanJourneyRequest, budget: str, symbol: str, currency: str) -> dict:
    modes_hint = (
        f"Preferred transport modes: {', '.join(body.transport_modes)}."
        if body.transport_modes
        else "Choose the best transport combination for this route."
    )
    date_hint = f"Departure date: {body.departure_date}." if body.departure_date else "Departure date not specified."

    prompt = f"""You are an expert travel routing specialist with deep knowledge of Indian transport
networks, international routes, and last-mile connectivity for remote destinations.

JOURNEY REQUEST:
Origin: {body.origin}
Destination: {body.destination}
Travelers: {body.travelers} | Budget: {budget} ({symbol})
{modes_hint}
{date_hint}

TASK: Plan the complete, practical door-to-door journey from {body.origin} to {body.destination}.

CRITICAL RULES:
1. Break into logical legs — include intermediate hubs for remote destinations.
   Example: Delhi→Shangarh = [Delhi→Aut by overnight bus] + [Aut→Shangarh by shared cab]
   Example: Delhi→Kasol = [Delhi→Bhuntar by bus] + [Bhuntar→Kasol by shared cab]
   NEVER make a single direct leg where an intermediate hub is required.
2. TRAIN legs: Only suggest train where rail connectivity exists.
   Small hill towns (Manali, Leh, Kasol, Shangarh, Spiti, Tirthan Valley) have NO railway.
   For smaller cities, route via the nearest major city with a station:
   - North India: New Delhi, Old Delhi, Hazrat Nizamuddin
   - Uttarakhand: Haridwar, Dehradun
   - Punjab/HP: Chandigarh, Ambala
   - South India: Chennai, Bangalore, Hyderabad
   Example: Bhiwani→Haridwar = [Bhiwani→New Delhi by train] + [New Delhi→Haridwar by train]
3. BUS legs: Use the largest nearby city with bus connectivity as origin/destination.
   If the destination itself is a well-known bus hub, use it directly — do NOT route via a smaller
   nearby district/town.
   Known HP/Uttarakhand bus hubs: Dharamshala (NOT Kangra), Manali, Shimla, Dehradun, Haridwar, Rishikesh.
   Example: Delhi→Dharamshala is a valid direct bus leg. Do NOT use Delhi→Kangra.
4. FLIGHT legs: Only include if an airport exists reasonably close to origin/destination.
5. Cab/shared_cab/auto/walk: needs_live_search=false.
6. International: set is_international=true, provide complete visa guidance.
7. needs_live_search=true ONLY for bus/train/flight.
8. search_params origin/destination must be BARE city names — NO state/country suffix.
   CORRECT: "origin": "Aut"   WRONG: "origin": "Aut, Himachal Pradesh"
9. COMPLETE PIPELINE: Every journey MUST have first-mile and last-mile legs.
   - FIRST LEG: auto/cab/metro from user's location → departure station/bus stand
   - LAST LEG: shared_cab/cab/auto from arrival point → accommodation area (needs_live_search: false)
10. NEVER output station codes (NDLS, HW, BNW, CDG), IATA codes (DEL, KUU, IXC),
    or operator names (HRTC, IndiGo). Output ONLY city/place names.
    The system resolves all codes and operators automatically.

Return ONLY valid JSON (no markdown):
{{
  "is_international": false,
  "origin_country": "India",
  "destination_country": "India",
  "visa_required": false,
  "visa_info": null,
  "documents_needed": [],
  "health_requirements": [],
  "currency_info": null,
  "journey_summary": "2-3 sentence narrative",
  "total_distance_km": 0,
  "total_duration_approx": "Xh Ym",
  "best_time_to_depart": "specific departure advice",
  "packing_tips": ["tip 1", "tip 2"],
  "legs": [
    {{
      "leg": 1,
      "from": "City or place name",
      "to": "City or place name",
      "mode": "bus|train|flight|cab|shared_cab|auto|metro|walk|ferry",
      "duration_approx": "Xh Ym",
      "distance_km": 0,
      "tips": "one practical tip",
      "needs_live_search": true,
      "search_params": {{
        "origin": "bare city name",
        "destination": "bare city name",
        "mode_type": "bus|train|flight"
      }}
    }}
  ]
}}"""

    raw = await with_retry(vertex_generate, prompt, response_mime_type="application/json",
                           timeout=120.0, max_retries=2)
    if not raw:
        return {}
    try:
        return extract_json(raw)
    except Exception as e:
        logger.debug(f"[journey] Gemini route parse error: {e}")
        return {}


# ── Booking URL builder ────────────────────────────────────────────────────────

def _leg_booking_url(leg: dict, live_result: dict, departure_date: str) -> str | None:
    mode = leg.get("mode", "")
    sp = leg.get("search_params") or {}

    if mode == "bus":
        buses = live_result.get("buses") or []
        if buses and buses[0].get("booking_url"):
            return buses[0]["booking_url"]
        origin = sp.get("origin") or leg.get("from", "")
        dest   = sp.get("destination") or leg.get("to", "")
        return f"https://www.redbus.in/bus-tickets/{origin.lower().replace(' ', '-')}-to-{dest.lower().replace(' ', '-')}"

    if mode in ("train", "railway"):
        fc = sp.get("from_station_code") or ""
        tc = sp.get("to_station_code") or ""
        if fc and tc and departure_date:
            try:
                doj = datetime.fromisoformat(departure_date).strftime("%d-%m-%Y")
                return f"https://www.confirmtkt.com/rbooking/trains/from/{fc}/to/{tc}/{doj}"
            except Exception:
                pass
        return f"https://www.confirmtkt.com/rbooking/trains/from/{fc}/to/{tc}"

    if mode == "flight":
        for f in (live_result.get("flights") or []):
            if f.get("booking_url"):
                return f["booking_url"]
        fi = sp.get("from_iata") or ""
        ti = sp.get("to_iata") or ""
        date_fmt = departure_date.replace("-", "") if departure_date else ""
        if fi and ti and date_fmt:
            return f"https://www.makemytrip.com/flights/domestic/results/oneway/{fi}/{ti}/{date_fmt}/1/0/0/E/MYB"

    return None


# ── Live cost computation ─────────────────────────────────────────────────────

def _live_fare_for_leg(leg: dict) -> int | None:
    """Extract cheapest live price for a leg, or None if no live data."""
    live = leg.get("live_data") or {}
    mode = leg.get("mode", "").lower()
    if mode == "bus":
        buses = live.get("buses") or []
        fares = [b.get("fare_discounted") or b.get("fare_original") for b in buses]
        fares = [f for f in fares if f]
        return min(fares) if fares else None
    if mode in ("train", "railway"):
        trains = live.get("trains") or []
        fares = [t.get("cheapest_fare") for t in trains if t.get("cheapest_fare")]
        return min(fares) if fares else None
    if mode == "flight":
        flights = live.get("flights") or []
        prices = [f.get("price_total") for f in flights if f.get("price_total")]
        return min(prices) if prices else None
    return None


def _parse_duration_approx(s: str) -> int:
    """Parse '4h 30m', '12h', '45 mins' → minutes. Returns 0 on failure."""
    if not s:
        return 0
    total = 0
    import re as _re
    h = _re.search(r'(\d+)\s*h', s)
    m = _re.search(r'(\d+)\s*m(?:in)?', s)
    if h:
        total += int(h.group(1)) * 60
    if m:
        total += int(m.group(1))
    return total


def _compute_live_duration(legs: list[dict]) -> str | None:
    """
    Compute total journey duration from actual bus/flight arrival+departure timestamps
    plus Gemini's estimates for non-live legs (cabs, shared cabs, walks).
    Returns "Xh Ym" string or None if insufficient live data.
    """
    first_dep: datetime | None = None
    last_arr: datetime | None = None
    extra_min = 0  # accumulated from non-live legs

    for leg in legs:
        mode = leg.get("mode", "").lower()
        has_live = leg.get("has_live_data", False)
        live = leg.get("live_data") or {}

        if mode == "bus" and has_live:
            buses = live.get("buses") or []
            # Prefer recommended bus; fall back to first
            rec = next((b for b in buses if b.get("is_recommended")), buses[0] if buses else None)
            if rec:
                try:
                    dep = datetime.strptime(rec["departure"], "%Y-%m-%d %H:%M:%S")
                    arr = datetime.strptime(rec["arrival"],  "%Y-%m-%d %H:%M:%S")
                    if arr < dep:          # overnight: arrival is next day
                        arr += timedelta(days=1)
                    if first_dep is None or dep < first_dep:
                        first_dep = dep
                    if last_arr is None or arr > last_arr:
                        last_arr = arr
                    continue
                except Exception:
                    pass

        elif mode == "flight" and has_live:
            flights = live.get("flights") or []
            rec = next((f for f in flights if f.get("is_recommended")), flights[0] if flights else None)
            if rec:
                try:
                    dep_str = rec.get("departure_time") or ""
                    dep = datetime.fromisoformat(dep_str.replace("Z", ""))
                    dur = rec.get("duration_min") or 0
                    if first_dep is None:
                        first_dep = dep
                    if dur:
                        arr = dep + timedelta(minutes=dur)
                        if last_arr is None or arr > last_arr:
                            last_arr = arr
                    continue
                except Exception:
                    pass

        elif mode in ("train", "railway") and has_live:
            trains = live.get("trains") or []
            if trains:
                extra_min += trains[0].get("duration_min") or _parse_duration_approx(leg.get("duration_approx", ""))
                continue

        # Non-live leg or live fetch failed: use Gemini's estimate
        extra_min += _parse_duration_approx(leg.get("duration_approx", ""))

    if first_dep is None:
        return None

    known_min = 0
    if last_arr is not None:
        known_min = max(0, int((last_arr - first_dep).total_seconds() / 60))

    total_min = known_min + extra_min
    if total_min <= 0:
        return None
    h, m = divmod(total_min, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _compute_live_costs(
    legs: list[dict],
    hotel_result: dict | None,
    stay_nights: int,
    travelers: int,
) -> dict:
    """
    Compute trip cost from live prices.
    Updates leg["estimated_cost_per_person"] in-place where live data exists.
    Returns cost summary dict.
    """
    transport_per_person = 0
    breakdown: dict[str, int] = {}

    for leg in legs:
        live_fare = _live_fare_for_leg(leg)
        leg_key = f"leg{leg.get('leg', '?')}_{leg.get('mode', '?')}"
        if live_fare is not None:
            leg["fare_per_person"] = live_fare
            leg["cost_source"] = "live"
            transport_per_person += live_fare
            breakdown[leg_key] = live_fare
        else:
            leg["cost_source"] = "no_live_data"

    # Hotel cost from live data.
    # hotel_per_night is the room rate — shared across travelers, not per person.
    hotel_per_night: int | None = None
    hotel_total = 0
    hotel_pp = 0
    if hotel_result and not isinstance(hotel_result, Exception):
        hotels = hotel_result.get("hotels") or []
        priced = [h.get("price_per_night") for h in hotels if h.get("price_per_night")]
        if priced:
            hotel_per_night = min(priced)
            hotel_total = hotel_per_night * stay_nights
            hotel_pp = hotel_total // max(travelers, 1)  # share room cost across group
            breakdown["hotel_total"] = hotel_total
            breakdown["hotel_per_person"] = hotel_pp

    total_per_person = transport_per_person + hotel_pp
    total_trip = total_per_person * travelers

    return {
        "total_per_person": total_per_person,
        "transport_per_person": transport_per_person,
        "hotel_per_night": hotel_per_night,
        "hotel_total": hotel_total,
        "hotel_per_person": hotel_pp,
        "total_for_all_travelers": total_trip,
        "travelers": travelers,
        "stay_nights": stay_nights,
        "breakdown": breakdown,
        "source": "live",
    }


# ── Recommendation helpers ────────────────────────────────────────────────────

def _maps_url(place_name: str) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(place_name)}"


def _enrich_buses_with_maps(buses: list[dict]) -> list[dict]:
    for bus in buses:
        if bus.get("boarding_point"):
            bus["boarding_point_map_url"] = _maps_url(bus["boarding_point"])
        if bus.get("dropping_point"):
            bus["dropping_point_map_url"] = _maps_url(bus["dropping_point"])
    return buses


def _pick_recommended_bus(buses: list[dict], budget: str) -> dict | None:  # noqa: ARG001
    """
    Composite scoring: 0.30 price + 0.25 rating + 0.20 arrival_time + 0.15 seats + 0.10 amenities.
    Arrival score peaks in the 10 AM–2 PM window (ideal for hotel check-in).
    Returns a shallow copy with is_recommended=True.
    """
    if not buses:
        return None

    fares = [b.get("fare_discounted") or b.get("fare_original") or 0 for b in buses]
    valid_fares = [f for f in fares if f > 0]
    min_fare = min(valid_fares) if valid_fares else 1
    max_fare = max(valid_fares) if valid_fares else 1

    ratings = [float(b.get("rating") or 0) for b in buses]
    max_rating = max(ratings) if any(r > 0 for r in ratings) else 1

    def _arrival_score(bus: dict) -> float:
        s = bus.get("arrival", "")
        try:
            arr_dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            arr_min = arr_dt.hour * 60 + arr_dt.minute
            ideal = 11 * 60 + 30  # 11:30 AM
            diff = abs(arr_min - ideal)
            return max(0.0, 1.0 - diff / 300)  # decays to 0 at 5 h from ideal
        except Exception:
            return 0.5

    seats_list = [min(b.get("available_seats") or 0, 10) for b in buses]
    max_seats = max(seats_list) if any(s > 0 for s in seats_list) else 1

    def _amenities(bus: dict) -> int:
        items = bus.get("amenities") or bus.get("facilities") or []
        return len(items) if isinstance(items, list) else 0
    max_am = max(_amenities(b) for b in buses) or 1

    best_score = -1.0
    best_bus: dict | None = None
    for i, bus in enumerate(buses):
        fare = fares[i]
        price_score  = (max_fare - fare) / (max_fare - min_fare) if max_fare > min_fare else 1.0
        rating_score = ratings[i] / max_rating
        arr_score    = _arrival_score(bus)
        seats_score  = seats_list[i] / max_seats
        am_score     = _amenities(bus) / max_am

        total = (
            0.30 * price_score
            + 0.25 * rating_score
            + 0.20 * arr_score
            + 0.15 * seats_score
            + 0.10 * am_score
        )
        if total > best_score:
            best_score = total
            best_bus = bus

    if not best_bus:
        return None
    pick = dict(best_bus)
    pick["is_recommended"] = True
    return pick


def _train_has_departed(train: dict, departure_date: str) -> bool:
    """True if this train's scheduled departure on departure_date is already in the past."""
    dep_str = (train.get("departure_time") or "").strip()
    if not dep_str or not departure_date:
        return False
    try:
        dep_dt = datetime.strptime(f"{departure_date} {dep_str}", "%Y-%m-%d %H:%M")
        return dep_dt < datetime.now()
    except Exception:
        return False


def _pick_recommended_hotel(hotels: list[dict], budget: str) -> dict | None:
    """
    Pick best hotel matching the user's budget tier.
    Falls back to overall top-ranked hotel if no hotels match the range.
    Returns a shallow copy with is_recommended=True.
    """
    if not hotels:
        return None
    ranges = {
        "low":  (0,    2500),
        "mid":  (1000, 10000),
        "high": (5000, 999_999),
    }
    lo, hi = ranges.get(budget, (0, 999_999))
    in_budget = [
        h for h in hotels
        if lo <= (h.get("price_per_night") or 0) <= hi
    ]
    pick = dict((in_budget or hotels)[0])
    pick["is_recommended"] = True
    return pick


def _pick_recommended_flight(flights: list[dict], budget: str) -> dict | None:
    """Top flight under the budget tier's sort (low=cheapest, mid=best, high=fastest)."""
    if not flights:
        return None
    ranked = sort_flights(flights, budget_to_sort(budget) or "CHEAPEST")
    pick = dict(ranked[0])
    pick["is_recommended"] = True
    return pick


# ── Main handler ──────────────────────────────────────────────────────────────

async def run_plan_journey(body: PlanJourneyRequest, user_id: str, profile: dict | None = None) -> dict:
    db = get_db()
    safe_budget = body.budget if body.budget in ("low", "mid", "high") else "mid"

    if profile is None:
        profile = await asyncio.get_event_loop().run_in_executor(None, fetch_user_profile, db, user_id)
    currency_code = (profile.get("currency") or "inr").lower()
    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]

    # Phase 1 — Gemini plans route with intermediate hubs
    route_plan = await _gemini_plan_route(body, safe_budget, symbol, currency_code)
    if not route_plan:
        raise HTTPException(502, "Route planning failed — AI could not plan this journey")

    legs: list[dict] = route_plan.get("legs") or []
    departure_date = body.departure_date or ""

    # ── Phase 2a: Batch pre-resolve all transport identifiers ─────────────────
    # Collect unique resolution tasks across all searchable legs.
    # Gemini already breaks routes into intermediate hubs (e.g. Delhi→Aut, not Delhi→Sharnagh),
    # so each leg's search_params already point to the nearest bus/train/flight hub.
    _seen: set[str] = set()
    _resolve_keys: list[str] = []
    _resolve_coros: list = []

    def _enqueue(prefix: str, city: str, coro_fn, *args):
        k = f"{prefix}|{city.lower()}"
        if k not in _seen:
            _seen.add(k)
            _resolve_keys.append(k)
            _resolve_coros.append(coro_fn(*args))

    for leg in legs:
        if not leg.get("needs_live_search") or not departure_date:
            continue
        sp = leg.get("search_params") or {}
        mode = leg.get("mode", "").lower()
        o = clean_city(sp.get("origin") or leg.get("from", ""))
        d = clean_city(sp.get("destination") or leg.get("to", ""))

        if mode == "bus":
            _enqueue("bus", o, _transport.resolve_bus_city_id, o)
            _enqueue("bus", d, _transport.resolve_bus_city_id, d)
        elif mode in ("train", "railway"):
            # Gemini no longer outputs station codes — always resolve from city name
            _enqueue("train", o, resolve_station_async, o)
            _enqueue("train", d, resolve_station_async, d)
        elif mode == "flight":
            # Gemini no longer outputs IATA codes — always resolve from city name
            _enqueue("flight", o, _flights.resolve_iata, o)
            _enqueue("flight", d, _flights.resolve_iata, d)

        # Also resolve train stations for non-train intercity legs (bus/flight),
        # so we can search a train alternative for that segment too — needed to
        # bundle a complete door-to-door all-train journey option.
        if mode in ("bus", "flight"):
            _enqueue("train", o, resolve_station_async, o)
            _enqueue("train", d, resolve_station_async, d)

    if _resolve_coros:
        _resolve_results = await asyncio.gather(*_resolve_coros, return_exceptions=True)
    else:
        _resolve_results = []

    resolved: dict[str, object] = {}
    for k, v in zip(_resolve_keys, _resolve_results):
        resolved[k] = None if isinstance(v, Exception) else v

    # Log pre-resolution results
    resolved_lines = "  ".join(
        f"[{k.split('|')[0]}] {k.split('|')[1]!r} → {v or '✗'}"
        for k, v in resolved.items()
    )
    logger.info(
        "[journey] %s → %s | resolved %d ids: %s",
        body.origin, body.destination, len(resolved), resolved_lines,
    )

    # ── Validate legs against resolved identifiers (anti-hallucination) ───────
    validate_route(legs, resolved)

    # ── Phase 2b: Build search tasks using pre-resolved IDs ───────────────────
    live_meta: list[tuple[int, str]] = []
    live_coros: list = []

    # Train-alternative searches for non-train intercity legs (bus/flight) —
    # lets us bundle a complete door-to-door all-train journey alongside
    # whatever modes Gemini actually planned.
    train_alt_meta: list[int] = []
    train_alt_coros: list = []

    for i, leg in enumerate(legs):
        if not leg.get("needs_live_search") or not departure_date:
            leg["has_live_data"] = False
            continue

        sp = leg.get("search_params") or {}
        mode = leg.get("mode", "").lower()
        origin_s = clean_city(sp.get("origin") or leg.get("from", ""))
        dest_s   = clean_city(sp.get("destination") or leg.get("to", ""))

        if mode == "bus":
            from_id = resolved.get(f"bus|{origin_s.lower()}")
            to_id   = resolved.get(f"bus|{dest_s.lower()}")
            if from_id and to_id:
                live_meta.append((i, "bus"))
                live_coros.append(with_retry(
                    _transport.search_buses_direct,
                    from_id, to_id, origin_s, dest_s, departure_date, body.travelers,
                    budget=safe_budget,
                    fallback={"buses": [], "resolved": False},
                ))
            else:
                leg["has_live_data"] = False
                leg["live_search_note"] = (
                    f"No bus stop found for {'origin' if not from_id else 'destination'}: "
                    f"{origin_s if not from_id else dest_s}"
                )

        elif mode in ("train", "railway"):
            from_code = resolved.get(f"train|{origin_s.lower()}")
            to_code   = resolved.get(f"train|{dest_s.lower()}")
            # Fall back to cache (populated by phase 2a resolve_station_async)
            if not from_code:
                from_code = city_to_station_code(origin_s)
            if not to_code:
                to_code = city_to_station_code(dest_s)
            if from_code and to_code:
                # Persist resolved codes back to search_params so booking URL builder can use them
                sp["from_station_code"] = from_code
                sp["to_station_code"]   = to_code
                live_meta.append((i, "train"))
                live_coros.append(with_retry(
                    _railway.search, origin_s, dest_s, departure_date,
                    from_code=from_code, to_code=to_code,
                    fallback={"trains": [], "resolved": False},
                ))
            else:
                leg["has_live_data"] = False
                leg["live_search_note"] = "No railway station found for this segment"

        elif mode == "flight":
            from_iata = resolved.get(f"flight|{origin_s.lower()}")
            to_iata   = resolved.get(f"flight|{dest_s.lower()}")
            if from_iata and to_iata:
                # Persist back so booking URL builder has the codes
                sp["from_iata"] = from_iata
                sp["to_iata"]   = to_iata
                live_meta.append((i, "flight"))
                live_coros.append(with_retry(
                    _flights.search, origin_s, dest_s, departure_date,
                    adults=body.travelers,
                    from_iata=from_iata, to_iata=to_iata,
                    sort=budget_to_sort(safe_budget),   # budget → upstream provider sort
                    fallback={"flights": [], "resolved": False},
                ))
            else:
                leg["has_live_data"] = False
                leg["live_search_note"] = (
                    f"No airport found for {'origin' if not from_iata else 'destination'}: "
                    f"{origin_s if not from_iata else dest_s}"
                )

        # Fetch a train alternative for this segment too (bus/flight legs only) —
        # so a complete all-train journey can be bundled even when Gemini chose
        # a different mode for this particular hop.
        if mode in ("bus", "flight"):
            alt_from_code = resolved.get(f"train|{origin_s.lower()}") or city_to_station_code(origin_s)
            alt_to_code   = resolved.get(f"train|{dest_s.lower()}") or city_to_station_code(dest_s)
            if alt_from_code and alt_to_code:
                train_alt_meta.append(i)
                train_alt_coros.append(with_retry(
                    _railway.search, origin_s, dest_s, departure_date,
                    from_code=alt_from_code, to_code=alt_to_code,
                    fallback={"trains": [], "resolved": False},
                ))

    # ── Phase 2c: Flight alternatives + hotels at FINAL destination ───────────
    # ── Phase 2c: hotel at destination (only extra search beyond per-leg) ────────
    hotel_coro = None
    checkin = checkout = None
    if body.stay_nights > 0 and departure_date:
        try:
            from datetime import datetime as _dt
            import math as _math
            checkin  = (datetime.fromisoformat(departure_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            checkout = (datetime.fromisoformat(departure_date) + timedelta(days=1 + body.stay_nights)).strftime("%Y-%m-%d")
            ci_dt = _dt.strptime(checkin, "%Y-%m-%d")
            co_dt = _dt.strptime(checkout, "%Y-%m-%d")
            if co_dt <= ci_dt:
                logger.warning("[journey] Hotel skipped: checkout %s <= checkin %s", checkout, checkin)
                hotel_coro = None
            elif (co_dt - ci_dt).days > 30:
                logger.warning("[journey] Hotel skipped: stay > 30 nights (%d)", (co_dt - ci_dt).days)
                hotel_coro = None
            else:
                # Real rooms needed: 2 travelers share 1 room, round up for odd group sizes
                rooms_needed = max(1, _math.ceil(body.travelers / 2))
                hotel_coro = with_retry(
                    _hotels.search, body.destination, checkin, checkout,
                    adults=body.travelers, rooms=rooms_needed, budget=safe_budget,
                    fallback={"hotels": [], "source": "none"},
                )
        except Exception:
            hotel_coro = None

    extra_coros = train_alt_coros + ([hotel_coro] if hotel_coro else [])
    all_results = await asyncio.gather(*(live_coros + extra_coros), return_exceptions=True) if (live_coros + extra_coros) else []

    transport_results = all_results[: len(live_coros)]
    train_alt_results = all_results[len(live_coros): len(live_coros) + len(train_alt_coros)]
    hotel_idx = len(live_coros) + len(train_alt_coros)
    hotel_result = all_results[hotel_idx] if hotel_coro and len(all_results) > hotel_idx else None

    # Stash train-alternative results on their legs (tagged for departure status)
    for task_idx, leg_idx in enumerate(train_alt_meta):
        if task_idx >= len(train_alt_results):
            break
        result = train_alt_results[task_idx]
        if isinstance(result, Exception):
            continue
        for t in (result.get("trains") or []):
            t["departed"] = _train_has_departed(t, departure_date)
        legs[leg_idx]["train_alternative"] = result

    # Inject live data into legs + enrich buses with Maps links
    _rec_bus:    dict | None = None
    _rec_train:  dict | None = None
    _rec_flight: dict | None = None

    for task_idx, (leg_idx, mode_type) in enumerate(live_meta):
        if task_idx >= len(transport_results):
            break
        result = transport_results[task_idx]
        if isinstance(result, Exception):
            legs[leg_idx]["has_live_data"] = False
            continue

        if mode_type == "bus":
            buses = _enrich_buses_with_maps(result.get("buses") or [])
            result["buses"] = buses
            # Mark recommended bus in the array too
            rec = _pick_recommended_bus(buses, safe_budget)
            if rec and not _rec_bus:
                _rec_bus = rec
                for b in buses:
                    if b is rec or (b.get("departure") == rec.get("departure") and b.get("operator") == rec.get("operator")):
                        b["is_recommended"] = True

        elif mode_type == "flight":
            # Order the list by the budget tier's sort so it matches the recommendation.
            flights = sort_flights(result.get("flights") or [], budget_to_sort(safe_budget) or "CHEAPEST")
            result["flights"] = flights
            rec = _pick_recommended_flight(flights, safe_budget)
            if rec and not _rec_flight:
                _rec_flight = rec
                for f in flights:
                    if f.get("flight_number") == rec.get("flight_number") and f.get("departure_time") == rec.get("departure_time"):
                        f["is_recommended"] = True

        elif mode_type == "train":
            trains = result.get("trains") or []
            for t in trains:
                t["departed"] = _train_has_departed(t, departure_date)
            upcoming = [t for t in trains if not t["departed"]]
            pool = upcoming or trains
            if pool and not _rec_train:
                _rec_train = dict(pool[0])
                _rec_train["is_recommended"] = True
                pool[0]["is_recommended"] = True

        legs[leg_idx]["live_data"] = result
        has = bool(
            result.get("buses")   if mode_type == "bus"    else
            result.get("trains")  if mode_type == "train"  else
            result.get("flights") if mode_type == "flight" else False
        )
        legs[leg_idx]["has_live_data"] = has
        legs[leg_idx]["booking_url"] = _leg_booking_url(legs[leg_idx], result, departure_date)

    # ── Recompute cost + duration from live data ──────────────────────────────
    live_costs = _compute_live_costs(legs, hotel_result, body.stay_nights, body.travelers)
    route_plan["cost_breakdown"] = live_costs

    live_duration = _compute_live_duration(legs)
    if live_duration:
        route_plan["total_duration_approx"] = live_duration
        route_plan["duration_source"] = "live"

    route_plan["legs"] = legs
    route_plan["currency"] = symbol
    route_plan["has_real_data"] = any(leg.get("has_live_data") for leg in legs)

    _rec_hotel: dict | None = None
    if hotel_result and not isinstance(hotel_result, Exception):
        ranked = rank_hotels(hotel_result.get("hotels") or [])
        _rec_hotel = _pick_recommended_hotel(ranked, safe_budget)
        if _rec_hotel:
            for h in ranked:
                if h.get("name") == _rec_hotel.get("name"):
                    h["is_recommended"] = True
        route_plan["destination_hotels"] = {
            "destination": body.destination,
            "source": hotel_result.get("source"),
            "checkin": checkin,
            "checkout": checkout,
            "nights": body.stay_nights,
            "hotels": ranked[:20],
            "top_pick": _rec_hotel,
        }

    # Unified recommendations block
    route_plan["recommendations"] = {
        "hotel": _rec_hotel,
        "transport": _rec_bus or _rec_train or _rec_flight,
        "bus": _rec_bus,
        "train": _rec_train,
        "flight": _rec_flight,
        "note": (
            "Bus scored by: 30% price, 25% rating, 20% arrival time, "
            "15% availability, 10% amenities. Hotel sorted by budget tier."
        ),
    }

    # ── Build fastest/cheapest/balanced journey options ───────────────────────
    route_plan["journey_options"] = _build_journey_options(route_plan, body.travelers)

    # ── Bundle a complete door-to-door all-train journey (if feasible) ────────
    train_journey = _build_train_journey_bundle(legs, body.travelers)
    if train_journey:
        route_plan["train_journey"] = train_journey

    # Summary log
    _h  = route_plan.get("destination_hotels")
    live_hits  = sum(1 for leg in legs if leg.get("has_live_data"))
    live_total = sum(1 for leg in legs if leg.get("needs_live_search"))
    leg_lines = " | ".join(
        f"Leg{leg.get('leg')}[{leg.get('mode')}] "
        + (f"✓ {len((leg.get('live_data') or {}).get('buses') or (leg.get('live_data') or {}).get('trains') or (leg.get('live_data') or {}).get('flights') or [])}"
           if leg.get("has_live_data") else ("✗" if leg.get("needs_live_search") else "n/a"))
        for leg in legs
    )
    hotel_info = f"{_h['source']}|{len(_h.get('hotels') or [])}@{_h.get('destination','?')}" if _h else "none"
    logger.info(
        "[journey] %s→%s | %d legs (%s) | live:%d/%d | hotels:%s",
        body.origin, body.destination, len(legs),
        route_plan.get("total_duration_approx", "?"),
        live_hits, live_total, hotel_info,
    )
    logger.debug("[journey] legs: %s", leg_lines)

    return route_plan


def _build_train_journey_bundle(legs: list[dict], travelers: int) -> dict | None:
    """
    Stitch the per-segment train options (live_data for train-mode legs,
    train_alternative for bus/flight-mode legs) into one door-to-door
    all-train itinerary, so the user can see/compare a complete train journey
    even when Gemini mixed modes (e.g. train + flight) across segments.

    Returns None when the journey has no intercity legs at all.
    """
    segments: list[dict] = []
    total_fare = 0
    total_duration = 0
    feasible = True

    for leg in legs:
        if not leg.get("needs_live_search"):
            continue
        mode = leg.get("mode", "").lower()
        if mode in ("train", "railway"):
            trains = (leg.get("live_data") or {}).get("trains") or []
        else:
            trains = (leg.get("train_alternative") or {}).get("trains") or []

        upcoming = [t for t in trains if not t.get("departed")]
        pool = upcoming or trains
        pick = pool[0] if pool else None

        seg: dict = {"from": leg.get("from"), "to": leg.get("to"), "available": bool(pick)}
        if pick:
            fare = pick.get("cheapest_fare") or 0
            dur  = pick.get("duration_min") or 0
            total_fare     += fare
            total_duration += dur
            seg.update({
                "train_number":   pick.get("train_number"),
                "train_name":     pick.get("train_name"),
                "from_station":   pick.get("from_station_name"),
                "to_station":     pick.get("to_station_name"),
                "departure_time": pick.get("departure_time"),
                "arrival_time":   pick.get("arrival_time"),
                "duration_min":   dur,
                "cheapest_fare":  fare,
                "booking_url":    pick.get("booking_url"),
            })
        else:
            feasible = False
        segments.append(seg)

    if not segments:
        return None

    return {
        "feasible": feasible,
        "segments": segments,
        "total_fare_per_person": total_fare,
        "total_cost": total_fare * travelers,
        "total_duration_min": total_duration,
        "total_duration_display": _fmt_duration(total_duration) if total_duration else None,
        "note": (
            "Complete door-to-door journey using trains for every intercity segment."
            if feasible else
            "Some segments have no train connectivity — shown for comparison only."
        ),
    }


# ── Journey options: fastest / cheapest / balanced ────────────────────────────

def _fmt_duration(minutes: int) -> str:
    if minutes >= 999_999:
        return "Unknown"
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _bus_duration_min(bus: dict) -> int:
    dep, arr = bus.get("departure"), bus.get("arrival")
    if dep and arr:
        try:
            d = datetime.strptime(dep, "%Y-%m-%d %H:%M:%S")
            a = datetime.strptime(arr, "%Y-%m-%d %H:%M:%S")
            if a < d:
                a += timedelta(days=1)
            return int((a - d).total_seconds() / 60)
        except Exception:
            pass
    return 999_999


def _option_block(c: dict, tag: str, label: str) -> dict:
    return {
        "tag": tag,
        "label": label,
        "mode": c["mode"],
        "description": c["description"],
        "cost_per_person": c["cost_per_person"],
        "total_cost": c["total_cost"],
        "duration_min": c["duration_min"],
        "duration_display": _fmt_duration(c["duration_min"]),
        "operator": c["operator"],
        "booking_url": c.get("booking_url"),
        "from_iata": c.get("from_iata"),
        "to_iata": c.get("to_iata"),
        "from_code": c.get("from_code"),
        "to_code": c.get("to_code"),
    }


def _build_journey_options(journey_result: dict, travelers: int) -> dict:
    """
    Produce fastest/cheapest/balanced options from per-leg live data.
    Only compares modes that Gemini actually planned and that have live data.
    """
    bus_items: list[dict] = []
    train_items: list[dict] = []
    flight_items: list[dict] = []

    for leg in (journey_result.get("legs") or []):
        if not leg.get("has_live_data"):
            continue
        mode = leg.get("mode", "").lower()
        ld = leg.get("live_data") or {}
        if mode == "bus":
            bus_items.extend(ld.get("buses") or [])
        elif mode in ("train", "railway"):
            train_items.extend(ld.get("trains") or [])
        elif mode == "flight":
            flight_items.extend(ld.get("flights") or [])

    candidates: list[dict] = []

    if flight_items:
        f = min(flight_items, key=lambda x: x.get("price_total") or 999_999)
        candidates.append({
            "mode": "flight",
            "cost_per_person": f.get("price_total") or 0,
            "total_cost": (f.get("price_total") or 0) * travelers,
            "duration_min": f.get("duration_min") or 999_999,
            "operator": f.get("airline") or "Flight",
            "description": f"{f.get('airline', 'Flight')} {f.get('flight_number', '')}".strip(),
            "booking_url": f.get("booking_url"),
        })

    if train_items:
        t = min(train_items, key=lambda x: x.get("cheapest_fare") or 999_999)
        candidates.append({
            "mode": "train",
            "cost_per_person": t.get("cheapest_fare") or 0,
            "total_cost": (t.get("cheapest_fare") or 0) * travelers,
            "duration_min": t.get("duration_min") or 999_999,
            "operator": t.get("train_name") or "Train",
            "description": f"{t.get('train_name', 'Train')} ({t.get('train_number', '')})".strip(" ()"),
            "booking_url": None,
        })

    if bus_items:
        b = min(bus_items, key=lambda x: x.get("fare_discounted") or x.get("fare_original") or 999_999)
        candidates.append({
            "mode": "bus",
            "cost_per_person": b.get("fare_discounted") or b.get("fare_original") or 0,
            "total_cost": (b.get("fare_discounted") or b.get("fare_original") or 0) * travelers,
            "duration_min": _bus_duration_min(b),
            "operator": b.get("operator") or "Bus",
            "description": f"{b.get('operator', 'Bus')} — {b.get('bus_type', '')}".strip(" —"),
            "booking_url": b.get("booking_url"),
        })

    if not candidates:
        return {}

    # When only one transport mode has live data, differentiate within that mode's items
    # so cheapest/fastest/balanced show meaningfully different options.
    if len(candidates) == 1:
        sole_mode = candidates[0]["mode"]
        pool: list[dict] = (
            bus_items    if sole_mode == "bus"    else
            train_items  if sole_mode == "train"  else
            flight_items
        )
        if len(pool) >= 2:
            def _fare(x: dict) -> int:
                return x.get("fare_discounted") or x.get("fare_original") or x.get("cheapest_fare") or x.get("price_total") or 999_999

            cheapest_item = min(pool, key=_fare)
            fastest_item  = min(pool, key=lambda x: (
                _bus_duration_min(x) if sole_mode == "bus" else
                x.get("duration_min") or 999_999
            ))

            def _balance_score(x: dict) -> float:
                f = _fare(x)
                d = _bus_duration_min(x) if sole_mode == "bus" else x.get("duration_min") or 999_999
                max_f = max(_fare(i) for i in pool) or 1
                max_d = max((_bus_duration_min(i) if sole_mode == "bus" else i.get("duration_min") or 999_999) for i in pool) or 1
                return (1 - f / max_f) * 0.50 + (1 - min(d, 999_998) / max_d) * 0.50

            balanced_item = max(pool, key=_balance_score)

            def _bus_to_candidate(b: dict) -> dict:
                fare = _fare(b)
                return {
                    "mode": sole_mode,
                    "cost_per_person": fare,
                    "total_cost": fare * travelers,
                    "duration_min": (
                        _bus_duration_min(b) if sole_mode == "bus" else b.get("duration_min") or 999_999
                    ),
                    "operator": (
                        b.get("operator") or b.get("train_name") or b.get("airline") or sole_mode.title()
                    ),
                    "description": (
                        f"{b.get('operator', '')} — {b.get('bus_type', '')}".strip(" —")
                        if sole_mode == "bus" else
                        f"{b.get('train_name', '')} ({b.get('train_number', '')})".strip(" ()")
                        if sole_mode == "train" else
                        f"{b.get('airline', '')} {b.get('flight_number', '')}".strip()
                    ),
                    "booking_url": b.get("booking_url"),
                }

            return {
                "cheapest": _option_block(_bus_to_candidate(cheapest_item), "cheapest", "Lowest price option"),
                "fastest":  _option_block(_bus_to_candidate(fastest_item),  "fastest",  "Shortest travel time"),
                "balanced": _option_block(_bus_to_candidate(balanced_item), "balanced", "Best mix of time and cost"),
            }

    cheapest = min(candidates, key=lambda x: x.get("cost_per_person") or 999_999)
    fastest  = min(candidates, key=lambda x: x.get("duration_min") or 999_999)

    max_cost = max(c["cost_per_person"] for c in candidates) or 1
    max_dur  = max(c["duration_min"] for c in candidates) or 1
    for c in candidates:
        c["_balance"] = (
            (1 - c["cost_per_person"] / max_cost) * 0.5
            + (1 - min(c["duration_min"], 999_998) / max_dur) * 0.5
        )
    balanced = max(candidates, key=lambda x: x.get("_balance", 0))

    return {
        "cheapest": _option_block(cheapest, "cheapest", "Best value for money"),
        "fastest":  _option_block(fastest,  "fastest",  "Saves the most time"),
        "balanced": _option_block(balanced,  "balanced", "Best mix of time and cost"),
    }
