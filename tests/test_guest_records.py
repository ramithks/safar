"""
The person, as opposed to the stay.

`guests` has held `stays_count`, `total_spend_paise` and `last_stay_at` since
the schema was written, and until now **nothing ever wrote any of them** —
nor `reservations.guest_id`. Every consequence of that was silent:

  * the owner's guest list never contained anybody who had actually booked
  * `get_guest` never matched a returning guest, so the AI employee greeted a
    fourth-time visitor as a stranger
  * `repeat_guest` reads `stays_count >= 2` and could not match a single row,
    so the loyalty segment was permanently empty
  * `post_stay` reads `reservations.guest_id` on checked-out stays, which was
    always null — so post-stay messaging had no audience, ever

None of it threw. The queries were correct and the answer was always nobody.
These tests exist so the answer cannot go back to nobody.
"""
import pytest

from app.services.pms import booking, frontdesk


class Recorder:
    """A stand-in for the PostgREST builder that records what it was asked."""

    def __init__(self, rows_by_table=None):
        self.rows = rows_by_table or {}
        self.inserted: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, dict]] = []
        self._table = None
        self._pending = None

    def table(self, name):
        self._table = name
        return self

    def select(self, *_a, **_k):
        self._pending = ("select", None)
        return self

    def insert(self, row):
        self.inserted.append((self._table, row))
        self._pending = ("insert", [{**row, "id": f"new-{self._table}"}])
        return self

    def update(self, patch):
        self.updated.append((self._table, patch))
        self._pending = ("update", [patch])
        return self

    def eq(self, *_a, **_k):
        return self

    def ilike(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        kind, payload = self._pending or ("select", None)
        data = payload if payload is not None else self.rows.get(self._table, [])
        return type("R", (), {"data": data})()


# ── matching a person to a booking ───────────────────────────────────────────

def test_a_new_guest_gets_a_record():
    db = Recorder({"guests": []})
    guest_id = booking._resolve_guest(
        db, "prop-1", "part-1", "Anita Rao", "+91 98765 43210", "a@example.com",
    )
    assert guest_id == "new-guests"
    table, row = db.inserted[0]
    assert table == "guests"
    assert row["property_id"] == "prop-1"
    assert row["name"] == "Anita Rao"


def test_a_new_guest_has_not_stayed_yet():
    """
    A booking is an intention. Counting it as a stay would make a guest who
    cancels twice look like a regular.
    """
    db = Recorder({"guests": []})
    booking._resolve_guest(db, "prop-1", "part-1", "Anita", "9876543210", None)
    row = db.inserted[0][1]
    assert "stays_count" not in row
    assert "total_spend_paise" not in row


def test_a_returning_guest_is_matched_not_duplicated():
    db = Recorder({"guests": [{"id": "guest-7", "stays_count": 3}]})
    guest_id = booking._resolve_guest(
        db, "prop-1", "part-1", "Anita Rao", "9876543210", None,
    )
    assert guest_id == "guest-7"
    assert db.inserted == [], "a returning guest must not get a second record"


@pytest.mark.parametrize("written", ["+919876543210", "9876543210", "09876543210",
                                     "+91 98765 43210", "919876543210"])
def test_one_person_however_their_number_is_written(written, monkeypatch):
    """
    A guest is `+91…` on WhatsApp, ten digits at the desk and `0…` on an OTA.
    Matching on the last ten digits is what makes those one person rather than
    three records with three stay counts.
    """
    seen = {}

    class Capturing(Recorder):
        def ilike(self, column, pattern):
            seen[column] = pattern
            return self

    db = Capturing({"guests": [{"id": "guest-7", "stays_count": 1}]})
    booking._resolve_guest(db, "prop-1", "part-1", "Anita", written, None)
    assert seen.get("phone") == "%9876543210"


def test_email_is_the_fallback_when_there_is_no_phone():
    seen = {}

    class Capturing(Recorder):
        def ilike(self, column, pattern):
            seen[column] = pattern
            return self

    db = Capturing({"guests": [{"id": "guest-9", "stays_count": 0}]})
    booking._resolve_guest(db, "prop-1", "part-1", "Anita", None, "a@example.com")
    assert seen.get("email") == "a@example.com"


def test_a_broken_guest_table_still_lets_the_booking_through():
    """
    The guest is standing at the desk. Failing their booking because a CRM row
    could not be written would be the wrong thing to protect.
    """
    class Broken(Recorder):
        def execute(self):
            raise RuntimeError("guests is unavailable")

    assert booking._resolve_guest(
        Broken(), "prop-1", "part-1", "Anita", "9876543210", None,
    ) is None


# ── counting the stay ────────────────────────────────────────────────────────

def test_checking_out_counts_the_stay():
    db = Recorder({"guests": [{"stays_count": 2, "total_spend_paise": 500_000}]})
    frontdesk._count_the_stay(db, "prop-1", {
        "guest_id": "guest-7", "paid_paise": 120_000, "check_out": "2026-09-03",
    })
    table, patch = db.updated[0]
    assert table == "guests"
    assert patch["stays_count"] == 3
    assert patch["total_spend_paise"] == 620_000
    assert patch["last_stay_at"] == "2026-09-03"


def test_spend_is_what_they_paid_not_what_it_was_priced_at():
    """A comped night is not spend."""
    db = Recorder({"guests": [{"stays_count": 0, "total_spend_paise": 0}]})
    frontdesk._count_the_stay(db, "prop-1", {
        "guest_id": "g", "paid_paise": 0, "total_paise": 900_000,
        "check_out": "2026-09-03",
    })
    assert db.updated[0][1]["total_spend_paise"] == 0


def test_a_stay_with_no_guest_record_counts_nothing():
    db = Recorder({"guests": []})
    frontdesk._count_the_stay(db, "prop-1", {"guest_id": None, "paid_paise": 100})
    assert db.updated == []


def test_counting_never_strands_a_room():
    """
    The guest has left. A statistic that cannot be written must not turn their
    check-out into a 500 and leave the room showing occupied.
    """
    class Broken(Recorder):
        def execute(self):
            raise RuntimeError("guests is unavailable")

    frontdesk._count_the_stay(Broken(), "prop-1", {
        "guest_id": "g", "paid_paise": 100, "check_out": "2026-09-03",
    })  # must not raise


def test_the_loyalty_segments_read_what_check_out_now_writes():
    """
    Ties the two halves together: the columns the marketing segments filter on
    are exactly the columns a departure writes. If somebody renames one, this
    fails rather than the audience quietly returning nobody.
    """
    import inspect

    from app.services.marketing import audience

    source = inspect.getsource(audience)
    assert "stays_count" in source
    assert "guest_id" in source

    written = inspect.getsource(frontdesk._count_the_stay)
    for column in ("stays_count", "total_spend_paise", "last_stay_at"):
        assert column in written, f"{column} is read by marketing and never written"
