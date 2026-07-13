"""
Offline unit tests for the multi-city trip builder (graph model + builder
handlers). Every live pipeline (journey routing, gateway search, itinerary
generation) is stubbed so the builder's LOGIC — graph mutation, projection,
derived recommendation/pricing, and the gateway-fallback flow — is exercised
deterministically. The live end-to-end run lives in
scripts/builder_e2e_multicity.py.
"""
import asyncio
import json

import pytest

from app.trip_state import builder, graph as G


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _offline_geocode(monkeypatch):
    """Stop map geocoding hitting Places for made-up test cities."""
    async def _none(city):
        return None

    monkeypatch.setattr("app.services.geo_nearby.geocode_city", _none)


def _train_block(fare=800, dur=180, name="Shatabdi"):
    return {"trains": {"items": [
        {"train_name": name, "cheapest_fare": fare, "duration_min": dur,
         "departure_time": "06:00", "arrival_time": "09:00"},
    ]}}


def _flight_block(fare=3500, dur=90, airline="IndiGo"):
    return {"flights": {"items": [
        {"airline": airline, "price_total": fare, "duration_min": dur,
         "departure_time": "10:00", "arrival_time": "11:30"},
    ]}}


def _base_state():
    return {
        "request": {
            "origin": "Delhi", "destination": "Manali",
            "departure_date": "2026-08-01", "travelers": 2,
            "budget_tier": "mid", "stay_nights": 0,
        },
        "transport": {}, "itinerary": {}, "stay": {}, "costs": {},
    }


# ── Ranking engine: recommendation is derived, cheapest wins on a low budget ──

def test_rank_options_prefers_cheapest_on_low_budget():
    hop = G.RouteHop(from_city="A", to_city="B", options=[
        G.TransportOption(id="flight-0", mode="flight", fare_per_person=3500, duration_min=90),
        G.TransportOption(id="train-0", mode="train", fare_per_person=800, duration_min=300),
    ])
    rec = G.recommended_option(hop, budget="low")
    assert rec.id == "train-0"           # cheapest
    rec_fast = G.recommended_option(hop, prefer="fastest")
    assert rec_fast.id == "flight-0"     # fastest


# ── add_leg (direct): builds the graph, prices it, persists NO recommendation ──

def test_add_leg_direct_builds_and_prices(monkeypatch):
    state = _base_state()

    async def fake_journey(f, t, *a, **k):
        return _train_block()

    async def fake_gateways(*a, **k):
        return []

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", fake_gateways)

    patches, reply, chips = _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))

    paths = {p.path for p in patches}
    assert "/graph" in paths and "/transport" in paths and "/costs" in paths
    assert not chips
    g = G.TripGraph.model_validate(state["graph"])
    assert [n.city for n in g.nodes] == ["Delhi", "Manali"]
    assert len(g.edges) == 1 and g.edges[0].routes[0].kind == "direct"
    # transport = 800/pp x 2 travelers
    assert state["costs"]["breakdown"]["transport"] == 1600
    assert "Delhi → Manali" in reply
    # The canonical graph must never carry a materialized recommendation.
    assert "is_recommended" not in json.dumps(state["graph"])


# ── add_leg (no direct route): offers nearby gateways as tap chips ──

def test_add_leg_no_direct_offers_gateways(monkeypatch):
    state = _base_state()

    async def no_journey(f, t, *a, **k):
        return None

    async def fake_gateways(f, t, *a, **k):
        return [
            {"city": "Chandigarh", "distance_km": 90, "mode": "flight",
             "fare_per_person": 3000, "search": {}},
            {"city": "Jaipur", "distance_km": 240, "mode": "train",
             "fare_per_person": 1200, "search": {}},
        ]

    monkeypatch.setattr(builder, "_shaped_journey", no_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", fake_gateways)

    patches, reply, chips = _run(builder.handle_add_leg(state, "u", "Kaza", "Manali"))

    assert patches == []
    assert chips == ["Via Chandigarh", "Via Jaipur"]
    assert "no direct" in reply.lower()
    assert "Chandigarh" in reply and "Jaipur" in reply


# ── add_leg (explicit via): builds a two-hop gateway route ──

def test_add_leg_via_gateway_builds_two_hops(monkeypatch):
    state = _base_state()

    async def fake_journey(f, t, *a, **k):
        # Kaza→Chandigarh by bus, Chandigarh→Manali by train — both non-empty.
        return _train_block() if t == "Manali" else _flight_block()

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)

    patches, reply, chips = _run(
        builder.handle_add_leg(state, "u", "Kaza", "Manali", via="Chandigarh")
    )

    assert patches and not chips
    g = G.TripGraph.model_validate(state["graph"])
    leg = g.edges[-1]
    assert leg.routes[0].kind == "via_gateway"
    assert leg.routes[0].gateway_city == "Chandigarh"
    assert len(leg.routes[0].hops) == 2
    assert "via Chandigarh" in reply


