"""
"isme breakfast included nahi hai" — and the draft changes.

A hotelier reading an extracted draft knows what is wrong with it in one
sentence. Making them find the field, click it, clear it and retype it is how a
review screen becomes the thing nobody finishes — and an unfinished review is a
hotel with two room descriptions and no amenities, which is what production
actually looks like.

**The model proposes edits; Python applies them.** Everything below tests the
second half, because the second half is where the safety lives: a closed field
list, rooms matched only to rooms already in the draft, and every value through
the same bounds `normalise` enforces. A model that misreads the correction
proposes a wrong edit — visible in `changes` — rather than making one.
"""
import copy

import pytest

from app.services.pms import extract

DRAFT = {
    "confidence": 0.8,
    "hotel": {
        "name": "Casa Mandala", "address": None, "city": "Anjuna",
        "state": "Goa", "pincode": None, "description": None,
        "amenities": ["Pool"], "checkInTime": "14:00", "checkOutTime": "11:00",
        "policies": {}, "meals": {},
    },
    "roomTypes": [
        {"name": "Deluxe Balcony", "totalUnits": 4, "maxOccupancy": 2,
         "bedType": "King", "sizeSqft": 320, "description": None,
         "amenities": ["AC", "Wi-Fi"], "priceRupees": 4500, "mealPlan": "CP"},
        {"name": "Garden Standard", "totalUnits": 6, "maxOccupancy": 3,
         "bedType": "Twin", "sizeSqft": None, "description": None,
         "amenities": [], "priceRupees": 3200, "mealPlan": "EP"},
    ],
    "droppedRooms": 0,
}


def apply(*edits) -> tuple[dict, list[str]]:
    return extract.apply_edits(copy.deepcopy(DRAFT), list(edits))


def rooms(draft: dict) -> dict:
    return {r["name"]: r for r in draft["roomTypes"]}


# ── the edits that were the point ────────────────────────────────────────────

def test_breakfast_is_not_included_after_all():
    draft, changes = apply({"room": "Deluxe Balcony", "field": "mealPlan", "value": "EP"})
    assert rooms(draft)["Deluxe Balcony"]["mealPlan"] == "EP"
    assert changes == ["Deluxe Balcony: meal plan CP → EP"]


@pytest.mark.parametrize("said,code", [
    ("no breakfast", "EP"), ("room only", "EP"), ("with breakfast", "CP"),
    ("half board", "MAP"), ("all meals", "AP"), ("EP", "EP"), ("map", "MAP"),
])
def test_a_meal_plan_said_in_words_is_understood(said, code):
    """
    The model is asked for a code, and a hotelier says "no breakfast". Accepting
    both refuses less and costs nothing; the extractor stays strict, because a
    brochure it half-read should produce nothing rather than a guess.
    """
    draft, _ = apply({"room": "Deluxe Balcony", "field": "mealPlan", "value": said})
    assert rooms(draft)["Deluxe Balcony"]["mealPlan"] == code


def test_a_price_correction_lands_on_one_room_only():
    draft, _ = apply(
        {"room": "Deluxe Balcony", "field": "priceRupees", "value": "4200"})
    assert rooms(draft)["Deluxe Balcony"]["priceRupees"] == 4200
    assert rooms(draft)["Garden Standard"]["priceRupees"] == 3200


def test_a_count_correction():
    draft, changes = apply(
        {"room": "Garden Standard", "field": "totalUnits", "value": "6 rooms"})
    assert rooms(draft)["Garden Standard"]["totalUnits"] == 6
    # Unchanged, so nothing is claimed to have moved.
    assert changes == []


def test_amenities_replace_rather_than_append():
    """"The garden rooms have AC and a kettle" is a statement of what they have,
    not an addition to a list the hotelier cannot see."""
    draft, _ = apply({"room": "Garden Standard", "field": "amenities",
                      "value": "AC, Kettle, Balcony"})
    assert rooms(draft)["Garden Standard"]["amenities"] == ["AC", "Kettle", "Balcony"]


def test_a_hotel_level_fact_has_no_room():
    draft, changes = apply({"room": "", "field": "checkInTime", "value": "1 pm"})
    assert draft["hotel"]["checkInTime"] == "13:00"
    assert changes == ["The hotel: check in time 14:00 → 13:00"]


def test_clearing_a_field():
    draft, changes = apply({"room": "Deluxe Balcony", "field": "bedType", "value": ""})
    assert rooms(draft)["Deluxe Balcony"]["bedType"] is None
    assert "→ nothing" in changes[0]


