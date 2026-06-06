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
from app.services.flights import FlightService
from app.services.hotels import HotelService
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

def _sort_flights(flights: list[dict], sort: str) -> list[dict]:
    if sort == "FASTEST":
        return sorted(flights, key=lambda f: (f.get("duration_min") or 9999))
    if sort == "BEST":
        def _score(f: dict) -> float:
            price = f.get("price_total") or 99999
            dur   = f.get("duration_min") or 999
            stops = (f.get("stops") or 0) * 60
            return price * 0.6 + (dur + stops) * 0.4
        return sorted(flights, key=_score)
    # default: CHEAPEST
    return sorted(flights, key=lambda f: (f.get("price_total") is None, f.get("price_total") or 99999))


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
    Search one-way flights. Results from Booking.com + EaseMyTrip (parallel, deduplicated).
    sort: CHEAPEST | FASTEST | BEST
    Returns all merged results; first item flagged is_recommended=true.
    """
    result = await _flights.search(
        origin=body.origin,
        destination=body.destination,
        date=body.date,
        adults=body.adults,
        cabin_class=body.cabin_class,
        sort="CHEAPEST",  # always fetch cheapest; client-sort handles UI ordering
        limit=body.limit,
        nonstop_only=body.nonstop_only,
    )
    flights = _tag_recommended(_sort_flights(result.get("flights") or [], body.sort))
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
        "sort_applied": body.sort,
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
        limit=body.limit,
    )
    hotels = _tag_recommended(_sort_hotels(result.get("hotels") or [], body.sort))
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
        "sort_applied": body.sort,
    }


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
                limit=10,
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