# ── add_stop_itinerary: attaches real days to the stop, projects them flat ──

def test_add_stop_itinerary_attaches_days(monkeypatch):
    state = _base_state()

    async def fake_trip(req, *a, **k):
        return {"days": [
            {"day": 1, "theme": "Arrival", "activities": [{"title": "Mall Road"}]},
            {"day": 2, "theme": "Solang", "activities": [{"title": "Solang Valley"}]},
        ], "hotels": [{"name": "Snow Peak", "price_per_night": 2000}]}

    monkeypatch.setattr("app.api.v1.plan_trip.orchestrator.run_plan_trip", fake_trip)

    patches, reply = _run(builder.handle_add_stop_itinerary(state, "u", "Manali", 2))

    g = G.TripGraph.model_validate(state["graph"])
    node = g.node_by_city("Manali")
    assert node and node.nights == 2 and len(node.days) == 2
    assert node.stay.search_results[0]["name"] == "Snow Peak"
    assert len(state["itinerary"]["days"]) == 2
    assert "2-day itinerary for Manali" in reply


# ── Full A→B→C→A multi-city scenario, built turn by turn ──

def test_full_multicity_scenario(monkeypatch):
    state = _base_state()

    async def fake_journey(f, t, *a, **k):
        return _flight_block(fare=4000) if (f, t) in {("Delhi", "Manali")} else _train_block(fare=600)

    async def no_gateways(*a, **k):
        return []

    async def fake_trip(req, *a, **k):
        n = req.days
        return {"days": [{"day": i + 1, "activities": [{"title": f"Spot {i+1}"}]} for i in range(n)]}

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)
    monkeypatch.setattr("app.api.v1.plan_trip.orchestrator.run_plan_trip", fake_trip)

    # A→B (fly), 2 days in B, B→C, 3 days in C, C→A (back to origin)
    _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))
    _run(builder.handle_add_stop_itinerary(state, "u", "Manali", 2))
    _run(builder.handle_add_leg(state, "u", "Manali", "Leh"))
    _run(builder.handle_add_stop_itinerary(state, "u", "Leh", 3))
    _run(builder.handle_add_leg(state, "u", "Leh", "Delhi"))

    g = G.TripGraph.model_validate(state["graph"])
    assert [n.city for n in g.nodes] == ["Delhi", "Manali", "Leh"]
    assert len(g.edges) == 3
    # The return leg targets the origin node.
    assert g.edges[-1].to_node_id == g.nodes[0].id
    assert g.journey_summary() == "Delhi → Manali → Leh"
    # Flat itinerary spans both stops, renumbered end to end.
    assert len(state["itinerary"]["days"]) == 5
    assert [d["day"] for d in state["itinerary"]["days"]] == [1, 2, 3, 4, 5]
    # Costs fold in all three legs: (4000 + 600 + 600)/pp x 2 travelers.
    assert state["costs"]["breakdown"]["transport"] == (4000 + 600 + 600) * 2


# ── Same-mode chain: two train legs (the real Bengaluru→Udaipur shape) ──
#
# The legacy mode-keyed buckets CANNOT express train + train: the planner emits
# trains = {legs: [seg1, seg2], items: []} (empty top-level items) with the real
# options per leg — the old frontend read only items and showed "No rides found"
# despite two successful searches. The graph derives one hop per bookable leg.

