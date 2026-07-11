"""
Offline unit tests for the copilot edit engine and the itinerary-cap changes.

These stub every network call (Google Places, live journey/itinerary pipelines,
Vertex) so the copilot's *logic* — routing dispatch, cost recompute, spot
insertion, waypoint splicing, hotel auto-select — is exercised deterministically.
The real Bhiwani→Meghalaya flow lives in scripts/copilot_e2e_bhiwani_meghalaya.py.
"""
import asyncio

from app.trip_state import copilot, structural
from app.trip_state.patch import PatchOp, apply_patches
from app.api.v1.plan_trip import validation


def _run(coro):
    return asyncio.run(coro)


# ── Itinerary cap is no longer squeezed to 2-3 ───────────────────────────────

def test_full_day_of_five_activities_survives_autofix():
    day = {"day": 1, "activities": [
        {"title": "Umiam Lake", "category": "Viewpoint", "confidence_score": 0.9},
        {"title": "Cafe Shillong", "category": "Cafe", "confidence_score": 0.8},
        {"title": "Don Bosco Museum", "category": "Museum", "confidence_score": 0.8},
        {"title": "Ward's Lake", "category": "Park", "confidence_score": 0.7},
        {"title": "Police Bazar", "category": "Market", "confidence_score": 0.7},
    ]}
    removed = validation.auto_fix_trip([day])
    assert removed == 0
    assert len(day["activities"]) == 5   # old cap would have trimmed to 3


def test_category_cap_scales_with_trip_length():
    assert validation._category_cap([{}]) == 3          # short trip floor
    assert validation._category_cap([{}] * 7) == 7      # week-long trip


# ── Baggage / rating surfaced on option lines ────────────────────────────────

def test_flight_option_lines_expose_baggage():
    state = {"transport": {"going": {"flights": {"items": [
        {"airline": "IndiGo", "price_total": 5000, "baggage_kg": 15, "stops": 0},
        {"airline": "Vistara", "price_total": 6200, "baggage_kg": 25, "stops": 0},
    ]}}}}
    lines = "\n".join(copilot._option_lines(state, "going"))
    assert "baggage 15kg" in lines
    assert "baggage 25kg" in lines
    assert "non-stop" in lines


# ── Cost recompute folds in the waypoint onward segment ──────────────────────

def test_recompute_costs_includes_onward_segment():
    state = {
        "request": {"travelers": 2},
        "costs": {"breakdown": {"stay": 1000, "food": 500, "activities": 300}},
        "transport": {
            "going": {"legs": [{"mode": "bus"}],
                      "buses": {"recommended": {"fare_discounted": 700}}},
            "onward_segments": [
                {"legs": [{"mode": "train"}],
                 "trains": {"recommended": {"cheapest_fare": 400}}},
            ],
        },
    }
    costs = copilot._recompute_costs(state)
    # (700 going + 400 onward) × 2 travelers
    assert costs["breakdown"]["transport"] == 2200
    assert costs["route_breakdown"]["leg1_bus"] == 700
    assert costs["route_breakdown"]["onward1_leg1_train"] == 400


# ── Constrained hotel search also locks in the top match as the stay ─────────

def test_auto_select_top_hotel_updates_stay_and_costs():
    state = {"request": {"travelers": 2, "stay_nights": 3}, "costs": {"breakdown": {}}}
    hotels = [
        {"name": "Budget Inn", "price_per_night": 900},
        {"name": "Grand Hotel", "price_per_night": 4000},
    ]
    patches = [PatchOp(op="set", path="/stay/hotels", value=hotels)]
    out, reply = copilot._auto_select_top_hotel(state, patches, "Found 2 stays.")
    selected = next(p for p in out if p.path == "/stay/selected")
    assert selected.value["name"] == "Budget Inn"
    assert state["stay"]["selected"]["name"] == "Budget Inn"
    assert any(p.path == "/costs" for p in out)
    assert "Budget Inn" in reply


# ── add_activities appends verified spots to the target day ──────────────────