def test_several_corrections_in_one_sentence():
    draft, changes = apply(
        {"room": "Deluxe Balcony", "field": "priceRupees", "value": "4200"},
        {"room": "Garden Standard", "field": "priceRupees", "value": "3000"},
    )
    assert len(changes) == 2
    assert rooms(draft)["Deluxe Balcony"]["priceRupees"] == 4200
    assert rooms(draft)["Garden Standard"]["priceRupees"] == 3000


# ── what a correction cannot do ──────────────────────────────────────────────

def test_a_room_that_is_not_in_the_draft_changes_nothing():
    """
    A model hallucinating a room, and the right response to a hallucination is
    that nothing happens.
    """
    draft, changes = apply(
        {"room": "Presidential Suite", "field": "priceRupees", "value": "90000"})
    assert draft == DRAFT
    assert changes == []


def test_a_field_nobody_allowed_changes_nothing():
    draft, changes = apply(
        {"room": "Deluxe Balcony", "field": "propertyId", "value": "somewhere-else"},
        {"room": "", "field": "policies", "value": "no refunds ever"},
        {"room": "", "field": "pincode", "value": "999999"},
    )
    assert draft == DRAFT
    assert changes == []


def test_a_correction_cannot_add_a_room():
    _, changes = apply({"room": "", "field": "roomTypes", "value": "New Suite"})
    assert changes == []


def test_a_correction_cannot_delete_a_room():
    draft, _ = apply({"room": "Deluxe Balcony", "field": "name", "value": ""})
    assert len(draft["roomTypes"]) == 2
    assert rooms(draft)["Deluxe Balcony"]["name"] == "Deluxe Balcony"


@pytest.mark.parametrize("value", ["9000000", "0", "not a price"])
def test_a_price_outside_the_bounds_is_refused(value):
    """
    The same gate `normalise` runs. Where a number came from is not a property
    of the field it is going into.
    """
    draft, _ = apply({"room": "Deluxe Balcony", "field": "priceRupees", "value": value})
    assert rooms(draft)["Deluxe Balcony"]["priceRupees"] in (4500, None)


@pytest.mark.parametrize("field", ["priceRupees", "totalUnits", "maxOccupancy"])
def test_a_negative_number_is_not_quietly_made_positive(field):
    """
    `to_int` drops the sign, so "-500" would have become a price of ₹500. A
    minus almost certainly means "take 500 off", which no field here can
    express, so the honest answer is that nothing moves.
    """
    draft, changes = apply({"room": "Deluxe Balcony", "field": field, "value": "-500"})
    assert rooms(draft)["Deluxe Balcony"][field] is None
    assert "nothing" in changes[0]


def test_an_occupancy_a_hotel_could_not_have_is_refused():
    draft, _ = apply(
        {"room": "Deluxe Balcony", "field": "maxOccupancy", "value": "400"})
    assert rooms(draft)["Deluxe Balcony"]["maxOccupancy"] in (2, None)


def test_a_meal_plan_nobody_recognises_becomes_nothing_not_itself():
    draft, _ = apply(
        {"room": "Deluxe Balcony", "field": "mealPlan", "value": "all inclusive plus"})
    assert rooms(draft)["Deluxe Balcony"]["mealPlan"] is None


def test_a_room_name_is_matched_case_insensitively_but_not_loosely():
    draft, changes = apply(
        {"room": "deluxe balcony", "field": "priceRupees", "value": "4200"})
    assert rooms(draft)["Deluxe Balcony"]["priceRupees"] == 4200
    # "Deluxe" alone is not "Deluxe Balcony". A near-miss is a model guessing.
    draft, changes = apply({"room": "Deluxe", "field": "priceRupees", "value": "1"})
    assert changes == []


def test_renaming_a_room_keeps_it_addressable():
    draft, changes = apply(
        {"room": "Garden Standard", "field": "name", "value": "Garden Room"},
        {"room": "Garden Room", "field": "priceRupees", "value": "3400"},
    )
    assert rooms(draft)["Garden Room"]["priceRupees"] == 3400
    assert len(changes) == 2


def test_nothing_is_mutated_in_place():
    """The caller's draft is what the hotelier is still looking at."""
    original = copy.deepcopy(DRAFT)
    extract.apply_edits(DRAFT, [
        {"room": "Deluxe Balcony", "field": "priceRupees", "value": "4200"}])
    assert DRAFT == original


