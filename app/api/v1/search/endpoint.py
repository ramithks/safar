"""
Quick-search endpoints — standalone transport + hotel search (no trip creation).

Routes:
  POST /api/v1/search/flights    — one-way flight search
  POST /api/v1/search/hotels     — hotel availability search
  POST /api/v1/search/buses      — bus route search
  POST /api/v1/search/trains     — Indian train search
  POST /api/v1/search/all        — flights + buses + trains (+ hotels) in parallel
"""
import asyncio
from fastapi import APIRouter, Depends

from app.deps.auth import require_user
from app.agents.recommendation_agent import recommend_flights, recommend_hotels
from app.services.flights import FlightService, budget_to_sort, sort_flights
from app.services.hotels import HotelService
from app.services.hotels_filters import budget_to_hotel_sort
from app.services.transport import TransportService

from .models import (
    FlightSearchRequest,
    HotelSearchRequest,
    BusSearchRequest,
    TrainSearchRequest,
    AllSearchRequest,
)

router = APIRouter(prefix="/search", tags=["quick-search"])

_flights   = FlightService()
_hotels    = HotelService()
_transport = TransportService()


# ── Sort helpers ──────────────────────────────────────────────────────────────
# Flight sorting lives in app.services.flights (sort_flights / budget_to_sort) so
# the standalone search and the trip planner stay in lock-step.

def _sort_hotels(hotels: list[dict], sort: str) -> list[dict]:
    if sort == "price_asc":
        return sorted(hotels, key=lambda h: (h.get("price_per_night") or h.get("estimated_price_per_night") or 999999))
    if sort == "price_desc":
        return sorted(hotels, key=lambda h: -(h.get("price_per_night") or h.get("estimated_price_per_night") or 0))
    if sort == "rating":
        return sorted(hotels, key=lambda h: -(
            float(h.get("review_score") or h.get("star_rating") or h.get("google_rating") or 0)
        ))
    return hotels  # recommended — already ordered by HotelService richness score


def _sort_buses(buses: list[dict], sort: str) -> list[dict]:
    if sort == "earliest":
        return sorted(buses, key=lambda b: (b.get("departure_time") or "99:99"))
    if sort == "latest":
        return sorted(buses, key=lambda b: -(ord((b.get("departure_time") or "00:00")[0])))
    if sort == "rating":
        return sorted(buses, key=lambda b: -(float(b.get("rating") or 0)))
    # cheapest
    return sorted(buses, key=lambda b: (b.get("price") is None, b.get("price") or 99999))


def _sort_trains(trains: list[dict], sort: str) -> list[dict]:
    if sort == "fastest":
        return sorted(trains, key=lambda t: (t.get("duration_min") or 9999))
    if sort == "earliest":
        return sorted(trains, key=lambda t: (t.get("departure_time") or "99:99"))
    # cheapest
    return sorted(trains, key=lambda t: (t.get("cheapest_fare") is None, t.get("cheapest_fare") or 99999))


# ── Recommended helper ────────────────────────────────────────────────────────

def _tag_recommended(items: list[dict]) -> list[dict]:
    """Mark the first item (best by the applied sort) as is_recommended=True."""
    for i, item in enumerate(items):
        item["is_recommended"] = (i == 0)
    return items


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/flights")
async def search_flights(body: FlightSearchRequest, _user=Depends(require_user)):
    """
    Search one-way flights. Results from Booking.com + EaseMyTrip + Gotogate (parallel, deduplicated).
    sort: CHEAPEST | FASTEST | BEST. If `budget` (low|mid|high) is set it dictates
    the sort and overrides `sort`. Returns all merged results; first flagged is_recommended.
    Pass `from_iata`/`to_iata` (from /autocomplete/airports) to skip city→IATA resolution.
    """
    # Budget always wins: a budget tier dictates sort, overriding any explicit sort.
    effective_sort = budget_to_sort(body.budget) or body.sort
    result = await _flights.search(
        origin=body.origin,
        destination=body.destination,
        date=body.date,
        adults=body.adults,
        cabin_class=body.cabin_class,
        sort=effective_sort,   # pushed upstream to each provider, then reranked
        # IATA pre-resolved by /autocomplete/airports → skip the resolver round-trip.
        from_iata=(body.from_iata or "").strip().upper() or None,
        to_iata=(body.to_iata or "").strip().upper() or None,
        nonstop_only=body.nonstop_only,
    )
    flights = _tag_recommended(sort_flights(result.get("flights") or [], effective_sort))
    cheapest = min(
        (f.get("price_total") for f in flights if f.get("price_total")),
        default=None,
    )
    top = flights[0] if flights else None
    recommended = {
        "airline": top.get("airline"),
        "flight_number": top.get("flight_number"),
        "price": top.get("price_total"),
        "duration_min": top.get("duration_min"),
        "stops": top.get("stops", 0),
        "departure_time": top.get("departure_time"),
        "arrival_time": top.get("arrival_time"),
        "booking_url": top.get("booking_url"),
    } if top else None
    return {
        **result,
        "flights": flights,
        "recommended": recommended,
        "cheapest_price": cheapest,
        "total": len(flights),
        "sort_applied": effective_sort,
    }


