"""Connection-aware leg selection: a connecting leg must never be recommended
if it departs before the previous leg arrives (+ buffer).

Reproduces the reported Manali→Delhi (overnight bus 21:00→08:00) → Delhi→Bhiwani
(train) case where a 05:00 train was wrongly recommended over a valid later one.
"""
from app.api.v1.plan_trip.journey import _apply_connection_constraints


def _bus_leg(dep, arr):
    return {
        "mode": "bus", "from": "Manali", "to": "Delhi", "has_live_data": True,
        "live_data": {"buses": [
            {"operator": "HRTC Volvo", "departure": dep, "arrival": arr,
             "fare_discounted": 1400, "rating": 4.2, "available_seats": 20,
             "amenities": ["wifi", "charging"]},
        ]},
    }


def _train_leg(trains):
    return {
        "mode": "train", "from": "Delhi", "to": "Bhiwani", "has_live_data": True,
        "live_data": {"trains": trains},
    }


def test_train_before_bus_arrival_is_not_recommended():
    # Overnight bus arrives Delhi 08:00 the next day; the 05:00 train departs
    # BEFORE that and must be rejected in favour of the 10:15 train.
    legs = [
        _bus_leg("2026-07-22 21:00:00", "2026-07-23 08:00:00"),
        _train_leg([
            {"train_number": "12345", "departure_time": "05:00", "arrival_time": "07:30",
             "duration_min": 150, "cheapest_fare": 200},
            {"train_number": "22222", "departure_time": "10:15", "arrival_time": "12:45",
             "duration_min": 150, "cheapest_fare": 220},
        ]),
    ]
    _apply_connection_constraints(legs, "2026-07-22", "low")

    trains = legs[1]["live_data"]["trains"]
    early = next(t for t in trains if t["train_number"] == "12345")
    later = next(t for t in trains if t["train_number"] == "22222")
    assert not early.get("is_recommended"), "05:00 train departs before the bus arrives"
    assert later.get("is_recommended"), "10:15 train is the first valid connection"
    # The connecting leg is stamped with the day the bus actually lands (the 23rd).
    assert legs[1]["date"] == "2026-07-23"
    assert legs[1]["recommended_departure"] == "2026-07-23 10:15"


def test_all_trains_too_early_flags_warning():
    legs = [
        _bus_leg("2026-07-22 21:00:00", "2026-07-23 08:00:00"),
        _train_leg([
            {"train_number": "12345", "departure_time": "05:00", "arrival_time": "07:30",
             "duration_min": 150, "cheapest_fare": 200},
            {"train_number": "33333", "departure_time": "06:30", "arrival_time": "09:00",
             "duration_min": 150, "cheapest_fare": 210},
        ]),
    ]
    _apply_connection_constraints(legs, "2026-07-22", "low")
    assert "connection_warning" in legs[1]
    # Falls back to the earliest available so the trip still books.
    trains = legs[1]["live_data"]["trains"]
    assert any(t.get("is_recommended") for t in trains)


def test_first_leg_date_stamped_from_base():
    legs = [_bus_leg("2026-07-19 22:00:00", "2026-07-20 08:00:00")]
    _apply_connection_constraints(legs, "2026-07-19", "low")
    assert legs[0]["date"] == "2026-07-19"
    assert legs[0]["recommended_arrival"] == "2026-07-20 08:00"
    assert "connection_warning" not in legs[0]
