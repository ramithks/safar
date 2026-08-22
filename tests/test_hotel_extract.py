"""
Reading a hotel's brochure into a draft, and refusing to read too much into it.

These cover the deterministic half of the importer — everything between the
model's raw JSON and the draft an owner is shown. The model call itself is not
exercised here; what matters is that a hallucination-shaped, messy or hostile
answer cannot produce a hotel with 2,014 deluxe doubles at ₹4 a night.

The rule the whole module is built on: **a missing field is a correct answer.**
An extractor that fills gaps is an extractor whose mistakes reach a guest as
confident sentences, and the fields it would be filling — the price, the
occupancy, the cancellation policy — are exactly the ones a hotel is then held
to.
"""
import pytest

from app.services.pms import extract


# ── numbers a person typed ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Rs. 4,200/-", 4200),
    ("₹8999 onwards", 8999),
    ("INR 12,500 per night", 12500),
    ("4200", 4200),
    (4200, 4200),
    (4200.0, 4200),
    ("two thousand", None),   # prose is not a number
    ("", None),
    (None, None),
    (True, None),             # a bool is not a price
])
def test_to_int(raw, expected):
    assert extract.to_int(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2 PM", "14:00"),
    ("14:00", "14:00"),
    ("11 am", "11:00"),
    ("12 am", "00:00"),
    ("12 pm", "12:00"),
    ("noon", None),
    ("", None),
    ("25:00", None),
])
def test_times(raw, expected):
    assert extract._time(raw) == expected


def test_lists_are_trimmed_deduplicated_and_bounded():
    out = extract.str_list(["AC", " ac ", "WiFi", "", None, *[f"x{i}" for i in range(40)]])
    assert out[:2] == ["AC", "WiFi"]
    assert len(out) == len(set(o.lower() for o in out))
    assert len(out) <= extract.MAX_LIST


def test_a_meal_plan_must_be_one_of_the_four():
    assert extract._meal_plan("cp") == "CP"
    assert extract._meal_plan(" map ") == "MAP"
    assert extract._meal_plan("breakfast included") is None
    assert extract._meal_plan(None) is None


# ── what a bad answer cannot become ──────────────────────────────────────────

def test_an_implausible_room_count_is_dropped_not_stored():
    """A page number read as a room count would give a hotel 2,014 rooms."""
    draft = extract.normalise({
        "confidence": 0.8,
        "room_types": [{"name": "Deluxe Double", "total_units": 2014}],
    })
    assert draft["roomTypes"][0]["totalUnits"] is None
    # The room itself survives — it is real, and the owner types the count.
    assert draft["roomTypes"][0]["name"] == "Deluxe Double"


def test_an_implausible_price_is_dropped():
    draft = extract.normalise({
        "confidence": 0.8,
        "room_types": [
            {"name": "Standard", "price_rupees": 4},           # a bullet number
            {"name": "Deluxe", "price_rupees": 99_999_999},    # a phone number
            {"name": "Suite", "price_rupees": 4200},
        ],
    })
    prices = [r["priceRupees"] for r in draft["roomTypes"]]
    assert prices == [None, None, 4200]


def test_an_implausible_occupancy_is_dropped():
    draft = extract.normalise({
        "confidence": 0.8,
        "room_types": [{"name": "Dorm", "max_occupancy": 400}],
    })
    assert draft["roomTypes"][0]["maxOccupancy"] is None


def test_a_room_with_no_usable_name_is_dropped_and_counted():
    """
    Silent for a field, loud for a room. A draft with three rooms when the
    brochure had four is something somebody should notice.
    """
    draft = extract.normalise({
        "confidence": 0.9,
        "room_types": [
            {"name": "Deluxe"},
            {"name": ""},
            {"name": "-"},
            "not even an object",
        ],
    })
    assert [r["name"] for r in draft["roomTypes"]] == ["Deluxe"]
    assert draft["droppedRooms"] == 3
    assert any("could not be read as a room" in line
               for line in extract.found_lines(draft))


def test_a_name_too_short_to_be_a_room_is_dropped():
    """
    The same two-character minimum `onboarding.add_room_type` enforces. A draft
    that offered a room the creation route would refuse is a draft that fails
    on approval, which is the worst moment to find out.
    """
    draft = extract.normalise({
        "confidence": 0.9, "room_types": [{"name": "A"}, {"name": "Deluxe"}],
    })
    assert [r["name"] for r in draft["roomTypes"]] == ["Deluxe"]
    assert draft["droppedRooms"] == 1