@router.post("/hotels")
async def search_hotels(body: HotelSearchRequest, _user=Depends(require_user)):
    """
    Search hotel availability. Results from EaseMyTrip + Booking.com (parallel).
    sort: recommended | price_asc | price_desc | rating
    Returns all deduplicated results; first item flagged is_recommended=true.
    """
    result = await _hotels.search(
        destination=body.destination,
        checkin=body.checkin,
        checkout=body.checkout,
        rooms=body.rooms,
        adults=body.adults,
        children=body.children,
        budget=body.budget,
        travel_type=body.travel_type,
    )
    # Budget always wins: a budget tier dictates sort, overriding any explicit sort.
    effective_sort = budget_to_hotel_sort(body.budget) or body.sort
    hotels = _tag_recommended(_sort_hotels(result.get("hotels") or [], effective_sort))
    cheapest = min(
        (
            h.get("price_per_night") or h.get("estimated_price_per_night")
            for h in hotels
            if h.get("price_per_night") or h.get("estimated_price_per_night")
        ),
        default=None,
    )
    top = hotels[0] if hotels else None
    recommended = {
        "name": top.get("name"),
        "property_type": top.get("property_type"),
        "price_per_night": top.get("price_per_night") or top.get("estimated_price_per_night"),
        "review_score": top.get("review_score") or top.get("star_rating") or top.get("google_rating"),
        "review_label": top.get("review_label"),
        "free_cancellation": top.get("free_cancellation"),
        "photos": (top.get("photos") or [])[:1],
        "booking_url": top.get("booking_url"),
        "source": top.get("source"),
    } if top else None
    return {
        **result,
        "hotels": hotels,
        "recommended": recommended,
        "cheapest_price_per_night": cheapest,
        "total": len(hotels),
        "sort_applied": effective_sort,
    }


def _attach_recommendation(items: list[dict], ai: dict | None) -> dict | None:
    """
    Map an agent recommendation back onto the real result list: stamp the chosen
    item (and runner-up) with the reason/pros, re-flag is_recommended on the pick,
    and return the top-level ai_recommendation block. Returns None when there's no
    usable recommendation (caller keeps the deterministic recommended item).
    """
    if not ai or not items:
        return None
    idx = ai["recommended_index"]
    # Re-point is_recommended at the agent's pick (search already flagged items[0]).
    for it in items:
        it["is_recommended"] = False
    items[idx]["is_recommended"] = True
    items[idx]["ai_reason"] = ai.get("reason")
    items[idx]["ai_pros"] = ai.get("pros")
    items[idx]["ai_best_for"] = ai.get("best_for")
    ru = ai.get("runner_up_index")
    if isinstance(ru, int) and 0 <= ru < len(items):
        items[ru]["ai_reason"] = ai.get("runner_up_reason")
    return {
        "recommended_index": idx,
        "reason": ai.get("reason"),
        "pros": ai.get("pros"),
        "best_for": ai.get("best_for"),
        "runner_up_index": ru,
        "runner_up_reason": ai.get("runner_up_reason"),
    }


