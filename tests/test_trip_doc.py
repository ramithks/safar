"""
Offline unit tests for the TripDoc layer (docs/tripstate_removal_plan.md):
graph → doc serialization, budget math, instant selections, budget re-rank,
selection carry-forward across regenerations, spec actions, and legacy
state → doc conversion. No DB, no network.
"""
import asyncio

import pytest

from app.trip_state import graph as G
from app.trips_doc import convert, select
from app.trips_doc.generate import initial_doc
from app.trips_doc.serialize import (
    carry_forward_selections,
    compute_budget,
    doc_from_graph,
    rerank_for_budget,
    trim_hotel,
    trim_transport_option,
)


def _run(coro):
    return asyncio.run(coro)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _hotels():
    return [
        {"id": "h1", "name": "Snow View", "price_per_night": 2000, "currency": "INR",
         "review_score": 8.2, "review_label": "Very good", "photos": ["http://p/1.jpg"],
         "amenities": ["wifi", "parking"], "source": "agoda",
         "raw": {"huge": "provider blob"}},
        {"id": "h2", "name": "Pine Resort", "price_per_night": 3000, "currency": "INR",
         "review_score": 9.0, "photos": [{"url": "http://p/2.jpg"}], "source": "booking"},
    ]


def _options(prefix="go"):
    return [
        G.TransportOption(
            id=f"{prefix}-flight-0", mode="flight", operator="IndiGo",
            departure="2026-08-01T10:00", arrival="2026-08-01T11:30",
            duration_min=90, fare_per_person=3500,
            raw={"airline": "IndiGo", "flight_number": "6E-123",
                 "booking_url": "http://book/f", "seats_left": 5}),
        G.TransportOption(
            id=f"{prefix}-train-0", mode="train", operator="IR",
            departure="2026-08-01T06:00", arrival="2026-08-01T11:00",
            duration_min=300, fare_per_person=800,
            raw={"train_name": "Shatabdi", "train_number": "12005"}),
    ]


def _graph():
    origin = G.TripStop(id="stop-0", city="Delhi", role="origin")
    term = G.TripStop(
        id="stop-1", city="Manali", role="terminal", nights=2,
        arrival_date="2026-08-01", departure_date="2026-08-03",
        days=[{"day": 1, "title": "Old Manali"}],
        stay=G.Stay(search_results=_hotels()),
    )
    going = G.TripLeg(
        id="leg-1", from_node_id="stop-0", to_node_id="stop-1",
        from_city="Delhi", to_city="Manali", date="2026-08-01",
        routes=[G.RouteCandidate(id="leg-1-direct", hops=[
            G.RouteHop(from_city="Delhi", to_city="Manali",
                       date="2026-08-01", options=_options("go"))])],
    )
    ret = G.TripLeg(
        id="leg-2", from_node_id="stop-1", to_node_id="stop-0",
        from_city="Manali", to_city="Delhi", date="2026-08-03",
        routes=[G.RouteCandidate(id="leg-2-direct", hops=[
            G.RouteHop(from_city="Manali", to_city="Delhi",
                       date="2026-08-03", options=_options("ret"))])],
    )
    return G.TripGraph(nodes=[origin, term], edges=[going, ret])


def _spec():
    return {"origin": "Delhi", "destination": "Manali",
            "departure_date": "2026-08-01", "days": 3, "travelers": 2,
            "budget_tier": "mid", "interests": [], "stops": [], "version": 1}


def _doc():
    return doc_from_graph("trip-1", _spec(), _graph())


# ── trimming ─────────────────────────────────────────────────────────────────

def test_trim_transport_option_drops_raw_keeps_display_fields():
    t = trim_transport_option(_options()[0])
    assert "raw" not in t and "seats_left" not in t   # non-whitelisted raw dropped
    assert t["service_no"] == "6E-123"
    assert t["flight_number"] == "6E-123"             # whitelisted detail kept
    assert t["airline"] == "IndiGo"
    assert t["booking_url"] == "http://book/f"
    assert t["fare_per_person"] == 3500
    assert t["available"] is True


