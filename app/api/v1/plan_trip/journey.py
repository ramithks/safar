"""
POST /api/v1/plan-journey — door-to-door multi-leg journey planner.

Phase 1 — Gemini plans the full route (legs, station codes, visa info).
Phase 2a — Batch pre-resolve all transport identifiers in parallel:
           RedBus city IDs (bus legs), station codes (train legs), IATA codes (flight legs).
Phase 2b — Build search tasks using pre-resolved IDs (no re-resolution inside search calls).
Phase 2c — Run all searches + flight alternatives + hotels in parallel.
"""
import asyncio
import urllib.parse
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.db.client import get_db
from app.services.vertex import vertex_generate, extract_json
from app.services.transport import TransportService
from app.services.hotels import HotelService
from app.services.flights import FlightService
from app.services.railway import RailwayService, city_to_station_code, resolve_station_async
from app.services.booking.flights import search_flights as booking_search_flights
from app.services.easemytrip.flights import search_flights as emt_search_flights

from .models import PlanJourneyRequest
from .helpers import (
    CURRENCY_CONFIG,
    fetch_user_profile,
    rank_hotels,
    with_retry,
    clean_city,
)

_transport = TransportService()
_hotels    = HotelService()
_flights   = FlightService()
_railway   = RailwayService()


# ── Transport alternatives (trains + flights) ────────────────────────────────

async def _search_train_alternatives(
    origin: str,
    destination: str,
    date: str,
    travelers: int,
    limit: int = 5,
) -> dict:
    """
    Search trains between overall origin → destination as an alternative mode.
    Returns {} if no rail connectivity.
    """
    from_code, to_code = await asyncio.gather(
        resolve_station_async(origin),
        resolve_station_async(destination),
    )
    if not from_code or not to_code:
        return {}
    result = await _railway.search(origin, destination, date,
                                   from_code=from_code, to_code=to_code, limit=limit)
    return result


async def _search_flight_alternatives(
    from_iata: str, to_iata: str, date: str, travelers: int, limit: int = 4,
) -> list[dict]:
    if not from_iata or not to_iata or not date:
        return []
    booking_r, emt_r = await asyncio.gather(
        with_retry(booking_search_flights, from_iata, to_iata, date, adults=travelers, limit=limit + 2, fallback=[]),
        with_retry(emt_search_flights,     from_iata, to_iata, date, adults=travelers, limit=limit + 2, fallback=[]),
    )
    seen: set[str] = set()
    merged: list[dict] = []
    for f in (booking_r or []) + (emt_r or []):
        key = (f.get("flight_number") or "") + str(f.get("departure_time") or "")
        if key and key in seen:
            continue
        seen.add(key)
        merged.append(f)
    merged.sort(key=lambda f: f.get("price_total") or 999_999)
    return merged[:limit]


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
1. Always break into logical legs — include intermediate hubs for remote destinations.
   Example: Delhi → Sharnagh = [Delhi→Aut by overnight bus, Aut→Sharnagh by shared cab]
   Example: Delhi → Kasol = [Delhi→Bhuntar by bus, Bhuntar→Kasol by shared cab]
   NEVER make a single leg for a route that requires an intermediate hub.
2. TRAIN legs: exact Indian Railway station codes (NDLS, PNBE, CDG etc.)
   Only suggest train where rail exists — Manali, Leh, Spiti, Kasol, Sharnagh have NO railway.
3. BUS legs: mention operator (HRTC, RSRTC, Volvo) and boarding point.
   Use the largest nearby city that has bus connectivity as origin/destination in search_params.