@router.post("/flights/smart")
async def search_flights_smart(body: FlightSearchRequest, _user=Depends(require_user)):
    """
    Agent-assisted flight search. Runs the normal /flights search, then asks the
    recommendation agent which option to book and *why*. Adds an `ai_recommendation`
    block (reason, pros, best_for, runner-up) and stamps the chosen flight with
    `ai_reason`/`ai_pros` + is_recommended. Falls back to the deterministic
    recommendation if the agent is unavailable.
    """
    base = await search_flights(body, _user)
    flights = base.get("flights") or []
    ai = await recommend_flights(flights, context={
        "route": f"{base.get('from_iata') or body.origin} → {base.get('to_iata') or body.destination}",
        "date": body.date,
        "passengers": body.adults,
        "cabin_class": body.cabin_class,
        "nonstop_only": body.nonstop_only,
        "sort": base.get("sort_applied"),
    })
    rec = _attach_recommendation(flights, ai)
    if rec:
        base["ai_recommendation"] = rec
    return base


@router.post("/hotels/smart")
async def search_hotels_smart(body: HotelSearchRequest, _user=Depends(require_user)):
    """
    Agent-assisted hotel search. Runs the normal /hotels search, then asks the
    recommendation agent which stay to book and *why*. Adds an `ai_recommendation`
    block (reason, pros, best_for, runner-up) and stamps the chosen hotel with
    `ai_reason`/`ai_pros` + is_recommended. Falls back to the deterministic
    recommendation if the agent is unavailable.
    """
    base = await search_hotels(body, _user)
    hotels = base.get("hotels") or []
    ai = await recommend_hotels(hotels, context={
        "destination": body.destination,
        "checkin": body.checkin,
        "checkout": body.checkout,
        "rooms": body.rooms,
        "adults": body.adults,
        "children": body.children,
        "budget": body.budget,
        "sort": base.get("sort_applied"),
    })
    rec = _attach_recommendation(hotels, ai)
    if rec:
        base["ai_recommendation"] = rec
    return base


@router.post("/buses")
async def search_buses(body: BusSearchRequest, _user=Depends(require_user)):
    """
    Search buses. Results from RedBus (primary) + FlixBus (fills gaps).
    sort: cheapest | earliest | latest | rating
    Returns all results; first item flagged is_recommended=true.
    """
    result = await _transport.search_buses(
        origin=body.origin,
        destination=body.destination,
        date=body.date,
        travelers=body.travelers,
        limit=body.limit,
    )
    buses = _tag_recommended(_sort_buses(result.get("buses") or [], body.sort))
    cheapest = min(
        (b.get("price") for b in buses if b.get("price")),
        default=None,
    )
    top = buses[0] if buses else None
    recommended = {
        "operator": top.get("operator") or top.get("bus_type") or "Bus",
        "price": top.get("price"),
        "departure_time": top.get("departure_time"),
        "arrival_time": top.get("arrival_time"),
        "duration": top.get("duration"),
        "available_seats": top.get("available_seats"),
        "rating": top.get("rating"),
        "booking_url": top.get("booking_url"),
    } if top else None
    return {
        **result,
        "buses": buses,
        "recommended": recommended,
        "cheapest_price": cheapest,
        "total": len(buses),
        "sort_applied": body.sort,
    }


@router.post("/trains")
async def search_trains(body: TrainSearchRequest, _user=Depends(require_user)):
    """
    Search Indian trains via ConfirmTKT.
    sort: cheapest | fastest | earliest
    Returns all results; first item flagged is_recommended=true.
    """
    result = await _transport.search_trains(
        origin=body.origin,
        destination=body.destination,
        date=body.date,
        limit=body.limit,
    )
    trains = _tag_recommended(_sort_trains(result.get("trains") or [], body.sort))
    cheapest = min(
        (t.get("cheapest_fare") for t in trains if t.get("cheapest_fare")),
        default=None,
    )
    top = trains[0] if trains else None
    recommended = {
        "train_number": top.get("train_number"),
        "train_name": top.get("train_name"),
        "price": top.get("cheapest_fare"),
        "cheapest_class": top.get("cheapest_class"),
        "departure_time": top.get("departure_time"),
        "arrival_time": top.get("arrival_time"),
        "duration": top.get("duration"),
        "booking_url": top.get("booking_url"),
    } if top else None
    return {
        **result,
        "trains": trains,
        "recommended": recommended,
        "cheapest_fare": cheapest,
        "total": len(trains),
        "sort_applied": body.sort,
    }