def _real_planner_state():
    """Mirrors the stored 6b34ad69 trip: auto → train → train → auto going,
    per-leg live_data, empty top-level trains.items, plus a bus return with a
    user pick (is_recommended) that must survive migration as the selection."""
    def train(name, fare, rec=False):
        it = {"train_name": name, "cheapest_fare": fare, "duration_min": 600,
              "available_classes": [{"code": "SL", "fare": fare}]}
        if rec:
            it["is_recommended"] = True
        return it

    return {
        "request": {"origin": "Bengaluru", "destination": "Udaipur",
                    "departure_date": "2026-07-19", "travelers": 2,
                    "budget_tier": "low", "stay_nights": 2},
        "transport": {
            "going": {
                "legs": [
                    {"leg": 1, "mode": "auto", "from": "Bengaluru (your location)", "to": "Bengaluru City Railway Station"},
                    {"leg": 2, "mode": "train", "from": "Bengaluru City Railway Station", "to": "Ahmedabad Junction",
                     "live_data": {"trains": [train("GANDHIDHAM EXP", 750, rec=True), train("BIKANER EXP", 735)]}},
                    {"leg": 3, "mode": "train", "from": "Ahmedabad Junction", "to": "Udaipur City Railway Station",
                     "live_data": {"trains": [train("ASV UDZ SF EXP", 150, rec=True), train("VIRBHUMI EXP", 215)]}},
                    {"leg": 4, "mode": "auto", "from": "Udaipur City Railway Station", "to": "Udaipur (your accommodation area)"},
                ],
                "trains": {"items": [], "legs": [{"items": []}, {"items": []}]},
                "flight_journey": {"items": [{"airline": "IndiGo", "price_total": 10254}]},
                "summary": {"duration": "38h 0m", "has_real_data": True},
            },
            "return_journey": {
                "legs": [{"leg": 1, "mode": "bus", "from": "Udaipur", "to": "Bengaluru"}],
                "buses": {"items": [
                    {"operator": "VRL", "fare_discounted": 1400, "duration_min": 2000},
                    {"operator": "S.R.Travels", "fare_discounted": 6000, "duration_min": 1800, "is_recommended": True},
                ]},
            },
        },
        "stay": {"hotels": [{"name": "Lake View", "price_per_night": 1200}]},
        "itinerary": {"days": [{"day": 1, "activities": []}]},
        "costs": {"breakdown": {"food": 3000, "activities": 2000}},
    }


def test_same_mode_chain_derives_per_leg_hops_and_projects():
    state = _real_planner_state()
    g = G.derive_graph_from_legacy(state)

    # One hop per bookable leg — the two train legs stay separate.
    going_leg = g.edges[0]
    hops = going_leg.routes[0].hops
    assert [(h.from_city, h.to_city) for h in hops] == [
        ("Bengaluru City Railway Station", "Ahmedabad Junction"),
        ("Ahmedabad Junction", "Udaipur City Railway Station"),
    ]
    assert [len(h.options) for h in hops] == [2, 2]
    # Legacy is_recommended became the selection (user-choice data)…
    assert going_leg.selection.per_hop_option_ids == ["train-0", "train-0"]
    # …and never leaks into the canonical graph as a flag.
    assert "is_recommended" not in json.dumps(g.model_dump(mode="json"))

    proj = G.project(state, g)
    going = proj["/transport"]["going"]
    # The full original chain (autos included) is re-emitted…
    assert [l.get("mode") for l in going["legs"]] == ["auto", "train", "train", "auto"]
    # …with per-hop buckets the frontend can render and select from.
    assert len(going["hops"]) == 2
    assert going["hops"][0]["trains"]["recommended"]["train_name"] == "GANDHIDHAM EXP"
    assert going["hops"][1]["trains"]["recommended"]["train_name"] == "ASV UDZ SF EXP"
    # Non-projected keys (the direct-flight alternative) survive the merge.
    assert "flight_journey" in going
    assert going["summary"]["duration"] == "38h 0m"

    # The return keeps the user's S.R.Travels pick, not the cheaper VRL.
    ret = proj["/transport"]["return_journey"]
    assert ret["buses"]["recommended"]["operator"] == "S.R.Travels"

    # Costs: (750 + 150 going + 6000 return) × 2 travelers; food carried over.
    assert proj["/costs"]["breakdown"]["transport"] == (750 + 150 + 6000) * 2
    assert proj["/costs"]["breakdown"]["food"] == 3000


