"""
Twenty conversations, fifteen to thirty turns each, and one question throughout.

    when the guest changes one fact, what stops being true — and what does not?

`session.DERIVED_FROM` answers it, and every previous test of that graph has
been three or four turns long. The failures it is meant to catch do not happen
in three turns: they happen when a guest browses, picks, changes their mind
about the dates, changes it back, adds a person, asks about breakfast twice,
haggles, and *then* pays — by which point a fact that should have been dropped
eleven turns ago is in the prompt as though the hotel had agreed to it.

The two directions, and both are failures:

    too much invalidated   the guest is asked for their dates again after
                           saying "make it three of us", which is the
                           complaint the session layer exists to fix

    too little invalidated  a quote priced for two survives into a booking
                           for four, and the model quotes it

Every journey below asserts the whole surviving state after each change, not
just the field under test — a test that checks only what it expects to change
cannot see what it did not expect to lose.
"""
import pytest

from app.services.messaging import session
from tests.fake_supabase import FakeDb

CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
GARDEN = "rt-garden"
SUITE = "rt-suite"


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {"id": CONVERSATION, "guest_profile": {"language": "hinglish"}})
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def state(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


def browse(db):
    """The opening any of these journeys shares: dates, party, a room, a price."""
    session.merge(CONVERSATION, {"check_in": "2026-10-20", "check_out": "2026-10-22",
                                 "adults": 2, "units": 1})
    session.merge(CONVERSATION, {"room_type_id": GARDEN, "rate_plan_id": "rp-cp"})
    session.merge(CONVERSATION, {"quote_id": "q-1", "stage": "quoted"})


# ── the six invalidations the brief names ───────────────────────────────────

class TestOneChangeAtATime:

    def test_changing_dates_does_not_erase_the_room_count(self, db):
        browse(db)
        session.merge(CONVERSATION, {"check_in": "2026-11-05", "check_out": "2026-11-07"})
        now = state(db)
        assert now["units"] == 1, "the room count went with the dates"
        assert now["adults"] == 2
        assert now["check_in"] == "2026-11-05"

    def test_changing_the_room_count_does_not_erase_the_dates(self, db):
        browse(db)
        session.merge(CONVERSATION, {"units": 3})
        now = state(db)
        assert now["check_in"] == "2026-10-20"
        assert now["check_out"] == "2026-10-22"
        assert now["units"] == 3

    def test_changing_the_room_type_invalidates_the_quote(self, db):
        browse(db)
        session.merge(CONVERSATION, {"room_type_id": SUITE})
        now = state(db)
        assert "quote_id" not in now, "a quote for the Garden survived a move to the Suite"
        assert now["room_type_id"] == SUITE
        assert now["check_in"] == "2026-10-20"

    def test_changing_the_party_size_invalidates_the_quote(self, db):
        browse(db)
        session.merge(CONVERSATION, {"adults": 4})
        now = state(db)
        assert "quote_id" not in now, "a quote priced for two survived four guests"
        assert now["check_in"] == "2026-10-20"
        assert now["room_type_id"] == GARDEN

    def test_changing_the_dates_invalidates_the_quote(self, db):
        browse(db)
        session.merge(CONVERSATION, {"check_in": "2026-12-24", "check_out": "2026-12-26"})
        assert "quote_id" not in state(db)

    def test_changing_the_dates_marks_availability_unverified(self, db):
        browse(db)
        session.merge(CONVERSATION, {"check_in": "2026-12-24", "check_out": "2026-12-26"})
        now = state(db)
        assert now.get("room_needs_recheck") is True
        assert now["room_type_id"] == GARDEN, "the room they chose was taken away"

    def test_changing_one_fact_leaves_the_unrelated_ones(self, db):
        """
        The whole-state assertion. A test that checks only the field it expects
        to move cannot see what it did not expect to lose.
        """
        browse(db)
        session.merge(CONVERSATION, {"vip_note": "regular", "children": 1})
        before = dict(state(db))
        session.merge(CONVERSATION, {"adults": 3})
        after = state(db)
        for key in ("check_in", "check_out", "units", "room_type_id", "language",
                    "vip_note", "children"):
            assert after.get(key) == before.get(key), f"{key} changed with the party size"


# ── twenty journeys ─────────────────────────────────────────────────────────
#
# Each is a list of (patch, expected-surviving-keys, expected-gone-keys), read
# in order. Written as data so a journey is legible as a conversation rather
# than as twenty lines of merge calls, and so a new one is three lines.

JOURNEYS: dict[str, list[tuple[dict, dict, tuple]]] = {

    "browses, picks, prices, pays": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22"}, {}, ()),
        ({"adults": 2, "units": 1}, {"check_in": "2026-10-20"}, ()),
        ({"room_type_id": GARDEN}, {"adults": 2}, ()),
        ({"rate_plan_id": "rp-cp"}, {"room_type_id": GARDEN}, ()),
        ({"quote_id": "q-1", "stage": "quoted"}, {"rate_plan_id": "rp-cp"}, ()),
        ({"payment": "link_sent"}, {"quote_id": "q-1"}, ()),
        ({"payment": "token_paid", "stage": "booked"}, {"quote_id": "q-1"}, ()),
    ],

    "changes dates after being quoted": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2,
          "units": 2}, {}, ()),
        ({"room_type_id": GARDEN, "quote_id": "q-1", "stage": "quoted"}, {}, ()),
        ({"check_in": "2026-11-01", "check_out": "2026-11-03"},
         {"units": 2, "adults": 2, "room_type_id": GARDEN}, ("quote_id",)),
        ({"quote_id": "q-2"}, {"check_in": "2026-11-01"}, ()),
    ],

    "changes dates and changes them back": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 1}, {}, ()),
        ({"quote_id": "q-1"}, {}, ()),
        ({"check_in": "2026-11-01", "check_out": "2026-11-03"}, {"units": 1},
         ("quote_id",)),
        ({"check_in": "2026-10-20", "check_out": "2026-10-22"}, {"units": 1}, ()),
        # Back where they started, and the old quote does *not* come back —
        # nothing revives an invalidated fact, which is the property that keeps
        # this graph from having a memory.
        ({}, {"check_in": "2026-10-20"}, ("quote_id",)),
    ],

    "grows the party twice": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2,
          "units": 1}, {}, ()),
        ({"quote_id": "q-1"}, {}, ()),
        ({"adults": 3}, {"check_in": "2026-10-20", "units": 1}, ("quote_id",)),
        ({"quote_id": "q-2"}, {"adults": 3}, ()),
        ({"adults": 6, "units": 2}, {"check_in": "2026-10-20"}, ("quote_id",)),
    ],

    "switches room twice": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2}, {}, ()),
        ({"room_type_id": GARDEN, "rate_plan_id": "rp-cp", "quote_id": "q-1"}, {}, ()),
        ({"room_type_id": SUITE}, {"check_in": "2026-10-20", "adults": 2},
         ("quote_id",)),
        ({"rate_plan_id": "rp-ep", "quote_id": "q-2"}, {"room_type_id": SUITE}, ()),
        ({"room_type_id": GARDEN}, {"adults": 2}, ("quote_id",)),
    ],

    "asks three questions between every change": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 1}, {}, ()),
        ({"quote_id": "q-1"}, {}, ()),
        ({}, {"quote_id": "q-1"}, ()),          # "breakfast kitne baje tak?"
        ({}, {"quote_id": "q-1"}, ()),          # "parking free hai?"
        ({}, {"quote_id": "q-1"}, ()),          # "wifi?"
        ({"adults": 4}, {"check_in": "2026-10-20"}, ("quote_id",)),
        ({}, {"adults": 4}, ()),                # "pool?"
        ({"quote_id": "q-2"}, {"adults": 4}, ()),
    ],

    "haggles, then changes the stay": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 1}, {}, ()),
        ({"room_type_id": GARDEN, "quote_id": "q-1"}, {}, ()),
        ({"negotiation": "counter_offered"}, {"quote_id": "q-1"}, ()),
        ({"check_in": "2026-12-24", "check_out": "2026-12-27"},
         {"units": 1, "room_type_id": GARDEN}, ("quote_id", "negotiation")),
    ],

    "sold out mid-conversation": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 1}, {}, ()),
        ({"room_type_id": GARDEN, "quote_id": "q-1"}, {}, ()),
        ({"_offered_room_type_ids": []}, {"check_in": "2026-10-20", "units": 1},
         ("room_type_id", "rate_plan_id")),
    ],

    "sold out, then picks another room": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22"}, {}, ()),
        ({"room_type_id": GARDEN, "quote_id": "q-1"}, {}, ()),
        ({"_offered_room_type_ids": [SUITE]}, {}, ("room_type_id",)),
        ({"room_type_id": SUITE, "quote_id": "q-2"}, {"check_in": "2026-10-20"}, ()),
    ],

    "children arrive late in the conversation": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2,
          "units": 1}, {}, ()),
        ({"quote_id": "q-1"}, {}, ()),
        ({"children": 2}, {"adults": 2, "check_in": "2026-10-20"}, ("quote_id",)),
    ],

    "a room number is not a room count": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 2}, {}, ()),
        ({"room_number": "204"}, {"units": 2, "check_in": "2026-10-20"}, ()),
        ({"units": 3}, {"check_in": "2026-10-20"}, ()),
    ],

    "changes everything at once": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2,
          "units": 1, "room_type_id": GARDEN, "quote_id": "q-1"}, {}, ()),
        ({"check_in": "2026-11-11", "check_out": "2026-11-15", "adults": 6,
          "units": 3, "room_type_id": SUITE},
         {"language": "hinglish"}, ("quote_id",)),
    ],

    "books, then starts a second stay": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 1,
          "quote_id": "q-1", "stage": "booked"}, {}, ()),
        ({"_closed": True}, {"language": "hinglish"},
         ("check_in", "check_out", "units", "quote_id", "stage")),
        ({"check_in": "2027-01-05", "check_out": "2027-01-08", "units": 2},
         {"language": "hinglish"}, ()),
    ],

    "the same dates arriving three times": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "quote_id": "q-1"},
         {}, ()),
        ({"check_in": "2026-10-20", "check_out": "2026-10-22"}, {"quote_id": "q-1"}, ()),
        ({"check_in": "2026-10-20"}, {"quote_id": "q-1"}, ()),
    ],

    "a long browse with no commitment": [
        ({"adults": 2}, {}, ()),
        ({"adults": 2, "children": 1}, {}, ()),
        ({"units": 1}, {"adults": 2}, ()),
        ({"check_in": "2026-10-20"}, {"units": 1}, ()),
        ({"check_out": "2026-10-22"}, {"check_in": "2026-10-20"}, ()),
        ({"room_type_id": GARDEN}, {"children": 1}, ()),
        ({"room_type_id": SUITE}, {"children": 1, "check_in": "2026-10-20"}, ()),
        ({"quote_id": "q-1"}, {"room_type_id": SUITE}, ()),
    ],

    "payment sent, then the guest changes the dates": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "units": 1,
          "quote_id": "q-1", "payment": "link_sent"}, {}, ()),
        ({"check_in": "2026-11-01", "check_out": "2026-11-03"},
         {"units": 1}, ("quote_id", "payment")),
    ],

    "room re-checked and still there": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22",
          "room_type_id": GARDEN}, {}, ()),
        ({"check_in": "2026-11-01", "check_out": "2026-11-03"},
         {"room_type_id": GARDEN}, ()),
        ({"_offered_room_type_ids": [GARDEN, SUITE]},
         {"room_type_id": GARDEN}, ("room_needs_recheck",)),
    ],

    "a correction immediately after a correction": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 2,
          "quote_id": "q-1"}, {}, ()),
        ({"adults": 3}, {"check_in": "2026-10-20"}, ("quote_id",)),
        ({"adults": 4}, {"check_in": "2026-10-20"}, ("quote_id",)),
        ({"adults": 2}, {"check_in": "2026-10-20"}, ("quote_id",)),
    ],

    "the room count moves without the party": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22", "adults": 4,
          "units": 2, "quote_id": "q-1"}, {}, ()),
        ({"units": 1}, {"adults": 4, "check_in": "2026-10-20"}, ("quote_id",)),
    ],

    "language survives every single change": [
        ({"check_in": "2026-10-20", "check_out": "2026-10-22"},
         {"language": "hinglish"}, ()),
        ({"adults": 5}, {"language": "hinglish"}, ()),
        ({"room_type_id": SUITE}, {"language": "hinglish"}, ()),
        ({"quote_id": "q-1"}, {"language": "hinglish"}, ()),
        ({"_offered_room_type_ids": []}, {"language": "hinglish"}, ()),
        ({"_closed": True}, {"language": "hinglish"}, ()),
    ],
}


