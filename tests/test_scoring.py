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


# ── Absent-field-aware ranking (renormalise over present signals) ───────────────

def test_weighted_present_renormalises_over_present():
    # Only two of the four weights are present → they get reweighted to sum 1,
    # so the score reflects what's KNOWN, not diluted toward 0 by the absentees.
    w = {"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}
    got = scoring._weighted_present({"a": 1.0, "b": 1.0, "c": None, "d": None}, w)
    assert got == 100.0                       # present ones are both 1.0 → full marks
    # A real zero still counts (not treated as absent):
    got2 = scoring._weighted_present({"a": 1.0, "b": 0.0, "c": None, "d": None}, w)
    assert got2 == 50.0
    # Nothing present → neutral, never a crash.
    assert scoring._weighted_present({"a": None}, {"a": 1.0}) == 50.0


def test_rating_normalisation_across_providers():
    # Each provider speaks a different dialect; after score_hotels every hotel must
    # carry a unified /10 rating_score + a label + separated stars.
    hotels = [
        {"name": "agoda", "source": "agoda", "star_rating": 4.0, "review_score": 8.7},
        {"name": "emt_ta", "source": "easemytrip", "star_rating": 3.0, "ta_rating": 4.2},
        {"name": "garbage_label", "source": "booking", "review_score": 9.1, "review_label": "Review score"},
        {"name": "unrated", "source": "agoda", "star_rating": 0.0},
    ]
    scoring.score_hotels(hotels)
    by = {h["name"]: h for h in hotels}
    assert by["agoda"]["rating_score"] == 8.7 and by["agoda"]["rating_label"] == "Very Good"
    # TripAdvisor /5 → /10, honestly attributed.
    assert by["emt_ta"]["rating_score"] == 8.4 and by["emt_ta"]["rating_basis"] == "TripAdvisor"
    assert by["emt_ta"]["review_score"] == 8.4          # legacy field backfilled
    # Garbage placeholder label replaced with a derived bucket.
    assert by["garbage_label"]["rating_label"] == "Exceptional"
    # 0 stars = unrated → None, not a "0-star" hotel.
    assert by["unrated"]["star_rating"] is None
    assert by["unrated"]["rating_score"] is None


def test_thin_provider_not_buried_by_missing_metadata():
    # An EaseMyTrip-style listing carries only price + rating; a Booking listing
    # carries everything. The thin, much-cheaper hotel must be scored on the
    # signals it HAS (and lead on value), not crushed to ~0 for absent metadata.
    thin_cheap = {"name": "Thin Cheap", "price_per_night": 1200,
                  "review_score": 8.0, "source": "easemytrip"}
    rich_pricey = {"name": "Rich Pricey", "price_per_night": 6000, "review_score": 8.2,
                   "review_count": 400, "distance_km": 0.5,
                   "photos": ["a", "b", "c"], "amenities": ["wifi", "pool", "gym", "spa"],
                   "free_cancellation": True, "source": "booking"}
    scoring.score_hotels([thin_cheap, rich_pricey])
    assert thin_cheap["rank_score"] > 60          # renormalised, not diluted to ~48
    assert thin_cheap["rank_score"] >= rich_pricey["rank_score"]
    # quality_score is the opposite signal — richness DOES lower it for the thin one.
    assert thin_cheap["quality_score"] < rich_pricey["quality_score"]