def test_trim_hotel_drops_blobs_keeps_card_fields():
    h = trim_hotel(_hotels()[0])
    assert "raw" not in h
    assert h["photos"] == ["http://p/1.jpg"]          # capped list kept for cards
    assert h["amenities"] == ["wifi", "parking"]
    assert h["photo"] == "http://p/1.jpg"
    assert h["review_score"] == 8.2                   # original field names kept
    assert h["review_label"] == "Very good"
    # dict-shaped photo entries resolve too
    assert trim_hotel(_hotels()[1])["photo"] == "http://p/2.jpg"


# ── graph → doc ──────────────────────────────────────────────────────────────

def test_doc_from_graph_shape_and_recommendation():
    doc = _doc()
    assert doc["route"] == ["Delhi", "Manali", "Delhi"]
    assert [leg["leg_id"] for leg in doc["transport"]] == ["leg-1", "leg-2"]
    hop = doc["transport"][0]["hops"][0]
    # mid budget → balanced axis → the train wins; recommendation is persisted
    assert hop["recommended_id"] == "go-train-0"
    assert hop["options"][0]["id"] == "go-train-0"
    assert hop.get("selected_id") is None
    # only stayed stops in the itinerary; days + hotels attached
    assert [s["stop_id"] for s in doc["itinerary"]] == ["stop-1"]
    stop = doc["itinerary"][0]
    assert stop["days"][0]["title"] == "Old Manali"
    assert stop["hotels"]["recommended_id"] == "h1"     # cheapest


def test_doc_budget_math():
    doc = _doc()
    b = doc["budget"]
    # recommended train both ways: (800+800) × 2 travelers = 3200
    assert b["breakdown"]["transport"] == 3200
    # cheapest hotel 2000 × 2 nights = 4000
    assert b["breakdown"]["stay"] == 4000
    assert b["breakdown"]["misc"] == 720
    assert b["total"] == 7920
    assert b["per_person"] == 3960


# ── instant selections ───────────────────────────────────────────────────────

def test_select_transport_updates_budget():
    doc, note = select.select_transport(_doc(), "leg-1", "go-flight-0")
    assert doc["transport"][0]["hops"][0]["selected_id"] == "go-flight-0"
    # going flight 3500 + return train 800 = 4300 × 2 = 8600
    assert doc["budget"]["breakdown"]["transport"] == 8600
    assert "IndiGo" in note or "6E-123" in note


def test_select_hotel_updates_budget():
    doc, note = select.select_hotel(_doc(), "stop-1", "h2")
    assert doc["itinerary"][0]["hotels"]["selected_id"] == "h2"
    assert doc["budget"]["breakdown"]["stay"] == 6000   # 3000 × 2 nights
    assert "Pine Resort" in note


def test_select_hotel_accepts_city_name():
    doc, _ = select.select_hotel(_doc(), "Manali", "h1")
    assert doc["itinerary"][0]["hotels"]["selected_id"] == "h1"


def test_select_rejects_unknown_targets():
    with pytest.raises(ValueError):
        select.select_transport(_doc(), "leg-9", "go-train-0")
    with pytest.raises(ValueError):
        select.select_transport(_doc(), "leg-1", "nope")
    with pytest.raises(ValueError):
        select.select_hotel(_doc(), "stop-1", "nope")


# ── budget re-rank (change_budget: no re-search) ─────────────────────────────

def test_rerank_for_budget_flips_recommendation():
    doc = rerank_for_budget(_doc(), "high")            # high → fastest
    hop = doc["transport"][0]["hops"][0]
    assert hop["recommended_id"] == "go-flight-0"
    assert doc["spec"]["budget_tier"] == "high"
    # flight both ways now drives the recommended budget
    assert doc["budget"]["breakdown"]["transport"] == (3500 + 3500) * 2


def test_rerank_keeps_explicit_selection():
    doc, _ = select.select_transport(_doc(), "leg-1", "go-train-0")
    doc = rerank_for_budget(doc, "high")
    hop = doc["transport"][0]["hops"][0]
    assert hop["selected_id"] == "go-train-0"          # user pick survives
    # selection wins over recommendation in the budget
    assert doc["budget"]["breakdown"]["transport"] == (800 + 3500) * 2


# ── carry-forward across regenerations ───────────────────────────────────────

