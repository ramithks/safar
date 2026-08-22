"""
The hotel's own facts, and what it still cannot do without them.

**None of this was editable through any route.** `properties.address`, `lat`,
`lon`, the check-in times and the whole policy object were writable only by
editing Postgres by hand — which is why nine of ten live hotels have no
address, no map pin and empty policies. The consequences are not cosmetic: the
AI hands a guest over rather than answering "what's your cancellation policy?",
and the hotel's website cannot be placed on a map or submitted for indexing.

Two rules are load-bearing here:

  * **A policies save must not drop the commercial settings** that share the
    column. `token_pct` and `max_discount_pct` are read by the pricing engine,
    and a screen that wrote the whole object would delete a hotel's deposit
    percentage the first time somebody typed a pet policy.
  * **Readiness names gaps rather than scoring them.** "You are at 62%" tells a
    hotelier nothing they can act on.
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.services.pms import onboarding
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    monkeypatch.setattr("app.services.pms.onboarding.get_db", lambda: fake)
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "slug": "casa-mandala-anjuna",
        "name": "Casa Mandala", "city": "Anjuna", "status": "active",
        "address": None, "state": None, "country": None, "pincode": None,
        "lat": None, "lon": None, "property_type": "boutique", "total_rooms": 9,
        "check_in_time": "14:00", "check_out_time": "11:00",
        # The commercial half of the column, which no policy screen may touch.
        "policies": {"token_pct": 20, "max_discount_pct": 15},
    })
    return fake


def edit(**fields) -> dict:
    return run(onboarding.update_property(property_id=PROPERTY, fields=fields))


def stored(db) -> dict:
    return db.rows("properties")[0]


# ── the PATCH contract ───────────────────────────────────────────────────────

def test_an_address_can_finally_be_set(db):
    edit(address="412 Ozran Beach Road", state="Goa", country="in", pincode="403509")
    row = stored(db)
    assert row["address"] == "412 Ozran Beach Road"
    assert row["state"] == "Goa"
    assert row["country"] == "IN"       # normalised, because schema.org wants ISO
    assert row["pincode"] == "403509"


def test_a_field_nobody_sent_is_left_alone(db):
    edit(address="412 Ozran Beach Road")
    assert stored(db)["check_in_time"] == "14:00"
    assert stored(db)["city"] == "Anjuna"


def test_an_empty_value_clears(db):
    edit(address="somewhere")
    edit(address="")
    assert stored(db)["address"] is None


def test_a_map_pin_can_be_accepted(db):
    edit(lat=15.5937, lon=73.7444)
    assert stored(db)["lat"] == 15.5937
    assert stored(db)["lon"] == 73.7444


@pytest.mark.parametrize("value", ["2pm", "24:00", "14:99", "afternoon"])
def test_a_time_that_is_not_a_time_is_refused(db, value):
    with pytest.raises(HTTPException):
        edit(check_in_time=value)


def test_a_real_time_is_accepted(db):
    edit(check_in_time="13:30")
    assert stored(db)["check_in_time"] == "13:30"


# ── the column that carries two different things ─────────────────────────────

def test_writing_a_policy_does_not_drop_the_pricing_settings(db):
    """
    The bug this prevents: a hotelier types a pet policy and their deposit
    percentage silently becomes nothing, which the pricing engine then reads.
    """
    edit(policies={"pets": "No pets, sorry."})
    policies = stored(db)["policies"]
    assert policies["pets"] == "No pets, sorry."
    assert policies["token_pct"] == 20
    assert policies["max_discount_pct"] == 15


def test_a_policy_screen_cannot_set_a_pricing_setting(db):
    """The other direction, and the one that costs money."""
    edit(policies={"max_discount_pct": "90", "token_pct": "0"})
    policies = stored(db)["policies"]
    assert policies["max_discount_pct"] == 15
    assert policies["token_pct"] == 20


def test_policies_merge_rather_than_replace(db):
    edit(policies={"pets": "No pets."})
    edit(policies={"smoking": "Not indoors."})
    policies = stored(db)["policies"]
    assert policies["pets"] == "No pets."
    assert policies["smoking"] == "Not indoors."


def test_an_empty_policy_is_removed(db):
    edit(policies={"pets": "No pets."})
    edit(policies={"pets": ""})
    assert "pets" not in stored(db)["policies"]


def test_another_hotel_is_not_editable(db):
    with pytest.raises(HTTPException) as exc:
        run(onboarding.update_property(property_id="not-mine", fields={"address": "x"}))
    assert "not one of yours" in exc.value.detail


# ── readiness ────────────────────────────────────────────────────────────────

def complete(db) -> None:
    """Everything a hotel needs to answer questions and be found."""
    db.rows("properties")[0].update({
        "address": "412 Ozran Beach Road", "lat": 15.5937, "lon": 73.7444,
        "policies": {
            "token_pct": 20, "cancellation": "Free until 48 hours before.",
            "children": "Under 6 free.", "pets": "No pets.",
            "smoking": "Not indoors.", "id_required": "Photo ID at check-in.",
        },
    })
    db.seed("room_types", {
        "id": "rt-1", "property_id": PROPERTY, "is_active": True,
        "name": "Deluxe", "description": "A corner room with a balcony.",
        "photos": ["a.jpg", "b.jpg", "c.jpg", "d.jpg"],
    })
    db.seed("hotel_websites", {
        "property_id": PROPERTY, "status": "published",
        "published": {
            "about": "A nine-room house a short walk from Ozran beach, run by the "
                     "family who built it. Mornings are quiet and the kitchen does "
                     "one breakfast well.",
            "amenities": ["Rooftop restaurant", "Free parking", "Garden"],
        },
    })


def test_a_complete_hotel_has_no_gaps(db):
    complete(db)
    report = run(onboarding.readiness(PROPERTY))
    assert report["gaps"] == []
    assert report["agentReady"] is True
    assert report["websiteIndexable"] is True


def test_a_bare_hotel_is_told_what_is_missing(db):
    report = run(onboarding.readiness(PROPERTY))
    ids = {g["id"] for g in report["gaps"]}
    assert {"photos", "location", "address", "cancellation", "about"} <= ids
    assert report["agentReady"] is False
    assert report["websiteIndexable"] is False


def test_every_gap_says_what_it_blocks(db):
    for gap in run(onboarding.readiness(PROPERTY))["gaps"]:
        assert gap["affects"], gap["id"]
        assert set(gap["affects"]) <= {"agent", "website"}
        # A gap with no sentence is a checkbox, which is the thing this is not.
        assert len(gap["detail"]) > 20


def test_the_photo_gap_counts_what_is_actually_there(db):
    complete(db)
    db.rows("room_types")[0]["photos"] = ["a.jpg"]
    gap = next(g for g in run(onboarding.readiness(PROPERTY))["gaps"] if g["id"] == "photos")
    assert "You have 1" in gap["detail"]
    assert str(onboarding.MIN_WEBSITE_PHOTOS) in gap["detail"]


def test_a_draft_website_is_a_gap_but_only_for_the_website(db):
    complete(db)
    db.rows("hotel_websites")[0]["status"] = "draft"
    report = run(onboarding.readiness(PROPERTY))
    gap = next(g for g in report["gaps"] if g["id"] == "publish")
    assert gap["affects"] == ["website"]
    # An unpublished page does not stop the AI answering a guest.
    assert report["agentReady"] is True
    assert report["websiteIndexable"] is False


def test_a_room_without_a_description_is_named(db):
    complete(db)
    db.seed("room_types", {
        "id": "rt-2", "property_id": PROPERTY, "is_active": True,
        "name": "Suite", "description": None, "photos": ["e.jpg"],
    })
    gap = next(
        g for g in run(onboarding.readiness(PROPERTY))["gaps"]
        if g["id"] == "room_descriptions"
    )
    assert "1 of 2" in gap["detail"]


# ── the two definitions of "ready for Google" ────────────────────────────────
#
# There are two, and they must be the same decision. `stayosHotelGate` in the
# website decides what actually gets indexed; `readiness` decides what a
# hotelier is told. They had drifted, and the drift told hoteliers the opposite
# of the truth: Casa Mandala was live in the sitemap with `index, follow` on the
# page and read "Google ✗" on its own dashboard, because two of its four rooms
# had no description.

def test_a_hotel_google_is_indexing_is_not_told_it_is_invisible(db):
    """
    The exact shape that was wrong: every requirement met, three signals
    present, and one *signal-level* gap outstanding. The gate indexes this. The
    dashboard used to say it would not.
    """
    complete(db)
    db.seed("room_types", {
        "id": "rt-2", "property_id": PROPERTY, "is_active": True,
        "name": "Suite", "description": None, "photos": ["e.jpg"],
    })
    report = run(onboarding.readiness(PROPERTY))

    assert report["websiteIndexable"] is True
    # And the gap is still reported, because it is still worth fixing — it is
    # what stops the AI answering "what's in the Suite?".
    assert "room_descriptions" in {g["id"] for g in report["gaps"]}
    assert report["agentReady"] is False


def test_too_few_photos_is_a_requirement_and_does_block(db):
    complete(db)
    db.rows("room_types")[0]["photos"] = ["a.jpg"]
    assert run(onboarding.readiness(PROPERTY))["websiteIndexable"] is False


def test_no_rooms_at_all_blocks(db):
    complete(db)
    db.rows("room_types").clear()
    assert run(onboarding.readiness(PROPERTY))["websiteIndexable"] is False


def test_an_unpublished_page_blocks_whatever_else_is_true(db):
    complete(db)
    db.rows("hotel_websites")[0]["status"] = "draft"
    assert run(onboarding.readiness(PROPERTY))["websiteIndexable"] is False


def test_two_signals_are_not_enough(db):
    """Three of five, the same as the gate. Two is a page that loses to the OTA
    result above it."""
    complete(db)
    # Strip it back to room photos + policies: two signals.
    db.rows("hotel_websites")[0]["published"] = {"about": "Short.", "amenities": []}
    db.rows("properties")[0].update({"lat": None, "lon": None})
    report = run(onboarding.readiness(PROPERTY))
    assert report["websiteIndexable"] is False


def test_three_signals_are(db):
    complete(db)
    db.rows("hotel_websites")[0]["published"] = {"about": "Short.", "amenities": []}
    # description gone, amenities gone → room photos, policies, map pin = three.
    assert run(onboarding.readiness(PROPERTY))["websiteIndexable"] is True
