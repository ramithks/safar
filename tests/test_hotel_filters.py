"""Unit tests for the canonical hotel filter layer (app/services/hotels_filters)."""
import pytest

from app.services.hotels_filters import (
    apply_hotel_filters,
    classify_property_type,
    hotel_filters_catalog,
    sanitize_hotel_filters,
    sort_hotels,
)


# ── Property-type classification ─────────────────────────────────────────────

@pytest.mark.parametrize("hotel,expected", [
    ({"name": "Zostel Goa"}, "hostel"),
    ({"name": "Sunset Villa Anjuna"}, "villa"),
    ({"name": "Taj Resort & Spa"}, "resort"),
    ({"name": "Casa Maria Guest House"}, "guesthouse"),
    ({"name": "Marbella Bed and Breakfast"}, "bnb"),
    ({"name": "Palm Vacation Home"}, "vacation_home"),
    ({"name": "Riverview Homestay"}, "homestay"),
    ({"name": "Ocean Studio Apartment"}, "apartment"),
    ({"name": "The Grand Plaza"}, "hotel"),
    ({"name": "Random Stay", "category": "Villa"}, "villa"),
    ({"name": "Random Stay", "category": "Bed and Breakfast"}, "bnb"),
])
def test_classify_property_type(hotel, expected):
    assert classify_property_type(hotel) == expected


# ── Sanitizer ────────────────────────────────────────────────────────────────

def test_sanitize_drops_unknown_and_normalizes():
    out = sanitize_hotel_filters({
        "property_types": ["Hostel", "castle", "hostel"],
        "facilities": ["Sea View", "swimming-lessons", "pool"],
        "meals": ["breakfast_included"],
        "star_ratings": [4, 5, 9, "junk"],
        "price_min": "500", "price_max": 200,      # swapped on purpose
        "min_review_score": 22,                     # clamped to 10
        "free_cancellation": True,
        "pet_friendly": False,                      # False = not a filter
        "sort": "Price_Asc",
        "made_up_key": 1,
    })
    assert out["property_types"] == ["hostel"]
    assert out["facilities"] == ["sea_view", "pool"]
    assert out["star_ratings"] == [4, 5]
    assert (out["price_min"], out["price_max"]) == (200.0, 500.0)
    assert out["min_review_score"] == 10
    assert out["free_cancellation"] is True
    assert "pet_friendly" not in out
    assert out["sort"] == "price_asc"
    assert "made_up_key" not in out


def test_sanitize_empty_inputs():
    assert sanitize_hotel_filters(None) == {}
    assert sanitize_hotel_filters({}) == {}
    assert sanitize_hotel_filters({"property_types": ["castle"]}) == {}


# ── apply_hotel_filters ──────────────────────────────────────────────────────

HOTELS = [
    {   # 0 — budget hostel, wifi+pool, breakfast, free cancel, 1.2 km
        "name": "Zostel", "property_type": "hostel", "price_per_night": 800,
        "review_score": 9.1, "star_rating": 2,
        "amenities": ["Free WiFi", "Swimming pool"], "is_breakfast_included": True,
        "free_cancellation": True, "distance_km": 1.2,
    },
    {   # 1 — mid hotel, EMT-style TA rating (no review_score), couple friendly
        "name": "The Grand", "property_type": "hotel", "price_per_night": 4000,
        "ta_rating": 4.5, "star_rating": 4, "couple_friendly": True,
        "amenities": ["Parking", "Restaurant"], "meal_plan": "Breakfast & dinner included",
        "no_prepayment": True, "distance_km": 3.0,
    },
    {   # 2 — luxury resort, sea view + spa, pets, no distance data
        "name": "Taj Resort", "property_type": "resort", "price_per_night": 15000,
        "review_score": 8.4, "star_rating": 5,
        "amenities": ["Spa and wellness centre", "Sea view", "Pets allowed"],
        "highlights": "All-inclusive available",
    },
    {   # 3 — unpriced, unrated homestay
        "name": "Hilltop Homestay", "property_type": "homestay",
        "amenities": [],
    },
]


def test_apply_property_types_and_price():
    out = apply_hotel_filters(HOTELS, {"property_types": ["hostel", "resort"]})
    assert [h["name"] for h in out] == ["Zostel", "Taj Resort"]
    out = apply_hotel_filters(HOTELS, {"price_max": 5000.0})
    assert [h["name"] for h in out] == ["Zostel", "The Grand"]  # unpriced excluded


def test_apply_review_score_uses_ta_rating_fallback():
    out = apply_hotel_filters(HOTELS, {"min_review_score": 9.0})
    assert [h["name"] for h in out] == ["Zostel", "The Grand"]  # 4.5 TA → 9.0


def test_apply_stars_facilities_meals():
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"star_ratings": [4, 5]})] \
        == ["The Grand", "Taj Resort"]
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"facilities": ["sea_view", "spa"]})] \
        == ["Taj Resort"]
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"meals": ["half_board"]})] \
        == ["The Grand"]
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"meals": ["breakfast_included"]})] \
        == ["Zostel", "The Grand"]


def test_apply_booleans_and_distance():
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"free_cancellation": True})] \
        == ["Zostel"]
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"no_prepayment": True})] \
        == ["The Grand"]
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"couple_friendly": True})] \
        == ["The Grand"]
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"pet_friendly": True})] \
        == ["Taj Resort"]
    # unknown distance excluded under an explicit radius
    assert [h["name"] for h in apply_hotel_filters(HOTELS, {"max_distance_km": 2.0})] \
        == ["Zostel"]


def test_facility_matching_avoids_substring_false_positives():
    hotels = [
        {"name": "A", "amenities": ["ATV rides"], "description": "easily accessible location"},
        {"name": "B", "amenities": ["Smart TV", "Wheelchair accessible"]},
        {"name": "C", "amenities": ["TV"]},   # boundary match at haystack edge
    ]
    assert [h["name"] for h in apply_hotel_filters(hotels, {"facilities": ["tv"]})] \
        == ["B", "C"]
    assert [h["name"] for h in apply_hotel_filters(hotels, {"facilities": ["wheelchair_accessible"]})] \
        == ["B"]


def test_apply_prefers_landmark_distance():
    hotels = [{"name": "A", "distance_km": 9.0, "distance_from_search_km": 0.5}]
    assert apply_hotel_filters(hotels, {"max_distance_km": 1.0}) == hotels


def test_apply_no_filters_passthrough_and_sort_ignored():
    assert apply_hotel_filters(HOTELS, {}) == HOTELS
    assert apply_hotel_filters(HOTELS, None) == HOTELS
    assert apply_hotel_filters(HOTELS, {"sort": "price_asc"}) == HOTELS


# ── sort_hotels ──────────────────────────────────────────────────────────────

def test_sort_hotels():
    by_price = sort_hotels(HOTELS, "price_asc")
    assert [h["name"] for h in by_price][:3] == ["Zostel", "The Grand", "Taj Resort"]
    assert by_price[-1]["name"] == "Hilltop Homestay"      # unpriced sinks
    by_rating = sort_hotels(HOTELS, "rating")
    assert by_rating[0]["name"] == "Zostel"
    assert sort_hotels(HOTELS, "recommended") == HOTELS


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_catalog_covers_vocabulary():
    cat = hotel_filters_catalog()
    assert "hostel" in cat["property_types"] and "villa" in cat["property_types"]
    assert "pool" in cat["facilities"] and "pet_friendly" in cat["facilities"]
    assert "breakfast_included" in cat["meals"]
    assert "price_asc" in cat["sorts"]
