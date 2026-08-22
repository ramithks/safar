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
    1 room / 2 adults. Nothing of September may survive.
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
    assert "room_type_id" not in now
    assert "rate_plan_id" not in now
    assert now["stage"] == "browsing"


def test_the_guests_language_survives_a_change_of_stay(db):
    """
    The one thing on this column that is about the person rather than the trip.
    Losing it is the bug Phase 2 exists to have fixed.
    """
    session.merge(CONVERSATION, {"check_in": "2026-10-20", "check_out": "2026-10-22"})
    assert profile(db)["language"] == "hinglish"


def test_correcting_the_party_size_is_not_a_new_stay(db):
    """
    "Actually make it three of us" must not throw away the quote they are three
    messages into. Dates are the signal, because dates are what a guest always
    restates when they mean a different trip.
    """
    session.merge(CONVERSATION, {"adults": 3})

    now = profile(db)
    assert now["adults"] == 3
    assert now["quote_id"] == "q-september"
    assert now["check_in"] == "2026-09-10"


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