def test_a_forty_page_brochure_cannot_become_two_hundred_room_types():
    draft = extract.normalise({
        "confidence": 0.5,
        "room_types": [{"name": f"Room type {i}"} for i in range(200)],
    })
    assert len(draft["roomTypes"]) == extract.MAX_ROOMS


def test_confidence_is_clamped_and_never_missing():
    assert extract.normalise({"room_types": []})["confidence"] == 0.0
    assert extract.normalise({"confidence": "high", "room_types": []})["confidence"] == 0.0
    assert extract.normalise({"confidence": 7, "room_types": []})["confidence"] == 1.0
    assert extract.normalise({"confidence": -1, "room_types": []})["confidence"] == 0.0


def test_an_empty_answer_is_a_valid_draft():
    """A menu, an invoice, a blank scan. Not an error — a draft with nothing in
    it and a confidence the caller can show."""
    draft = extract.normalise({})
    assert draft["roomTypes"] == []
    assert draft["confidence"] == 0.0
    assert draft["hotel"]["name"] is None


@pytest.mark.parametrize("placeholder", ["unknown", "N/A", "none", "-", "TBD", ""])
def test_placeholders_a_model_reaches_for_become_nothing(placeholder):
    draft = extract.normalise({
        "confidence": 0.7,
        "hotel_name": placeholder,
        "room_types": [{"name": "Deluxe", "bed_type": placeholder,
                        "description": placeholder}],
    })
    assert draft["hotel"]["name"] is None
    assert draft["roomTypes"][0]["bedType"] is None
    assert draft["roomTypes"][0]["description"] is None


# ── the whole shape ──────────────────────────────────────────────────────────

def test_a_realistic_brochure():
    draft = extract.normalise({
        "confidence": 0.86,
        "hotel_name": "Anandi House",
        "city": "Udaipur",
        "description": "A haveli on the lake.",
        "property_amenities": ["Rooftop restaurant", "Free parking", "free parking"],
        "check_in_time": "2 PM",
        "check_out_time": "11:00 am",
        "policies": {"cancellation": "Free until 48 hours before arrival.",
                     "pets": None, "children": "Under 6 stay free."},
        "meals": {"breakfast": "07:30–10:30, buffet", "lunch": None},
        "room_types": [
            {"name": "Deluxe Lake View", "total_units": 6, "max_occupancy": 2,
             "bed_type": "1 king", "size_sqft": 320, "price_rupees": 4200,
             "meal_plan": "cp", "amenities": ["AC", "Balcony"]},
            {"name": "Family Suite", "total_units": 2, "max_occupancy": 4,
             "price_rupees": "Rs. 7,500/-"},
        ],
    })

    assert draft["hotel"]["checkInTime"] == "14:00"
    assert draft["hotel"]["checkOutTime"] == "11:00"
    # De-duplicated even across capitalisation, which is what a brochure does.
    assert draft["hotel"]["amenities"] == ["Rooftop restaurant", "Free parking"]
    # A null policy is absent rather than present-and-empty: an empty
    # cancellation policy would read to the agent as a policy that says nothing.
    assert set(draft["hotel"]["policies"]) == {"cancellation", "children"}
    assert set(draft["hotel"]["meals"]) == {"breakfast"}

    deluxe, suite = draft["roomTypes"]
    assert deluxe["mealPlan"] == "CP"
    assert deluxe["priceRupees"] == 4200
    assert suite["priceRupees"] == 7500
    assert suite["mealPlan"] is None

    found = extract.found_lines(draft)
    assert any("2 room types" in line for line in found)
    assert any("₹4,200" in line for line in found)
    assert any("Policies" in line for line in found)


def test_found_lines_say_how_many_rooms_have_no_price():
    """The gap an owner most needs to see before approving: a room with no rate
    is a room the booking engine cannot sell."""
    draft = extract.normalise({
        "confidence": 0.7,
        "room_types": [{"name": "Standard", "price_rupees": 3000},
                       {"name": "Deluxe"}, {"name": "Suite"}],
    })
    assert any("1 of 3" in line for line in extract.found_lines(draft))


def test_nothing_to_read_is_refused_before_a_model_is_called():
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(extract.extract_hotel())
