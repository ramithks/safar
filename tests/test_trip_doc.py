"""
Offline unit tests for the TripDoc layer (docs/tripstate_removal_plan.md):
graph → doc serialization, budget math, instant selections, budget re-rank,
and selection carry-forward across regenerations. No DB, no network.
"""
import pytest

from app.trip_state import graph as G
from app.trips_doc import select
from app.trips_doc.generate import initial_doc
from app.trips_doc.serialize import (
    carry_forward_selections,
    compute_budget,
    doc_from_graph,
    rerank_for_budget,
    trim_hotel,
    trim_transport_option,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

def _hotels():
    return [
        {"id": "h1", "name": "Snow View", "price_per_night": 2000, "currency": "INR",
         "review_score": 8.2, "review_label": "Very good", "photos": ["http://p/1.jpg"],
         "amenities": ["wifi", "parking"], "source": "agoda",
         "lat": 32.24, "lon": 77.18, "is_recommended": True,
         "ai_reason": "Best balance of price and location",
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
    assert h["lat"] == 32.24 and h["lon"] == 77.18    # map coords survive
    assert h["ai_reason"].startswith("Best balance")  # agent note survives
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
    assert stop["hotels"]["recommended_id"] == "h1"     # the agent's is_recommended pick


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



# ── itinerary day editing ────────────────────────────────────────────────────

def test_edit_day_activities_replaces_add_reorder_delete():
    from app.trips_doc.itinerary import edit_day_activities
    doc = _doc()
    new = [{"title": "Hadimba Temple", "place_id": "x1"},
           {"title": "Old Manali Cafe", "place_id": "x2"}]
    doc, note = edit_day_activities(doc, "stop-1", 0, new)
    assert doc["itinerary"][0]["days"][0]["activities"] == new
    assert "Manali" in note and "2 places" in note
    # city-name targeting works like hotel selection
    doc, _ = edit_day_activities(doc, "Manali", 0, [])
    assert doc["itinerary"][0]["days"][0]["activities"] == []


def test_edit_day_activities_rejects_bad_targets():
    import pytest as _pytest
    from app.trips_doc.itinerary import edit_day_activities
    with _pytest.raises(ValueError):
        edit_day_activities(_doc(), "stop-9", 0, [])
    with _pytest.raises(ValueError):
        edit_day_activities(_doc(), "stop-1", 7, [])
    with _pytest.raises(ValueError):
        edit_day_activities(_doc(), "stop-1", 0, [{"no_title": True}])
    with _pytest.raises(ValueError):
        edit_day_activities(_doc(), "stop-1", 0, [{"title": f"p{i}"} for i in range(16)])


def test_shape_place_builds_renderable_activity():
    from app.trips_doc.itinerary import _shape_place
    a = _shape_place({
        "name": "Rock Garden", "place_id": "pid1", "rating": 4.5,
        "user_ratings_total": 1200, "formatted_address": "Sector 1, Chandigarh",
        "geometry": {"location": {"lat": 30.75, "lng": 76.8}},
        "photo_reference": "REF", "types": ["tourist_attraction"],
    })
    assert a["title"] == "Rock Garden" and a["lat"] == 30.75
    assert a["photo_url"] == "/api/v1/images/REF"
    assert a["maps_url"].endswith("place_id:pid1")
    assert a["source"] == "user_added"