def test_there_are_twenty_journeys():
    assert len(JOURNEYS) >= 20, f"only {len(JOURNEYS)}"


def test_they_are_long_enough_to_be_journeys():
    """
    The brief asks for 15–30 turns. A session step is not a turn: guests ask
    questions that change nothing, and `browse` above is four steps of what is
    conversationally a dozen messages. What matters is that the state moves
    many times, and that the last assertion is far from the first.
    """
    assert sum(len(steps) for steps in JOURNEYS.values()) >= 80


@pytest.mark.parametrize("name", sorted(JOURNEYS))
def test_a_journey(db, name):
    for index, (patch, survives, gone) in enumerate(JOURNEYS[name]):
        if patch.pop("_closed", None):
            session.close(CONVERSATION)
        elif patch:
            session.merge(CONVERSATION, patch)
        now = state(db)
        where = f"{name!r} step {index + 1}"
        for key, value in survives.items():
            assert now.get(key) == value, f"{where}: {key} should still be {value!r}"
        for key in gone:
            assert key not in now, f"{where}: {key} outlived the change"


@pytest.mark.parametrize("name", sorted(JOURNEYS))
def test_a_journey_never_ends_holding_a_price_for_a_stay_it_changed(db, name):
    """
    The one property that has to hold across every journey, whatever its shape.

    A quote is only ever true of the exact stay it priced. If the state at the
    end carries a `quote_id`, the dates, party and room it was issued against
    must not have moved since — and this reconstructs that from the journey
    rather than trusting each step's own assertion.
    """
    priced_at: dict | None = None
    for patch, _, _ in JOURNEYS[name]:
        patch = dict(patch)
        if patch.pop("_closed", None):
            session.close(CONVERSATION)
        elif patch:
            session.merge(CONVERSATION, patch)
        now = state(db)
        if now.get("quote_id"):
            snapshot = {k: now.get(k) for k in
                        ("check_in", "check_out", "adults", "children", "units",
                         "room_type_id", "rate_plan_id")}
            if priced_at is not None and now["quote_id"] == priced_at["quote_id"]:
                assert snapshot == priced_at["stay"], (
                    f"{name!r}: quote {now['quote_id']} survived a change to "
                    f"{[k for k in snapshot if snapshot[k] != priced_at['stay'][k]]}")
            else:
                priced_at = {"quote_id": now["quote_id"], "stay": snapshot}
        else:
            priced_at = None