def test_a_flood_of_edits_is_bounded():
    edits = [{"room": "Deluxe Balcony", "field": "priceRupees", "value": str(4000 + i)}
             for i in range(500)]
    draft, changes = extract.apply_edits(copy.deepcopy(DRAFT), edits)
    assert len(changes) <= extract.MAX_ROOMS * 4


def test_junk_in_the_edit_list_is_survived():
    draft, changes = extract.apply_edits(
        copy.deepcopy(DRAFT),
        ["not a dict", None, 42, {"room": "Deluxe Balcony", "field": "priceRupees",
                                  "value": "4200"}],
    )
    assert changes == ["Deluxe Balcony: price rupees 4500 → 4200"]


# ── the found lines follow the correction ────────────────────────────────────

def test_what_the_owner_reads_is_regenerated():
    draft, _ = apply({"room": "Garden Standard", "field": "name", "value": "Garden Room"})
    assert any("Garden Room" in line for line in draft["found"]), draft["found"]
    assert not any("Garden Standard" in line for line in draft["found"])


def test_an_unchanged_draft_is_left_exactly_alone():
    draft, changes = apply({"room": "Nobody", "field": "priceRupees", "value": "1"})
    assert "found" not in draft or draft.get("found") == DRAFT.get("found")
    assert changes == []


# ── the header the model sometimes will not carry down ───────────────────────

def test_a_draft_with_rooms_and_no_prices_is_read_again(monkeypatch):
    """
    A rate sheet puts the price basis once, above the rows. About one run in
    four the model reads each row in isolation and returns the names, the unit
    counts, and null for every price — which is not a document with no prices,
    it is the header dropped. The gateway's ladder cannot see it, because the
    answer is valid JSON, so the check is on the shape of the result.
    """
    import asyncio

    calls: list[str] = []

    async def two_answers(contents, *, tier="muscle", **kwargs):
        calls.append(tier)
        if tier == "muscle":
            return {"confidence": 0.7, "room_types": [
                {"name": "Deluxe Balcony", "total_units": 4},
                {"name": "Garden Standard", "total_units": 6}]}
        return {"confidence": 0.9, "room_types": [
            {"name": "Deluxe Balcony", "total_units": 4, "price_rupees": 4500},
            {"name": "Garden Standard", "total_units": 6, "price_rupees": 3200}]}

    monkeypatch.setattr(extract, "generate_json", two_answers)
    draft = asyncio.run(extract.extract_hotel(text="a tariff table" * 5))

    assert calls == ["muscle", "brain"]
    assert [r["priceRupees"] for r in draft["roomTypes"]] == [4500, 3200]


def test_a_draft_that_already_has_a_price_is_not_read_twice():
    """One retry on a shape, not a second call on every extraction."""
    import asyncio

    calls: list[str] = []

    async def priced(contents, *, tier="muscle", **kwargs):
        calls.append(tier)
        return {"confidence": 0.9, "room_types": [
            {"name": "Deluxe Balcony", "total_units": 4, "price_rupees": 4500}]}

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(extract, "generate_json", priced)
        asyncio.run(extract.extract_hotel(text="a tariff table" * 5))
    assert calls == ["muscle"]


def test_a_brochure_that_really_has_no_prices_keeps_its_first_answer(monkeypatch):
    """A facilities PDF costs one extra call and comes back the same, rather
    than coming back empty."""
    import asyncio

    async def never_priced(contents, *, tier="muscle", **kwargs):
        return {"confidence": 0.6, "room_types": [{"name": "Garden Standard"}]}

    monkeypatch.setattr(extract, "generate_json", never_priced)
    draft = asyncio.run(extract.extract_hotel(text="a facilities brochure" * 5))
    assert [r["name"] for r in draft["roomTypes"]] == ["Garden Standard"]


def test_a_failed_retry_does_not_lose_the_first_draft(monkeypatch):
    import asyncio

    async def muscle_ok_brain_dies(contents, *, tier="muscle", **kwargs):
        if tier == "brain":
            raise RuntimeError("the reasoning tier is down")
        return {"confidence": 0.7, "room_types": [{"name": "Garden Standard"}]}

    monkeypatch.setattr(extract, "generate_json", muscle_ok_brain_dies)
    draft = asyncio.run(extract.extract_hotel(text="a tariff table" * 5))
    assert [r["name"] for r in draft["roomTypes"]] == ["Garden Standard"]
