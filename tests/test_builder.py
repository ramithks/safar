"""
Offline unit tests for the KEPT trip-build machinery (post-TripDoc migration):
graph ranking, the travel-time date cascade, return re-pointing, and the
legacy-state → graph derivation that lazy TripDoc conversion rides on.
Everything is pure — no DB, no network.
"""
from app.trip_state import builder as B, graph as G
from app.trip_state.compose import build_skeleton


# ── Ranking: recommendation is derived at serialization time ────────────────────

def test_rank_options_prefers_cheapest_on_low_budget():
    hop = G.RouteHop(from_city="A", to_city="B", options=[
        G.TransportOption(id="flight-0", mode="flight", fare_per_person=3500, duration_min=90),
        G.TransportOption(id="train-0", mode="train", fare_per_person=800, duration_min=300),
    ])
    assert G.recommended_option(hop, budget="low").id == "train-0"      # cheapest
    assert G.recommended_option(hop, prefer="fastest").id == "flight-0"  # fastest


def test_sold_out_options_never_recommended():
    hop = G.RouteHop(from_city="A", to_city="B", options=[
        G.TransportOption(id="cheap-full", mode="train", fare_per_person=500,
                          duration_min=300, available=False),
        G.TransportOption(id="pricier-open", mode="train", fare_per_person=900,
                          duration_min=300, available=True),
    ])
    assert G.recommended_option(hop, budget="low").id == "pricier-open"


# ── Date cascade: multi-day journeys shift every later date ─────────────────────

def _overnight_option() -> G.TransportOption:
    # Departs Aug 1 evening, arrives Aug 3 morning — a 2-day journey.
    return G.TransportOption(
        id="t0", mode="train", fare_per_person=900, duration_min=2340,
        departure="2026-08-01 20:00", arrival="2026-08-03 11:00",
    )


def test_resequence_dates_uses_travel_days():
    g = build_skeleton({"origin": "Pune", "destination": "Manali", "days": 4, "stops": []})
    leg = g.edges[0]
    leg.routes = [G.RouteCandidate(id="r", hops=[
        G.RouteHop(from_city="Pune", to_city="Manali", options=[_overnight_option()])])]

    B._resequence_dates(g, "2026-08-01")

    terminal = next(n for n in g.nodes if n.role == "terminal")
    assert terminal.arrival_date == "2026-08-03"          # not same-day
    # 3 nights at the terminal (days-1), so departure + return shift with arrival
    assert terminal.departure_date == "2026-08-06"
    ret = g.edges[-1]
    assert ret.date == "2026-08-06"


# ── Return re-point: the return always leaves from the real terminal ────────────

def test_repoint_return_after_chain_grows():
    g = build_skeleton({"origin": "Delhi", "destination": "Manali", "days": 4, "stops": []})
    manali = next(n for n in g.nodes if n.role == "terminal")
    chd = G.TripStop(id="stop-9", city="Chandigarh", role="stop", nights=2,
                     departure_date="2026-08-06")
    g.nodes.append(chd)
    g.edges.insert(len(g.edges) - 1, G.TripLeg(
        id="leg-9", from_node_id=manali.id, to_node_id=chd.id,
        from_city=manali.city, to_city=chd.city, routes=[], status="routed"))

    note = B._repoint_return_leg(g)

    ret = next(e for e in g.edges if e.to_node_id == g.nodes[0].id)
    assert ret.from_city == "Chandigarh"
    assert ret.routes == [] and ret.selection is None      # options were for the old pair
    assert note and "Chandigarh" in note


# ── Legacy derivation: what lazy TripDoc conversion rides on ────────────────────

def _legacy_state():
    return {
        "request": {"origin": "Delhi", "destination": "Manali",
                    "departure_date": "2026-08-01", "days": 3, "stay_nights": 2,
                    "travelers": 2, "budget_tier": "mid"},
        "transport": {
            "going": {"trains": {"items": [
                {"train_name": "Shatabdi", "train_number": "12005",
                 "cheapest_fare": 800, "duration_min": 300,
                 "departure_time": "06:00", "arrival_time": "11:00"}]}},
            "return_journey": {"buses": {"items": [
                {"bus_operator": "HRTC", "price": 900, "duration_min": 700,
                 "departure_time": "20:00", "arrival_time": "07:40"}]}},
        },
        "stay": {"hotels": []},
        "itinerary": {"days": []},
    }


def test_derive_graph_from_legacy_builds_round_trip():
    g = G.derive_graph_from_legacy(_legacy_state())
    ordered, outbound, inbound = G.ordered_path(g)
    assert [n.city for n in ordered] == ["Delhi", "Manali"]
    assert len(outbound) == 1 and inbound is not None
    going_opts = outbound[0].routes[0].hops[0].options
    assert going_opts and going_opts[0].mode == "train"
    assert going_opts[0].fare_per_person == 800
    ret_opts = inbound.routes[0].hops[0].options
    assert ret_opts and ret_opts[0].mode == "bus"


def test_ensure_graph_prefers_stored_graph():
    stored = build_skeleton({"origin": "A B", "destination": "C D", "days": 3,
                             "stops": []}).model_dump(mode="json")
    g = G.ensure_graph({"graph": stored, "request": {}})
    assert [n.city for n in g.nodes] == ["A B", "C D"]