def test_handle_add_activities_appends_places(monkeypatch):
    async def fake_search_places(query, type_filter=None):
        return [
            {"name": "Laitlum Canyons", "place_id": "p1", "types": ["natural_feature"],
             "rating": 4.7, "user_ratings_total": 1200,
             "geometry": {"location": {"lat": 25.4, "lng": 91.9}}},
            {"name": "Mawlynnong", "place_id": "p2", "types": ["tourist_attraction"],
             "rating": 4.6, "user_ratings_total": 900,
             "geometry": {"location": {"lat": 25.2, "lng": 91.9}}},
        ]

    monkeypatch.setattr(structural, "search_places", fake_search_places)
    state = {
        "request": {"destination": "Meghalaya", "budget_tier": "mid"},
        "itinerary": {"days": [
            {"day": 1, "theme": "Shillong", "activities": [
                {"title": "Umiam Lake", "category": "Viewpoint"}]},
        ]},
    }
    patches, reply = _run(structural.handle_add_activities(state, 0, "viewpoints", 2))
    day = next(p.value for p in patches if p.path == "/itinerary/days/0")
    titles = [a["title"] for a in day["activities"]]
    assert "Laitlum Canyons" in titles and "Mawlynnong" in titles
    assert all(a.get("start_time") for a in day["activities"])   # re-timed
    added = [a for a in day["activities"] if a.get("added_by") == "copilot"]
    assert len(added) == 2 and all(a["place_id"] for a in added)


# ── add_waypoint re-routes + splices in a stopover city ──────────────────────

def test_handle_add_waypoint_splices_city(monkeypatch):
    import app.api.v1.plan_trip.journey as journey_mod
    import app.api.v1.plan_trip.orchestrator as orch_mod
    import app.api.v1.plan_trip.plan as plan_mod

    async def fake_journey(body, user_id, profile=None):
        return {"legs": [{"mode": "train", "from": body.origin, "to": body.destination}],
                "trains": {"recommended": {"cheapest_fare": 500}}}

    async def fake_trip(body, user_id, trace=None, profile=None):
        return {"days": [{"day": 1, "theme": f"{body.destination} day",
                          "activities": [{"title": "Naini Lake", "category": "Viewpoint"}]}]}

    def fake_shape(j, include_hotels=False):
        return {"legs": j.get("legs") or [], "trains": j.get("trains") or {}}

    monkeypatch.setattr(journey_mod, "run_plan_journey", fake_journey)
    monkeypatch.setattr(orch_mod, "run_plan_trip", fake_trip)
    monkeypatch.setattr(plan_mod, "_shape_journey", fake_shape)

    state = {
        "request": {"origin": "Bhiwani", "destination": "Meghalaya",
                    "days": 4, "stay_nights": 3, "travelers": 2, "budget_tier": "mid"},
        "itinerary": {"days": [
            {"day": 1, "activities": [{"title": "Umiam Lake", "category": "Viewpoint"}]},
        ]},
        "costs": {"breakdown": {}},
        "transport": {"going": {"legs": []}},
    }
    patches, reply = _run(structural.handle_add_waypoint(state, "user-1", "Nainital", 1))

    days = next(p.value for p in patches if p.path == "/itinerary/days")
    assert days[0]["waypoint"] == "Nainital"          # stopover comes first (en route)
    assert [d["day"] for d in days] == [1, 2]          # renumbered
    new_req = next(p.value for p in patches if p.path == "/request")
    assert new_req["waypoints"] == ["Nainital"]
    assert new_req["days"] == 5                         # 4 + 1 waypoint day
    assert any(p.path == "/transport/onward_segments" for p in patches)
    assert "Nainital" in reply


# ── Chat history is compact and only surfaces copilot turns ──────────────────

def test_recent_history_formats_turns(monkeypatch):
    events = [
        {"phase": "itinerary_ready", "label": "built", "reasoning": None},
        {"phase": "copilot_edit", "label": "Found 3 stays.", "reasoning": "cheapest hotel"},
        {"phase": "copilot_edit", "label": "Locked in the 8pm Volvo.", "reasoning": "cheaper bus"},
    ]
    monkeypatch.setattr(copilot.store, "get_events", lambda trip_id: events)
    hist = copilot._recent_history("t1")
    assert "User: cheapest hotel" in hist
    assert "Copilot: Locked in the 8pm Volvo." in hist
    assert "built" not in hist   # non-copilot phases excluded