def test_carry_forward_keeps_picks_by_id_and_identity():
    prior, _ = select.select_transport(_doc(), "leg-1", "go-train-0")
    prior, _ = select.select_hotel(prior, "stop-1", "h2")

    fresh = _doc()
    # same ride, new option id — must match by identity (mode+service+clock)
    hop = fresh["transport"][0]["hops"][0]
    for o in hop["options"]:
        if o["id"] == "go-train-0":
            o["id"] = "go-train-NEW"
    dropped = carry_forward_selections(prior, fresh)
    assert dropped == []
    assert fresh["transport"][0]["hops"][0]["selected_id"] == "go-train-NEW"
    assert fresh["itinerary"][0]["hotels"]["selected_id"] == "h2"


def test_carry_forward_reports_dropped_picks():
    prior, _ = select.select_transport(_doc(), "leg-1", "go-flight-0")
    fresh = _doc()
    hop = fresh["transport"][0]["hops"][0]
    hop["options"] = [o for o in hop["options"] if o["mode"] != "flight"]
    hop["recommended_id"] = hop["options"][0]["id"]
    dropped = carry_forward_selections(prior, fresh)
    assert len(dropped) == 1 and "Delhi → Manali" in dropped[0]
    assert fresh["transport"][0]["hops"][0].get("selected_id") is None


# ── initial doc + staleness marker ───────────────────────────────────────────

def test_initial_doc_route_and_status():
    doc = initial_doc(_spec())
    assert doc["status"] == "planning" and doc["progress"] == 5
    assert doc["route"] == ["Delhi", "Manali", "Delhi"]


def test_needs_conversion_rules():
    legacy_state = {"meta": {"status": "ready"}}
    # legacy trip, no doc yet
    assert convert.needs_conversion({"doc": None, "state": legacy_state, "last_seq": 3})
    # converted and untouched since
    assert not convert.needs_conversion(
        {"doc": {"last_seq_converted": 3}, "state": legacy_state, "last_seq": 3})
    # legacy write landed after conversion
    assert convert.needs_conversion(
        {"doc": {"last_seq_converted": 3}, "state": legacy_state, "last_seq": 5})
    # v3-native trip (empty state) is never reconverted
    assert not convert.needs_conversion({"doc": {"id": "x"}, "state": {}, "last_seq": 0})


# ── legacy state → doc conversion ────────────────────────────────────────────

def _legacy_state():
    return {
        "meta": {"title": "Delhi to Manali", "status": "ready"},
        "request": {"origin": "Delhi", "destination": "Manali",
                    "departure_date": "2026-08-01", "days": 3, "stay_nights": 2,
                    "travelers": 2, "budget_tier": "mid", "interests": []},
        "transport": {
            "going": {"trains": {"items": [
                {"train_name": "Shatabdi", "train_number": "12005",
                 "cheapest_fare": 800, "duration_min": 300,
                 "departure_time": "06:00", "arrival_time": "11:00"}]}},
            "return_journey": {"buses": {"items": [
                {"bus_operator": "HRTC", "price": 900, "duration_min": 700,
                 "departure_time": "20:00", "arrival_time": "07:40"}]}},
        },
        "stay": {"hotels": _hotels(), "selected": _hotels()[1]},
        "itinerary": {"days": [{"day": 1, "title": "Old Manali"}]},
        "weather": {"daily": [{"t": 20}], "location": "Manali"},
        "costs": {"breakdown": {"food": 1500}},
    }


def test_doc_from_legacy_row_converts_graphless_trip():
    row = {"id": "trip-legacy", "title": "Delhi to Manali", "status": "ready",
           "last_seq": 7, "state": _legacy_state(), "created_at": "2026-07-01T00:00:00Z"}
    doc = _run(convert.doc_from_legacy_row(row))
    assert doc["status"] == "ready"
    assert doc["last_seq_converted"] == 7
    assert doc["route"][0] == "Delhi" and "Manali" in doc["route"]
    legs = doc["transport"]
    assert len(legs) == 2
    assert legs[0]["hops"][0]["options"][0]["name"].startswith("Shatabdi")
    # flat legacy stay/days landed on the terminal stop
    stop = doc["itinerary"][0]
    assert stop["days"][0]["title"] == "Old Manali"
    assert stop["hotels"]["selected_id"] == "h2"
    # legacy selected hotel (3000×2) + train 800 + bus 900 → budget carries food
    assert doc["budget"]["breakdown"]["food"] == 1500
    assert doc["budget"]["breakdown"]["stay"] == 6000
    assert doc["weather"]["location"] == "Manali"