4. FLIGHT legs: include airport IATA codes (DEL, BOM, IXL etc.)
5. Cab/shared_cab/auto: realistic local fares, needs_live_search=false.
6. International: set is_international=true, provide complete visa guidance.
7. Cost estimates in {currency.upper()} ({symbol}).
8. needs_live_search=true ONLY for bus/train/flight.
9. search_params origin/destination must be BARE city names — NO state/country suffix.
   CORRECT: "origin": "Aut"   WRONG: "origin": "Aut, Himachal Pradesh"

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
  "total_estimated_cost": 0,
  "best_time_to_depart": "specific departure advice",
  "packing_tips": ["tip 1", "tip 2"],
  "flight_search": {{"from_iata": "DEL", "to_iata": "KUU", "route_note": "..."}},
  "legs": [
    {{
      "leg": 1,
      "from": "exact place name",
      "to": "exact place name",
      "mode": "bus|train|flight|cab|shared_cab|auto|metro|walk|ferry",
      "operator_hint": "HRTC / Volvo / IndiGo / local taxi",
      "departure_time_approx": "22:00 or Flexible or Overnight",
      "duration_approx": "Xh Ym",
      "distance_km": 0,
      "estimated_cost_per_person": 0,
      "booking_channel": "RedBus / IRCTC / counter / local stand",
      "tips": "one practical tip",
      "notes": "important detail",
      "needs_live_search": true,
      "search_params": {{
        "origin": "bare city name",
        "destination": "bare city name",
        "mode_type": "bus|train|flight",
        "from_station_code": "NDLS",
        "to_station_code": "CDG",
        "from_iata": null,
        "to_iata": null
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
        print(f"[journey] Gemini route parse error: {e}")
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
            leg["estimated_cost_per_person"] = live_fare
            leg["cost_source"] = "live"
            transport_per_person += live_fare
            breakdown[leg_key] = live_fare
        else:
            # Keep Gemini estimate (cab, shared_cab, walk, etc.)
            est = leg.get("estimated_cost_per_person") or 0
            leg["cost_source"] = "estimated"
            transport_per_person += est
            breakdown[leg_key] = est

    # Hotel cost from live data
    hotel_per_night: int | None = None
    hotel_total = 0
    if hotel_result and not isinstance(hotel_result, Exception):
        hotels = hotel_result.get("hotels") or []
        priced = [h.get("price_per_night") for h in hotels if h.get("price_per_night")]
        if priced:
            hotel_per_night = min(priced)
            hotel_total = hotel_per_night * stay_nights
            breakdown["hotel"] = hotel_total

    total_per_person = transport_per_person + hotel_total
    total_trip = total_per_person * travelers

    return {
        "total_per_person": total_per_person,
        "transport_per_person": transport_per_person,
        "hotel_per_night": hotel_per_night,
        "hotel_total": hotel_total,
        "total_for_all_travelers": total_trip,
        "travelers": travelers,
        "stay_nights": stay_nights,
        "breakdown": breakdown,
        "source": "live+estimated",
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


def _pick_recommended_bus(buses: list[dict], budget: str) -> dict | None:
    """
    Pick bus that arrives closest to 11:00 AM (checkin time).
    Among equally-close arrivals, prefer cheaper options for low budget.
    Returns a shallow copy with is_recommended=True.
    """
    if not buses:
        return None
    target_minutes = 11 * 60   # 11:00 AM in minutes from midnight
    scored: list[tuple[int, int | float, dict]] = []
    for bus in buses:
        arrival = bus.get("arrival", "")
        fare = bus.get("fare_discounted") or bus.get("fare_original") or 999_999
        try:
            arr_dt = datetime.strptime(arrival, "%Y-%m-%d %H:%M:%S")
            diff = abs(arr_dt.hour * 60 + arr_dt.minute - target_minutes)
        except Exception:
            diff = 999_999
        scored.append((diff, fare if budget == "low" else 0, bus))

    scored.sort(key=lambda x: (x[0], x[1]))
    pick = dict(scored[0][2])
    pick["is_recommended"] = True
    return pick


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
    if not flights:
        return None
    pick = dict(min(flights, key=lambda f: f.get("price_total") or 999_999))
    pick["is_recommended"] = True
    return pick


# ── Main handler ──────────────────────────────────────────────────────────────

async def run_plan_journey(body: PlanJourneyRequest, user_id: str) -> dict:
    db = get_db()
    safe_budget = body.budget if body.budget in ("low", "mid", "high") else "mid"

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
            if not sp.get("from_station_code"):
                _enqueue("train", o, resolve_station_async, o)
            if not sp.get("to_station_code"):
                _enqueue("train", d, resolve_station_async, d)
        elif mode == "flight":
            if not sp.get("from_iata"):
                _enqueue("flight", o, _flights.resolve_iata, o)
            if not sp.get("to_iata"):
                _enqueue("flight", d, _flights.resolve_iata, d)

    # Always resolve IATA for overall origin/destination (flight alternatives panel)
    _enqueue("flight", body.origin,      _flights.resolve_iata, body.origin)
    _enqueue("flight", body.destination, _flights.resolve_iata, body.destination)

    if _resolve_coros:
        _resolve_results = await asyncio.gather(*_resolve_coros, return_exceptions=True)
    else:
        _resolve_results = []

    resolved: dict[str, object] = {}
    for k, v in zip(_resolve_keys, _resolve_results):
        resolved[k] = None if isinstance(v, Exception) else v

    # Log pre-resolution results
    print(f"\n[plan_journey] ── {body.origin} → {body.destination} ──")
    print(f"  Autocomplete: {len(resolved)} identifiers resolved")
    for k, v in resolved.items():
        prefix, city = k.split("|", 1)
        print(f"    [{prefix}] {city!r} → {v or '✗ not found'}")

    # ── Phase 2b: Build search tasks using pre-resolved IDs ───────────────────
    live_meta: list[tuple[int, str]] = []
    live_coros: list = []

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
                    fallback={"buses": [], "resolved": False},
                ))
            else:
                leg["has_live_data"] = False
                leg["live_search_note"] = (
                    f"No bus stop found for {'origin' if not from_id else 'destination'}: "
                    f"{origin_s if not from_id else dest_s}"
                )

        elif mode in ("train", "railway"):
            from_code = sp.get("from_station_code") or resolved.get(f"train|{origin_s.lower()}")
            to_code   = sp.get("to_station_code")   or resolved.get(f"train|{dest_s.lower()}")
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
            from_iata = sp.get("from_iata") or resolved.get(f"flight|{origin_s.lower()}")
            to_iata   = sp.get("to_iata")   or resolved.get(f"flight|{dest_s.lower()}")
            if from_iata and to_iata:
                # Persist back so booking URL builder has the codes
                sp["from_iata"] = from_iata
                sp["to_iata"]   = to_iata
                live_meta.append((i, "flight"))
                live_coros.append(with_retry(
                    _flights.search, origin_s, dest_s, departure_date,
                    adults=body.travelers,
                    from_iata=from_iata, to_iata=to_iata,
                    fallback={"flights": [], "resolved": False},
                ))
            else:
                leg["has_live_data"] = False
                leg["live_search_note"] = (
                    f"No airport found for {'origin' if not from_iata else 'destination'}: "
                    f"{origin_s if not from_iata else dest_s}"
                )

    # ── Phase 2c: Flight alternatives + hotels at FINAL destination ───────────
    fs = route_plan.get("flight_search") or {}
    flight_from_iata = (
        fs.get("from_iata")
        or resolved.get(f"flight|{body.origin.lower()}")
        or ""
    )
    flight_to_iata = (
        fs.get("to_iata")
        or resolved.get(f"flight|{body.destination.lower()}")
        or ""
    )
    flight_alt_coro = (
        _search_flight_alternatives(flight_from_iata, flight_to_iata, departure_date, body.travelers)
        if departure_date and flight_from_iata and flight_to_iata else None
    )

    # Train alternatives — always searched for origin→destination regardless of Gemini's mode choice
    train_alt_coro = (
        _search_train_alternatives(body.origin, body.destination, departure_date, body.travelers)
        if departure_date else None
    )

    hotel_coro = None
    checkin = checkout = None
    if departure_date:
        try:
            checkin  = (datetime.fromisoformat(departure_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            checkout = (datetime.fromisoformat(departure_date) + timedelta(days=1 + body.stay_nights)).strftime("%Y-%m-%d")
            hotel_coro = with_retry(
                _hotels.search, body.destination, checkin, checkout,
                adults=body.travelers, budget=safe_budget,
                fallback={"hotels": [], "source": "none"},
            )
        except Exception:
            hotel_coro = None

    extra_coros = [c for c in [flight_alt_coro, train_alt_coro, hotel_coro] if c]
    all_results = await asyncio.gather(*(live_coros + extra_coros), return_exceptions=True) if (live_coros + extra_coros) else []

    transport_results = all_results[: len(live_coros)]
    extras = list(all_results[len(live_coros):])

    ei = 0
    flight_alt_result = extras[ei] if flight_alt_coro and ei < len(extras) else None
    if flight_alt_coro: ei += 1
    train_alt_result  = extras[ei] if train_alt_coro  and ei < len(extras) else None
    if train_alt_coro:  ei += 1
    hotel_result      = extras[ei] if hotel_coro      and ei < len(extras) else None

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
            flights = result.get("flights") or []
            rec = _pick_recommended_flight(flights, safe_budget)
            if rec and not _rec_flight:
                _rec_flight = rec
                for f in flights:
                    if f.get("flight_number") == rec.get("flight_number") and f.get("departure_time") == rec.get("departure_time"):
                        f["is_recommended"] = True

        elif mode_type == "train":
            trains = result.get("trains") or []
            if trains and not _rec_train:
                _rec_train = dict(trains[0])
                _rec_train["is_recommended"] = True
                trains[0]["is_recommended"] = True

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
    route_plan["total_estimated_cost"]   = live_costs["total_per_person"]
    route_plan["cost_per_person"]        = live_costs["total_per_person"]
    route_plan["cost_for_all_travelers"] = live_costs["total_for_all_travelers"]
    route_plan["cost_breakdown"]         = live_costs

    live_duration = _compute_live_duration(legs)
    if live_duration:
        route_plan["total_duration_approx"] = live_duration
        route_plan["duration_source"] = "live"

    route_plan["legs"] = legs
    route_plan["currency"] = symbol
    route_plan["has_real_data"] = any(leg.get("has_live_data") for leg in legs)

    fa_flights = flight_alt_result if flight_alt_result and not isinstance(flight_alt_result, Exception) else []
    # Mark recommended in flight alternatives too
    fa_rec = _pick_recommended_flight(fa_flights, safe_budget)
    if fa_rec:
        for f in fa_flights:
            if f.get("flight_number") == fa_rec.get("flight_number") and f.get("departure_time") == fa_rec.get("departure_time"):
                f["is_recommended"] = True
        if not _rec_flight:
            _rec_flight = fa_rec

    route_plan["flight_alternatives"] = {
        "from_iata": flight_from_iata,
        "to_iata": flight_to_iata,
        "route_note": fs.get("route_note", ""),
        "flights": fa_flights,
        "best_pick": fa_rec,
    }

    if train_alt_result and not isinstance(train_alt_result, Exception):
        trains_alt = train_alt_result.get("trains") or []
        if trains_alt:
            # Mark cheapest train
            cheapest = min(trains_alt, key=lambda t: t.get("cheapest_fare") or 999_999)
            cheapest["is_recommended"] = True
            if not _rec_train:
                _rec_train = dict(cheapest)
        route_plan["train_alternatives"] = {
            "from_station": train_alt_result.get("from_code"),
            "to_station":   train_alt_result.get("to_code"),
            "trains": trains_alt,
            "best_pick": dict(cheapest) if trains_alt else None,
            "note": "Direct trains — may have long journey time vs flight.",
        }

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
            "hotels": ranked[:8],
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
            "Bus picked for ~11 AM arrival (hotel checkin). "
            "Hotel and transport sorted by your budget tier."
        ),
    }

    # Summary log
    _fa = route_plan.get("flight_alternatives") or {}
    _ta = route_plan.get("train_alternatives") or {}
    _h  = route_plan.get("destination_hotels")
    live_hits  = sum(1 for leg in legs if leg.get("has_live_data"))
    live_total = sum(1 for leg in legs if leg.get("needs_live_search"))
    print(
        f"  Gemini:    ✓ {len(legs)}-leg route | {route_plan.get('total_duration_approx')}\n"
        f"  Live data: {live_hits}/{live_total} legs resolved"
    )
    for leg in legs:
        ld = leg.get("live_data") or {}
        count = len(ld.get("buses") or ld.get("trains") or ld.get("flights") or [])
        needs = leg.get("needs_live_search")
        status = f"✓ {count}" if leg.get("has_live_data") else ("✗ 0" if needs else "— n/a")
        note = leg.get("live_search_note", "")
        print(f"    Leg {leg.get('leg')} [{leg.get('mode')}] {leg.get('from')} → {leg.get('to')}: {status}{' (' + note + ')' if note else ''}")
    print(
        f"  Flights:   {len(_fa.get('flights') or [])} alts ({_fa.get('from_iata')} → {_fa.get('to_iata')})\n"
        f"  Trains:    {len(_ta.get('trains') or [])} alts ({_ta.get('from_station')} → {_ta.get('to_station')})\n"
        f"  Hotels:    {(_h['source'] + ' | ' + str(len(_h.get('hotels') or [])) + ' @ ' + _h.get('destination','?')) if _h else '✗ none'}\n"
        f"──────────────────────────────────────────────────────────"
    )

    return route_plan
