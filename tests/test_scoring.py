"""
Guards for the ranking scorer: weight families must stay normalised (sum to 1.0
so rank_score stays comparable across result sets), and the human-readable
`why_tags` must reflect the underlying signals.
"""
from app.services import scoring


def test_rank_weight_families_sum_to_one():
    for weights in (
        scoring.HOTEL_RANK_WEIGHTS,
        scoring.HOTEL_QUALITY_WEIGHTS,
        scoring.FLIGHT_RANK_WEIGHTS,
        scoring.FLIGHT_QUALITY_WEIGHTS,
    ):
        assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_hotel_why_tags_and_flexibility():
    hotels = [
        {"name": "A", "price_per_night": 1000, "review_score": 9.3,
         "review_count": 300, "distance_km": 0.4, "free_cancellation": True,
         "source": "booking"},
        {"name": "B", "price_per_night": 1600, "review_score": 8.0,
         "review_count": 30, "distance_km": 4.0,
         "is_breakfast_included": True, "source": "agoda"},
    ]
    scoring.score_hotels(hotels)
    a = next(h for h in hotels if h["name"] == "A")
    assert "Lowest price" in a["why_tags"]
    assert "Free cancellation" in a["why_tags"]
    assert a["rank_score"] > 0


def test_flight_why_tags():
    flights = [
        {"airline": "X", "price_total": 4000, "duration_min": 120, "stops": 0,
         "baggage_kg": 20, "seats_available": 3, "source": "booking"},
        {"airline": "Y", "price_total": 6000, "duration_min": 110, "stops": 1,
         "source": "easemytrip"},
    ]
    scoring.score_flights(flights)
    x = next(f for f in flights if f["airline"] == "X")
    assert "Lowest fare" in x["why_tags"]
    assert "Non-stop" in x["why_tags"]
    assert any("baggage" in t for t in x["why_tags"])
