"""
A finished booking must stop being the current conversation.

`session.merge` was `{**profile, **patch}` and nothing ever removed anything. So
a session only accumulated, and three things followed from that:

- A guest who booked **10–14 September, 2 rooms, 4 adults** and came back in
  October about **20–22 October, 1 room, 2 adults** kept `units: 2` and the old
  `children` for any field the new tool call did not happen to set.
- `quote_id`, `room_type_id` and `rate_plan_id` kept pointing at a stay that had
  already been paid for.
- `describe()` put September's dates into the system prompt of an October
  conversation, under the heading *"Do not ask for them again"*.

The rule now: **what the guest told us about a stay belongs to that stay.** When
the stay changes, or ends, or goes cold, it goes with it. What we know about the
*guest* — their language, above all — never does.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.messaging import session
from tests.fake_supabase import FakeDb

CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"

SEPTEMBER = {
    "check_in": "2026-09-10", "check_out": "2026-09-14",
    "adults": 4, "children": 1, "units": 2,
    "stage": "booked", "quote_id": "q-september",
    "room_type_id": "rt-king", "rate_plan_id": "rp-cp",
    "language": "hinglish",
}


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    fake.seed("conversations", {"id": CONVERSATION, "guest_profile": dict(SEPTEMBER)})
    monkeypatch.setattr("app.services.messaging.session.get_db", lambda: fake)
    return fake


def profile(db) -> dict:
    return db.rows("conversations")[0]["guest_profile"]


# ── a different stay ────────────────────────────────────────────────────────

def test_new_dates_replace_the_old_stay_rather_than_merging_into_it(db):
    """
    The §10 case, verbatim: 10–14 Sep / 2 rooms / 4 adults, then 20–22 Oct /
    1 room / 2 adults. Nothing *priced* about September may survive.

    **Narrowed, deliberately, from "nothing may survive".** The original rule
    took the chosen room with it, and that turned out to delete a fact it had no
    business touching: a guest who picks the Deluxe Balcony and then says
    "actually the 20th to the 22nd" has changed their dates, not their mind
    about the room, and being asked which room they wanted was the exact
    complaint the audit had just fixed one layer up in the prompt.

    So the split is between a *preference* and a *price*. The room stays and is
    marked for re-checking (see the test below); the quote, the rate plan, the
    stage and the party size go, because every one of them was true only of
    nights the guest is no longer asking about.
    """
    session.merge(CONVERSATION, {
        "check_in": "2026-10-20", "check_out": "2026-10-22",
        "adults": 2, "units": 1, "stage": "browsing",
    })

    now = profile(db)
    assert now["check_in"] == "2026-10-20"
    assert now["units"] == 1
    assert now["adults"] == 2
    # The fields the new search did not mention are *gone*, not inherited.
    assert "children" not in now
    assert "quote_id" not in now
    assert "rate_plan_id" not in now
    assert now["stage"] == "browsing"


def test_a_change_of_dates_keeps_the_room_but_not_the_promise(db):
    """
    §9: "guest changes dates → preserve selected room if still valid".

    Valid is the operative word and it is not knowable here — availability is a
    tool call away — so the room is kept together with the fact that nobody has
    checked it. `describe()` turns that into an instruction to re-check before
    promising, which is the honest version of remembering.
    """
    session.merge(CONVERSATION, {"check_in": "2026-10-20", "check_out": "2026-10-22"})

    now = profile(db)
    assert now["room_type_id"] == "rt-king"
    assert now["room_needs_recheck"] is True

    told = session.describe({"guest_profile": now})
    assert "rt-king" in told
    assert "different dates" in told


def test_a_room_that_stops_being_offered_is_taken_off_the_session(db):
    """
    §9: "room becomes unavailable → explicitly invalidate it and ask again".

    The failure without this is not a repeated question — it is the agent being
    told in its own prompt that the guest has settled on a room, and offering
    them a room that sold an hour ago.
    """
    session.merge(CONVERSATION, {"_offered_room_type_ids": ["rt-deluxe", "rt-suite"]})

    now = profile(db)
    assert "room_type_id" not in now
    assert "rate_plan_id" not in now
    assert now["room_unavailable"] is True
    assert "_offered_room_type_ids" not in now, "the private key must not be stored"

    told = session.describe({"guest_profile": now})
    assert "no longer free" in told


def test_a_room_still_on_offer_is_confirmed_rather_than_doubted(db):
    """The other half: an availability read that finds it settles the recheck."""
    session.merge(CONVERSATION, {"room_needs_recheck": True})
    session.merge(CONVERSATION, {"_offered_room_type_ids": ["rt-king", "rt-suite"]})

    now = profile(db)
    assert now["room_type_id"] == "rt-king"
    assert "room_needs_recheck" not in now
    assert "room_unavailable" not in now


def test_a_turn_that_never_asked_about_availability_invalidates_nothing(db):
    """
    A turn about breakfast cannot take the guest's room away.

    The key is absent rather than empty on such a turn, and that distinction is
    the whole safety property: absent means "nobody asked", empty means "we
    asked and the hotel has nothing", and only the second may delete anything.
    """
    session.merge(CONVERSATION, {"adults": 3})

    now = profile(db)
    assert now["room_type_id"] == "rt-king"
    assert "room_unavailable" not in now


def test_a_sold_out_stay_takes_the_room_with_it(db):
    """
    The empty-list half of the same rule. `search_availability` returns no room
    types when nothing is free, and a session that kept the chosen room through
    that would have the prompt insisting on a room the hotel cannot sell.
    """
    session.merge(CONVERSATION, {"_offered_room_type_ids": []})

    now = profile(db)
    assert "room_type_id" not in now
    assert now["room_unavailable"] is True


def test_the_guests_language_survives_a_change_of_stay(db):
    """
    The one thing on this column that is about the person rather than the trip.
    Losing it is the bug Phase 2 exists to have fixed.
    """
    session.merge(CONVERSATION, {"check_in": "2026-10-20", "check_out": "2026-10-22"})
    assert profile(db)["language"] == "hinglish"


def test_correcting_the_party_size_is_not_a_new_stay(db):
    """
    "Actually make it three of us" is a correction to the stay being discussed,
    not a different trip. Dates are the signal for that, because dates are what
    a guest always restates when they mean a different one.

    **One assertion here changed, from keeping the quote to dropping it, and it
    is the opposite of a weakening.**

    It used to read `assert now["quote_id"] == "q-september"`, on the reasoning
    that a guest three messages into a quote should not lose it. That reasoning
    conflates two questions the session has to answer separately:

        is this a different trip?     no — and everything below still asserts so
        is the quote still true?      no. It priced two adults.

    A quote is derived state and its inputs are the exact stay it priced. Kept
    after the party changes, it goes into the next prompt as *"there is a live
    quote for ₹11,400"* and the model offers a three-person party a price
    calculated for two — on a room that may not even sleep three. That is the
    whole class of failure `DERIVED_FROM` was added for, and this test was
    pinning one instance of it.

    What is asserted instead is both halves: the stay survives, and the price
    for a different party does not.
    """
    session.merge(CONVERSATION, {"adults": 3})

    now = profile(db)
    assert now["adults"] == 3
    # Not a new trip: the dates and the room they chose are still theirs.
    assert now["check_in"] == "2026-09-10"
    assert now.get("room_type_id") == profile(db).get("room_type_id")
    # But the price is not. It was for two.
    assert "quote_id" not in now, (
        "a quote priced for two adults survived the guest saying there are three"
    )


def test_the_same_dates_arriving_again_change_nothing(db):
    session.merge(CONVERSATION, {"check_in": "2026-09-10", "check_out": "2026-09-14"})

    now = profile(db)
    assert now["quote_id"] == "q-september"
    assert now["units"] == 2


# ── a stay that has ended ───────────────────────────────────────────────────

def test_a_confirmed_booking_closes_the_session(db):
    """
    After the confirmation the guest is owed `get_booking_status`, which reads
    the reservation. None of this is needed and all of it is misleading.
    """
    session.close(CONVERSATION)

    now = profile(db)
    for gone in ("check_in", "check_out", "adults", "children", "units",
                 "stage", "quote_id", "room_type_id", "rate_plan_id"):
        assert gone not in now, f"{gone} outlived the booking"
    assert now["language"] == "hinglish"


def test_closing_keeps_anything_else_stored_on_the_column(db):
    """
    `guest_profile` is shared. Closing a session is a subtraction of a named
    list, not "keep language and drop the rest" — so the next thing somebody
    stores here is not silently deleted by a booking ending.
    """
    db.rows("conversations")[0]["guest_profile"]["vip_note"] = "regular, likes room 12"
    session.close(CONVERSATION)
    assert profile(db)["vip_note"] == "regular, likes room 12"


def test_closing_an_already_closed_session_writes_nothing(db):
    session.close(CONVERSATION)
    before = dict(profile(db))
    session.close(CONVERSATION)
    assert profile(db) == before


def test_describe_says_nothing_once_the_session_is_closed(db):
    """
    The end of the chain. `describe` is what puts the dates in the prompt, and
    a closed session must produce no sentence at all rather than a stale one.
    """
    assert session.describe({"guest_profile": SEPTEMBER}) is not None

    session.close(CONVERSATION)
    assert session.describe({"guest_profile": profile(db)}) is None


# ── a stay that has gone cold ───────────────────────────────────────────────

def test_a_session_nobody_has_touched_for_days_is_not_carried_forward(db):
    """
    A guest returning after three days has come back about something else, and
    dates given last week are not an answer to a question asked today.
    """
    stale = datetime.now(timezone.utc) - timedelta(hours=session.STALE_AFTER_HOURS + 1)
    db.rows("conversations")[0]["guest_profile"]["touched_at"] = stale.isoformat()

    session.merge(CONVERSATION, {"adults": 2})

    now = profile(db)
    assert "quote_id" not in now
    assert "check_in" not in now
    assert now["adults"] == 2
    assert now["language"] == "hinglish"


def test_an_hours_gap_keeps_the_session(db):
    """
    Failure ⑦ of the reference conversation, which this must not reintroduce:
    an hour passed and every fact was re-asked. An hour is the same
    conversation.
    """
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    db.rows("conversations")[0]["guest_profile"]["touched_at"] = recent.isoformat()

    session.merge(CONVERSATION, {"adults": 4})

    assert profile(db)["check_in"] == "2026-09-10"
    assert profile(db)["quote_id"] == "q-september"


def test_a_session_with_no_timestamp_is_not_treated_as_ancient(db):
    """Every session written before this existed has no `touched_at`."""
    assert "touched_at" not in SEPTEMBER
    session.merge(CONVERSATION, {"adults": 4})
    assert profile(db)["quote_id"] == "q-september"


def test_the_clock_measures_the_stay_and_not_the_traffic(db):
    """
    `touched_at` is stamped only when something actually changed. Stamping it
    on every turn would mean a guest chatting about breakfast for three days
    kept a quote alive that expired on the first one.
    """
    session.merge(CONVERSATION, {"adults": 4})   # already 4 — no change
    assert "touched_at" not in profile(db)

    session.merge(CONVERSATION, {"adults": 5})
    assert "touched_at" in profile(db)


# ── the things that must not have changed ───────────────────────────────────

def test_an_empty_patch_still_writes_nothing(db):
    before = dict(profile(db))
    session.merge(CONVERSATION, {})
    assert profile(db) == before


def test_a_write_failure_is_never_fatal(monkeypatch):
    """
    A session that failed to save is a stale sentence in a prompt. Losing the
    guest's message over it would be worse.
    """
    def explode():
        raise RuntimeError("connection reset")

    monkeypatch.setattr("app.services.messaging.session.get_db", explode)
    session.merge(CONVERSATION, {"adults": 2})   # must not raise
    session.close(CONVERSATION)                  # nor this