def test_projected_state_derives_on_read_without_persisting():
    state = _real_planner_state()
    out = G.projected_state(state)
    # Rides render (the old shape showed "No rides found" here).
    assert len(out["transport"]["going"]["hops"]) == 2
    # Read-time derivation must not write the graph back into the state.
    assert not state.get("graph")


def test_select_leg_option_finds_hop_by_mode():
    state = _real_planner_state()
    g = G.derive_graph_from_legacy(state)
    state["graph"] = g.model_dump(mode="json")
    # hop_index 0 + wanting the SECOND hop's train would be ambiguous (both hops
    # are trains) — but targeting hop 1 explicitly works…
    patches, reply = builder.handle_select_leg_option(state, "leg-0", "train", 1, hop_index=1)
    assert patches and "VIRBHUMI" in reply
    g2 = G.TripGraph.model_validate(state["graph"])
    assert g2.edges[0].selection.per_hop_option_ids[1] == "train-1"
    # …and the untouched hop keeps its migrated pick.
    assert g2.edges[0].selection.per_hop_option_ids[0] == "train-0"


# ── Lazy migration: a legacy single-destination trip derives a valid graph ──

def test_derive_graph_from_legacy_round_trips():
    state = {
        "request": {"origin": "Delhi", "destination": "Manali",
                    "departure_date": "2026-08-01", "travelers": 2, "stay_nights": 3},
        "transport": {
            "outbound_chain": ["Delhi", "Manali"],
            "going": _train_block(),
            "return_journey": _flight_block(),
        },
        "stay": {"hotels": [{"name": "Snow Peak", "price_per_night": 2000}]},
        "itinerary": {"days": [{"day": 1, "activities": []}]},
        "costs": {},
    }
    g = G.derive_graph_from_legacy(state)
    assert [n.city for n in g.nodes] == ["Delhi", "Manali"]
    assert len(g.edges) == 2          # going + return
    proj = G.project(state, g)
    assert "trains" in proj["/transport"]["going"]
    assert "flights" in proj["/transport"]["return_journey"]


# ── select_leg_option: pick a ride on ANY graph leg; selection survives reprojection ──

def test_select_leg_option_persists_and_projects(monkeypatch):
    state = _base_state()

    async def fake_journey(f, t, *a, **k):
        return {"trains": {"items": [
            {"train_name": "Shatabdi", "cheapest_fare": 800, "duration_min": 180},
            {"train_name": "Duronto", "cheapest_fare": 1200, "duration_min": 150},
        ]}}

    async def no_gateways(*a, **k):
        return []

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)

    _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))
    # Ranking would pick the cheap Shatabdi; the user picks the Duronto (index 1).
    patches, reply = builder.handle_select_leg_option(state, "leg-0", "train", 1)

    assert patches and "Duronto" in reply
    g = G.TripGraph.model_validate(state["graph"])
    sel = g.edges[0].selection
    assert sel and sel.per_hop_option_ids[0] == "train-1"
    # The projection marks the USER'S pick, not the derived cheapest.
    items = state["transport"]["going"]["trains"]["items"]
    flags = [it["is_recommended"] for it in items]
    assert flags == [False, True]
    # Costs follow the pick: 1200/pp x 2 travelers.
    assert state["costs"]["breakdown"]["transport"] == 2400
    # The projected block carries the leg id so the UI can target this leg.
    assert state["transport"]["going"]["leg_id"] == "leg-0"
    # Selection is user data, recommendation stays derived-only in the graph.
    assert "is_recommended" not in json.dumps(state["graph"])


