"""
Changing a room that already exists.

**There was no way to.** Every one of the twenty-one room types in production
was created before the form asked for a description, amenities or photos, so
every one of them had none — and no route existed that could add them. The AI
employee had been taught to send room photos some weeks before any hotel had a
way to have one, and a guest mid-purchase was told the hotel had not put any up.

The contract these tests hold is the one that makes a PATCH safe to call from a
screen that edits one thing: **an absent key means unchanged, an empty value
means cleared.** Without that distinction, renaming a room would silently empty
its description — which is the same class of bug as the state this route exists
to repair.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.services.pms import onboarding
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ROOM = "10000000-0000-4000-8000-000000000001"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    monkeypatch.setattr("app.services.pms.onboarding.get_db", lambda: fake)
    fake.seed("room_types", {
        "id": ROOM, "property_id": PROPERTY, "partner_id": PARTNER,
        "code": "DELUXE", "name": "Deluxe Room", "total_units": 4,
        "max_occupancy": 2, "standard_occupancy": 2,
        "description": "Corner room with a balcony.", "bed_type": "1 king",
        "size_sqft": 320, "amenities": ["AC", "WiFi"],
        "photos": ["https://cdn.example/a.jpg"], "is_active": True,
    })
    return fake


def edit(**fields) -> dict:
    return run(onboarding.update_room_type(
        property_id=PROPERTY, room_type_id=ROOM, fields=fields,
    ))


def stored(db) -> dict:
    return db.rows("room_types")[0]


# ── the contract ─────────────────────────────────────────────────────────────

def test_a_field_nobody_sent_is_left_alone(db):
    edit(name="Deluxe Garden Room")
    row = stored(db)
    assert row["name"] == "Deluxe Garden Room"
    assert row["description"] == "Corner room with a balcony."
    assert row["amenities"] == ["AC", "WiFi"]
    assert row["photos"] == ["https://cdn.example/a.jpg"]


def test_an_empty_value_clears(db):
    edit(description="")
    assert stored(db)["description"] is None


def test_an_empty_list_clears(db):
    edit(photos=[])
    assert stored(db)["photos"] == []


def test_an_empty_patch_changes_nothing(db):
    before = dict(stored(db))
    edit()
    assert stored(db) == before


# ── the point of the whole route ─────────────────────────────────────────────

def test_photos_can_be_added_to_a_room_that_had_none(db):
    db.rows("room_types")[0]["photos"] = []
    edit(photos=["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"])
    assert len(stored(db)["photos"]) == 2


def test_photos_are_trimmed_deduplicated_and_bounded(db):
    edit(photos=["  https://cdn.example/1.jpg ", "https://cdn.example/1.jpg",
                 *[f"https://cdn.example/{i}.jpg" for i in range(2, 20)]])
    photos = stored(db)["photos"]
    assert photos[0] == "https://cdn.example/1.jpg"
    assert len(photos) == len(set(photos))
    assert len(photos) <= 12


def test_amenities_a_person_typed_are_cleaned(db):
    edit(amenities=["AC", "ac", " WiFi ", ""])
    assert stored(db)["amenities"] == ["AC", "WiFi"]


# ── the rules that already applied to creating one ───────────────────────────

def test_a_name_that_belongs_to_another_room_is_refused(db):
    db.seed("room_types", {
        "id": "other", "property_id": PROPERTY, "partner_id": PARTNER,
        "code": "SUITE", "name": "Suite", "total_units": 1, "max_occupancy": 2,
        "standard_occupancy": 2, "is_active": True, "photos": [], "amenities": [],
    })
    with pytest.raises(HTTPException) as exc:
        edit(name="Suite")
    assert "already have a room type" in exc.value.detail


def test_keeping_your_own_name_is_not_a_clash(db):
    edit(name="Deluxe Room", description="Now with a view.")
    assert stored(db)["description"] == "Now with a view."


@pytest.mark.parametrize("fields", [
    {"name": "A"},
    {"total_units": 0},
    {"total_units": 1000},
    {"max_occupancy": 0},
    {"max_occupancy": 31},
])
def test_the_same_limits_as_creating_a_room(db, fields):
    with pytest.raises(HTTPException):
        edit(**fields)


def test_lowering_the_maximum_lowers_the_standard_occupancy(db):
    """
    Every indicative price is computed from `standard_occupancy`. A room whose
    maximum drops to one must not still claim two sleep in it.
    """
    edit(max_occupancy=1)
    row = stored(db)
    assert row["max_occupancy"] == 1
    assert row["standard_occupancy"] == 1


def test_raising_the_maximum_does_not_raise_the_standard(db):
    edit(max_occupancy=4)
    assert stored(db)["standard_occupancy"] == 2


# ── isolation ────────────────────────────────────────────────────────────────

def test_another_hotels_room_is_not_editable(db):
    db.seed("room_types", {
        "id": "theirs", "property_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "partner_id": PARTNER, "code": "X", "name": "Theirs", "total_units": 1,
        "max_occupancy": 2, "standard_occupancy": 2, "is_active": True,
        "photos": [], "amenities": [],
    })
    with pytest.raises(HTTPException) as exc:
        run(onboarding.update_room_type(
            property_id=PROPERTY, room_type_id="theirs", fields={"name": "Mine now"},
        ))
    assert "not one of yours" in exc.value.detail
    assert db.store["room_types"][1]["name"] == "Theirs"


def test_a_room_that_does_not_exist_is_refused(db):
    with pytest.raises(HTTPException):
        run(onboarding.update_room_type(
            property_id=PROPERTY, room_type_id="nope", fields={"name": "Ghost"},
        ))
