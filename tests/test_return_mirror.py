"""Return journey mirrors the outbound's hubs in reverse (bhiwani→delhi→
haridwar→rishikesh ⇒ rishikesh→haridwar→delhi→bhiwani)."""
from app.api.v1.plan_trip.journey import reverse_route_plan, _is_multihop_route


def _leg(frm, to, mode, live=True):
    return {
        "from": frm, "to": to, "mode": mode, "needs_live_search": live,
        "duration_approx": "3h", "distance_km": 200, "tips": "carry ID",
        "search_params": {"origin": frm, "destination": to, "mode_type": mode},
    }


def _outbound():
    return {
        "is_international": False,
        "legs": [
            _leg("home", "bhiwani", "auto", live=False),       # first-mile
            _leg("bhiwani", "Delhi", "train"),
            _leg("Delhi", "Haridwar", "train"),
            _leg("Haridwar", "Rishikesh", "bus"),
            _leg("Rishikesh", "hotel", "auto", live=False),    # last-mile
        ],
    }


def test_multihop_route_is_detected():
    assert _is_multihop_route(_outbound()) is True


def test_reverse_mirrors_leg_order_and_endpoints():
    rev = reverse_route_plan(_outbound())
    assert rev is not None
    chain = [(l["from"], l["to"]) for l in rev["legs"]]
    # Exact reverse of the outbound, endpoints swapped per leg.
    assert chain == [
        ("hotel", "Rishikesh"),
        ("Rishikesh", "Haridwar"),
        ("Haridwar", "Delhi"),
        ("Delhi", "bhiwani"),
        ("bhiwani", "home"),
    ]
    assert rev["mirrored_from_outbound"] is True
    assert [l["leg"] for l in rev["legs"]] == [1, 2, 3, 4, 5]


def test_reverse_swaps_search_params():
    rev = reverse_route_plan(_outbound())
    # The Rishikesh→Haridwar bus leg's search_params must point the right way.
    bus_leg = next(l for l in rev["legs"] if l["from"] == "Rishikesh" and l["to"] == "Haridwar")
    assert bus_leg["search_params"]["origin"] == "Rishikesh"
    assert bus_leg["search_params"]["destination"] == "Haridwar"
    assert bus_leg["mode"] == "bus"
    assert bus_leg["needs_live_search"] is True


def test_single_hop_is_not_mirrored():
    direct = {"legs": [
        _leg("home", "bhiwani", "auto", live=False),
        _leg("bhiwani", "Goa", "flight"),
        _leg("Goa", "hotel", "auto", live=False),
    ]}
    assert _is_multihop_route(direct) is False
    assert reverse_route_plan(direct) is None


def test_missing_endpoint_falls_back():
    broken = {"legs": [_leg("bhiwani", "Delhi", "train"),
                       {"from": "Delhi", "mode": "train", "needs_live_search": True},
                       _leg("x", "Rishikesh", "train")]}
    # A leg missing `to` can't be safely mirrored → None (independent planning).
    assert reverse_route_plan(broken) is None