def test_select_leg_option_reply_quotes_live_prices_not_estimates(monkeypatch):
    # The copilot reply must quote the SAME live total the workspace shows
    # (transport + stay), never costs.total, which folds in food/activities/misc
    # estimates and so contradicts the budget breakdown the user is reading.
    state = _base_state()
    state["costs"] = {"breakdown": {"food": 5000, "activities": 3000}}

    async def two_trains(f, t, *a, **k):
        return {"trains": {"items": [
            {"train_name": "Shatabdi", "cheapest_fare": 800, "duration_min": 180},
            {"train_name": "Duronto", "cheapest_fare": 1200, "duration_min": 150},
        ]}}

    async def no_gateways(*a, **k):
        return []

    monkeypatch.setattr(builder, "_shaped_journey", two_trains)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)

    _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))
    _patches, reply = builder.handle_select_leg_option(state, "leg-0", "train", 1)  # Duronto ₹1200

    breakdown = state["costs"]["breakdown"]
    live = breakdown["transport"] + breakdown["stay"]   # 1200 × 2 travelers + 0 stay
    assert live == 2400
    assert f"₹{live:,}" in reply and "at live prices" in reply
    # costs.total (2400 + food 5000 + activities 3000 + misc 240 = 10,640) must NOT appear.
    assert state["costs"]["total"] == 10640
    assert "10,640" not in reply and "Trip total" not in reply


def test_select_leg_option_by_cities_and_bad_index(monkeypatch):
    state = _base_state()

    async def fake_journey(f, t, *a, **k):
        return _train_block()

    async def no_gateways(*a, **k):
        return []

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)
    _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))

    # City-pair fallback (what the agent has in hand).
    patches, _ = builder.handle_select_leg_option(
        state, None, "train", 0, from_city="delhi", to_city="Manali",
    )
    assert patches
    # Out-of-range index → friendly error, no patches.
    patches, reply = builder.handle_select_leg_option(state, "leg-0", "train", 9)
    assert patches == [] and "pick one of the" in reply


# ── select_stop_hotel: per-stop stay choice drives per-stop costs ──

def test_select_stop_hotel_updates_costs(monkeypatch):
    state = _base_state()

    async def fake_trip(req, *a, **k):
        return {"days": [{"day": 1, "activities": []}, {"day": 2, "activities": []}],
                "hotels": [
                    {"name": "Cheap Inn", "price_per_night": 900},
                    {"name": "Lake Resort", "price_per_night": 2500},
                ]}

    monkeypatch.setattr("app.api.v1.plan_trip.orchestrator.run_plan_trip", fake_trip)
    _run(builder.handle_add_stop_itinerary(state, "u", "Manali", 2))

    # Default (no selection) prices the cheapest: 900 x 2 nights.
    assert state["costs"]["breakdown"]["stay"] == 1800

    patches, reply = builder.handle_select_stop_hotel(state, "Manali", 1)
    assert patches and "Lake Resort" in reply and "Manali" in reply
    g = G.TripGraph.model_validate(state["graph"])
    assert g.node_by_city("Manali").stay.selected_id == "Lake Resort"
    # 2500 x 2 nights now.
    assert state["costs"]["breakdown"]["stay"] == 5000


# ── Inserting a stop into an existing single-destination trip ──────────────────
#
# The exact failing scenario: a generated Bengaluru→Jaipur trip, user says "add
# Goa, 2 days, then go to Jaipur". Before the fix this stacked duplicate legs,
# left the phantom direct Bengaluru→Jaipur leg as the journey, and scrambled the
# stop order — which made the agent re-plan the same actions in a loop.

def _legacy_single_dest_state():
    """A generated single-destination Bengaluru→Jaipur trip in the legacy shape —
    the starting point when a user asks to thread a stop in."""
    return {
        "request": {"origin": "Bengaluru", "destination": "Jaipur",
                    "departure_date": "2026-07-25", "return_date": "2026-07-28",
                    "travelers": 2, "budget_tier": "mid", "stay_nights": 2},
        "transport": {
            "outbound_chain": ["Bengaluru", "Jaipur"],
            "going": _flight_block(fare=8000),
            "return_journey": _flight_block(fare=8500),
        },
        "stay": {"hotels": [{"name": "Jaipur Palace", "price_per_night": 3000}]},
        "itinerary": {"days": [
            {"day": 1, "activities": [{"title": "Amber Fort"}]},
            {"day": 2, "activities": [{"title": "Hawa Mahal"}]},
        ]},
        "costs": {"breakdown": {}},
    }