@router.post("/all")
async def search_all(body: AllSearchRequest, _user=Depends(require_user)):
    """
    Run flights + buses + trains (+ hotels if checkin/checkout provided) in parallel.
    Returns best option per mode under `recommendations` + full lists under `results`.
    Include checkin + checkout in the request body to also get hotel results.
    """
    want_hotels = bool(body.checkin and body.checkout)

    transport_task = _transport.search_all(
        origin=body.origin,
        destination=body.destination,
        date=body.date,
        travelers=body.travelers,
        modes=body.modes,
    )

    if want_hotels:
        transport_result, hotels_result = await asyncio.gather(
            transport_task,
            _hotels.search(
                destination=body.destination,
                checkin=body.checkin,
                checkout=body.checkout,
                rooms=body.rooms,
                adults=body.travelers,
            ),
            return_exceptions=True,
        )
    else:
        transport_result = await transport_task
        hotels_result = None

    if isinstance(transport_result, Exception):
        transport_result = {}
    if isinstance(hotels_result, Exception):
        hotels_result = None

    def _best_flight(r: dict) -> dict | None:
        flights = r.get("flights") or []
        priced  = [f for f in flights if f.get("price_total")]
        return min(priced, key=lambda f: f["price_total"]) if priced else (flights[0] if flights else None)

    def _best_bus(r: dict) -> dict | None:
        buses = r.get("buses") or []
        priced = [b for b in buses if b.get("price")]
        return min(priced, key=lambda b: b["price"]) if priced else (buses[0] if buses else None)

    def _best_train(r: dict) -> dict | None:
        trains = r.get("trains") or []
        priced  = [t for t in trains if t.get("cheapest_fare")]
        return min(priced, key=lambda t: t["cheapest_fare"]) if priced else (trains[0] if trains else None)

    def _best_hotel(hotels: list[dict]) -> dict | None:
        priced = [h for h in hotels if h.get("price_per_night") or h.get("estimated_price_per_night")]
        if not priced:
            return hotels[0] if hotels else None
        # richness-sorted already; among priced take the first (best data)
        return priced[0]

    recommendations: dict = {}

    if "flights" in transport_result:
        best = _best_flight(transport_result["flights"])
        if best:
            recommendations["flight"] = {
                "type": "flight",
                "label": f"{best.get('airline', '')} {best.get('flight_number', '')}".strip(),
                "price": best.get("price_total"),
                "duration_min": best.get("duration_min"),
                "departure_time": best.get("departure_time"),
                "stops": best.get("stops", 0),
                "booking_url": best.get("booking_url"),
            }

    if "buses" in transport_result:
        best = _best_bus(transport_result["buses"])
        if best:
            recommendations["bus"] = {
                "type": "bus",
                "label": best.get("operator") or best.get("bus_type") or "Bus",
                "price": best.get("price"),
                "departure_time": best.get("departure_time"),
                "duration": best.get("duration"),
                "available_seats": best.get("available_seats"),
                "booking_url": best.get("booking_url"),
            }

    if "trains" in transport_result:
        best = _best_train(transport_result["trains"])
        if best:
            recommendations["train"] = {
                "type": "train",
                "label": best.get("train_name") or f"Train {best.get('train_number', '')}",
                "price": best.get("cheapest_fare"),
                "departure_time": best.get("departure_time"),
                "duration": best.get("duration"),
                "train_number": best.get("train_number"),
                "booking_url": best.get("booking_url"),
            }

    if want_hotels and hotels_result:
        hotels_list = hotels_result.get("hotels") or []
        best = _best_hotel(hotels_list)
        if best:
            recommendations["hotel"] = {
                "type": "hotel",
                "label": best.get("name"),
                "price_per_night": best.get("price_per_night") or best.get("estimated_price_per_night"),
                "review_score": best.get("review_score") or best.get("google_rating"),
                "free_cancellation": best.get("free_cancellation"),
                "photos": (best.get("photos") or [])[:1],
                "booking_url": best.get("booking_url"),
                "source": best.get("source"),
            }

    results = dict(transport_result)
    if want_hotels and hotels_result:
        results["hotels"] = hotels_result

    return {
        "origin": body.origin,
        "destination": body.destination,
        "date": body.date,
        "travelers": body.travelers,
        "recommendations": recommendations,
        "results": results,
    }