def test_insert_stop_into_legacy_trip_rewires_cleanly(monkeypatch):
    state = _legacy_single_dest_state()

    async def fake_journey(f, t, *a, **k):
        return _flight_block(fare=4000)

    async def no_gateways(*a, **k):
        return []

    async def fake_trip(req, *a, **k):
        return {"days": [{"day": i + 1, "activities": [{"title": f"Goa spot {i+1}"}]}
                         for i in range(req.days)],
                "hotels": [{"name": "Goa Beach Resort", "price_per_night": 2500}]}

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)
    monkeypatch.setattr("app.api.v1.plan_trip.orchestrator.run_plan_trip", fake_trip)

    # The agent's plan for "add Goa, 2 days, then Jaipur" — note it also re-adds
    # the return leg, which must be recognised as already present, not duplicated.
    _run(builder.handle_add_leg(state, "u", "Bengaluru", "Goa"))
    _run(builder.handle_add_stop_itinerary(state, "u", "Goa", 2))
    _run(builder.handle_add_leg(state, "u", "Goa", "Jaipur"))
    dup_patches, dup_reply, _ = _run(builder.handle_add_leg(state, "u", "Jaipur", "Bengaluru"))

    # The re-added return leg is a no-op, not a second edge.
    assert dup_patches == [] and "already in your trip" in dup_reply

    g = G.TripGraph.model_validate(state["graph"])
    # Travel order (not creation order), phantom direct Bengaluru→Jaipur gone,
    # exactly the three real legs, one return.
    assert [n.city for n in g.nodes] == ["Bengaluru", "Goa", "Jaipur"]
    assert {(e.from_city, e.to_city) for e in g.edges} == {
        ("Bengaluru", "Goa"), ("Goa", "Jaipur"), ("Jaipur", "Bengaluru"),
    }
    assert len(g.edges) == 3

    # The projected journey follows the real route, not the stale direct leg.
    t = state["transport"]
    assert t["outbound_chain"] == ["Bengaluru", "Goa", "Jaipur"]
    assert t["journey_summary"] == "Bengaluru → Goa → Jaipur"
    assert t["going"]["legs"][0]["to"] == "Goa"
    assert t["onward_segments"][0]["to"] == "Jaipur"
    assert "flights" in t["return_journey"]

    # Flat itinerary is in TRAVEL order: Goa's 2 days first, then Jaipur's 2.
    days = state["itinerary"]["days"]
    assert [d["day"] for d in days] == [1, 2, 3, 4]
    assert days[0]["stop_city"] == "Goa" and days[3]["stop_city"] == "Jaipur"


def test_add_leg_duplicate_is_noop(monkeypatch):
    state = _base_state()

    async def fake_journey(f, t, *a, **k):
        return _train_block()

    async def no_gateways(*a, **k):
        return []

    monkeypatch.setattr(builder, "_shaped_journey", fake_journey)
    monkeypatch.setattr("app.services.geo_nearby.find_gateways", no_gateways)

    first_patches, _, _ = _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))
    assert first_patches
    # Re-adding the same leg does nothing — no second edge, no patches.
    dup_patches, dup_reply, _ = _run(builder.handle_add_leg(state, "u", "Delhi", "Manali"))
    assert dup_patches == [] and "already in your trip" in dup_reply
    g = G.TripGraph.model_validate(state["graph"])
    assert len(g.edges) == 1


def test_add_stop_itinerary_duplicate_is_noop(monkeypatch):
    state = _base_state()

    calls = {"n": 0}

    async def fake_trip(req, *a, **k):
        calls["n"] += 1
        return {"days": [{"day": i + 1, "activities": [{"title": f"Spot {i+1}"}]}
                         for i in range(req.days)]}

    monkeypatch.setattr("app.api.v1.plan_trip.orchestrator.run_plan_trip", fake_trip)

    _run(builder.handle_add_stop_itinerary(state, "u", "Manali", 2))
    dup_patches, dup_reply = _run(builder.handle_add_stop_itinerary(state, "u", "Manali", 2))
    # Same city + same day count → no rebuild (the pipeline is not hit again).
    assert dup_patches == [] and "already have a 2-day itinerary" in dup_reply
    assert calls["n"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
