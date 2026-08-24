"""
Undoing a booking: the rooms come back, and the money comes back by the policy.

**The gap this closes.** The readiness audit found no cancellation anywhere in
the system. The AI could sell a room in ninety seconds and a guest had to reach
a human to undo it — and neither of the two things that happen when a booking
goes away had any code path: the inventory coming back, and the money.

Of those two, the inventory is the one that costs the hotel silently. A room
cancelled and never released is a room that cannot be sold again, and nothing
anywhere reports it. That is the same failure the reconciliation tool had to
repair after test cleanup wrote `status = 'cancelled'` with raw SQL and left 377
phantom room-nights sold at Casa Mandala — the identical bug, arrived at from the
other direction.

**The refund is arithmetic and the model does not do arithmetic.** §24 rule 3.
Every figure below comes from `policies.refund_for`, which already existed for
`get_cancellation_terms` — the tool that *quotes* a policy — applied now to a
booking that is actually going away. The agent's whole part is to identify the
booking and read the answer out.

§14's list, each one a test:

    free cancellation · partial refund · non-refundable · before the deadline
    after the deadline · a booking the hotel cannot cancel · already cancelled
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.services.pms import cancellation
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RESERVATION = "a0a0a0a0-0001-4001-8001-000000000001"
ROOM_TYPE = "22222222-2222-4222-8222-222222222222"
RATE_PLAN = "11111111-1111-4111-8111-111111111111"

# Arrival is a week out and the clock is fixed, so "48 hours before" means the
# same thing on every run. A test whose refund depends on when it is run is a
# test that goes red on a Tuesday.
NOW = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
CHECK_IN = date(2026, 9, 10)


def run(coro):
    return asyncio.run(coro)


def make_db(monkeypatch, *, status: str = "confirmed", total: int = 1000000,
            paid: int = 1000000, refundable: bool = True,
            policy: dict | None = None, units: int = 2) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "check_in_time": "14:00",
        "timezone": "Asia/Kolkata", "policies": {},
    })
    fake.seed("reservations", {
        "id": RESERVATION, "ref": "TB-CN0001", "property_id": PROPERTY,
        "partner_id": PARTNER, "room_type_id": ROOM_TYPE, "rate_plan_id": RATE_PLAN,
        "check_in": str(CHECK_IN), "check_out": str(CHECK_IN + timedelta(days=2)),
        "nights": 2, "units": units, "adults": 2, "children": 0,
        "total_paise": total, "paid_paise": paid, "gross_paise": total,
        "discount_paise": 0, "tax_paise": 0,
        "status": status, "payment_status": "paid" if paid >= total else "partially_paid",
        "guest_name": "Nisha Kapoor", "cancellation_policy": None,
    })
    fake.seed("rate_plans", {
        "id": RATE_PLAN, "property_id": PROPERTY, "name": "Flexible",
        "refundable": refundable, "cancellation_cutoff_hours": 48,
        "cancellation_policy": policy,
    })
    monkeypatch.setattr("app.services.pms.cancellation.get_db", lambda: fake)
    return fake


def reservation(db) -> dict:
    return db.rows("reservations")[0]


# ── the money, case by case ─────────────────────────────────────────────────

def test_free_cancellation_before_the_deadline(monkeypatch):
    """
    A refundable rate, cancelled a week out. Everything comes back — and
    `kind` says so in a word, so nothing downstream has to infer "free" from a
    percentage.
    """
    db = make_db(monkeypatch)
    out = run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW, reason="plans changed"))

    assert out["refundPct"] == 100
    assert out["refundPaise"] == 1000000
    assert out["retainedPaise"] == 0
    assert out["kind"] == "free"
    assert out["afterDeadline"] is False


def test_nothing_comes_back_after_the_deadline(monkeypatch):
    """
    The same booking, cancelled the morning of arrival. The cutoff is 48 hours
    and four hours remain, so the ladder's bottom rung applies.

    The hours are counted to the *arrival moment* — check-in date plus the
    hotel's 2pm — and not to midnight. Counting to midnight would hand away ten
    hours of every deadline a hotel sets.
    """
    db = make_db(monkeypatch)
    morning_of = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    out = run(cancellation.cancel(PROPERTY, RESERVATION, at=morning_of))

    assert out["refundPct"] == 0
    assert out["refundPaise"] == 0
    assert out["retainedPaise"] == 1000000
    assert out["kind"] == "non_refundable"
    assert out["afterDeadline"] is True


def test_a_non_refundable_rate_refunds_nothing_even_a_month_out(monkeypatch):
    """
    §14: non-refundable. The date is irrelevant — that is the whole deal the
    guest took in exchange for the price, and an agent that softens it into a
    maybe has mis-sold the rate.
    """
    db = make_db(monkeypatch, refundable=False)
    out = run(cancellation.cancel(PROPERTY, RESERVATION,
                                  at=NOW - timedelta(days=30)))

    assert out["refundPct"] == 0
    assert out["refundPaise"] == 0
    assert out["kind"] == "non_refundable"


def test_a_staged_policy_gives_a_partial_refund(monkeypatch):
    """
    §14: partial refund. The hotel's own ladder — full a week out, half inside
    72 hours, nothing inside 24 — read from `cancellation_policy` rather than
    from the two fallback columns.
    """
    db = make_db(monkeypatch, policy={"tiers": [
        {"hoursBefore": 168, "refundPct": 100},
        {"hoursBefore": 72, "refundPct": 50},
        {"hoursBefore": 24, "refundPct": 0},
    ]})
    # Four days out: 100 hours to the 2pm arrival, which is inside the 168-hour
    # rung and outside the 72-hour one. The ladder reads "with at least X hours
    # left you get Y%", so this is the 50 band.
    four_days_out = datetime(2026, 9, 6, 10, 0, tzinfo=timezone.utc)
    out = run(cancellation.cancel(PROPERTY, RESERVATION, at=four_days_out))

    assert out["refundPct"] == 50
    assert out["refundPaise"] == 500000
    assert out["retainedPaise"] == 500000
    assert out["kind"] == "partial"


def test_a_refund_never_exceeds_what_the_guest_actually_paid(monkeypatch):
    """
    **The two-number rule.** The policy says what percentage of the *stay* comes
    back; the ledger says what the guest handed over. A hotel that took a 30%
    deposit on a fully refundable rate owes the deposit back and not the whole
    stay — and a function that returned the policy figure alone would have
    promised three times the money.
    """
    db = make_db(monkeypatch, total=1000000, paid=300000)
    out = run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert out["refundPct"] == 100
    assert out["refundPaise"] == 300000, "only what was paid may come back"
    assert out["paidPaise"] == 300000
    assert out["totalPaise"] == 1000000


def test_a_no_show_is_priced_by_its_own_rule(monkeypatch):
    db = make_db(monkeypatch, policy={"tiers": [{"hoursBefore": 24, "refundPct": 100}],
                                      "noShowPct": 20})
    out = run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW, no_show=True))

    assert out["refundPct"] == 20
    assert out["refundPaise"] == 200000


# ── the rooms ───────────────────────────────────────────────────────────────

def test_the_rooms_go_back_into_inventory(monkeypatch):
    """
    **The assertion this whole module exists for.** A cancelled booking that
    never releases its stock is a room the hotel cannot sell again, invisibly,
    for as many nights as the stay was — and nothing anywhere reports it.

    Asserted on the RPC call rather than on a row, because `release_booking` is
    the only thing allowed to move inventory and the failure being prevented is
    precisely somebody writing the status change without it.
    """
    db = make_db(monkeypatch, units=2)
    run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    released = [c for c in db.rpc_calls if c[0] == "release_booking"]
    assert len(released) == 1, "inventory was not released exactly once"
    assert released[0][1] == {
        "p_room_type": ROOM_TYPE,
        "p_check_in": str(CHECK_IN),
        "p_check_out": str(CHECK_IN + timedelta(days=2)),
        "p_units": 2,
    }


def test_inventory_comes_back_before_the_status_changes(monkeypatch):
    """
    The order is deliberate and a crash decides which half survives.

    Stock released against a booking still marked confirmed reads as an oversell
    risk on one room and the night audit catches it. A cancelled booking still
    holding stock is invisible and costs the hotel a room a night until somebody
    runs a reconciliation. One of those is loud; that is the one to leave behind.
    """
    db = make_db(monkeypatch)
    seen: list[str] = []
    original = db.rpc

    def watched(name, params=None):
        # The status *at the moment inventory moved*. Asserting on the value
        # rather than on a call ordering, because the read that loads the
        # reservation touches the same table and would make an ordering
        # assertion pass or fail for the wrong reason.
        seen.append(reservation(db)["status"])
        return original(name, params)

    monkeypatch.setattr(db, "rpc", watched)
    run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert seen == ["confirmed"], (
        "the reservation was already marked cancelled when the rooms were "
        "released, so a crash between the two would have left stock sold "
        "against a booking nobody can find"
    )
    assert reservation(db)["status"] == "cancelled"


def test_a_pending_booking_releases_nothing(monkeypatch):
    """
    A `pending` reservation never called `book_inventory`, so releasing against
    it would hand the hotel stock it never sold — inventory *above* what the
    property has, which is how an oversell is manufactured.
    """
    db = make_db(monkeypatch, status="pending")
    run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert [c for c in db.rpc_calls if c[0] == "release_booking"] == []
    assert reservation(db)["status"] == "cancelled"


# ── the record ──────────────────────────────────────────────────────────────

def test_the_booking_records_that_it_was_cancelled_and_why(monkeypatch):
    db = make_db(monkeypatch)
    run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW,
                            reason="flight got cancelled"))

    row = reservation(db)
    assert row["status"] == "cancelled"
    assert row["cancelled_at"]
    assert row["cancellation_reason"] == "flight got cancelled"
    assert row["payment_status"] == "refunded"


def test_the_refund_is_a_line_on_the_folio(monkeypatch):
    """
    §14: "create refund/ledger state". The folio is where a hotel's money lives,
    and a refund that exists only as a status on a reservation is a refund the
    night audit cannot see.
    """
    db = make_db(monkeypatch)
    run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    entries = db.rows("folio_entries")
    assert len(entries) == 1
    assert entries[0]["kind"] == "refund"
    assert entries[0]["amount_paise"] == 1000000
    assert "TB-CN0001" in entries[0]["description"]
    # And the reservation's own running total agrees with it.
    assert reservation(db)["paid_paise"] == 0


def test_a_zero_refund_posts_no_folio_line(monkeypatch):
    """
    A non-refundable cancellation moves no money. A ₹0 folio entry is a row that
    says nothing, and `post_folio_entry` refuses one anyway.
    """
    db = make_db(monkeypatch, refundable=False)
    run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert db.rows("folio_entries") == []
    assert reservation(db)["paid_paise"] == 1000000


def test_the_money_is_recorded_as_owed_and_not_as_sent(monkeypatch):
    """
    Deliberate, not unfinished. An automated outbound refund is a payment the
    hotel did not authorise at the moment it happened, and the difference
    between "₹10,000 is owed to this guest" and "₹10,000 has left the hotel's
    account" is a difference a hotelier is entitled to make themselves.
    """
    db = make_db(monkeypatch)
    out = run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert out["refundDue"] is True
    assert out["refundMethod"] == "hotel_processes_manually"


# ── asking twice, and asking about the wrong thing ──────────────────────────

def test_cancelling_twice_releases_the_rooms_once(monkeypatch):
    """
    **The idempotency that matters.** "Cancel kar do" followed thirty seconds
    later by "cancel hua?" is two attempts, and releasing the same rooms twice
    puts inventory *above* what the hotel has — the same corruption, from the
    other end, that §13's phantom room-nights came from.
    """
    db = make_db(monkeypatch)
    first = run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))
    second = run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert len([c for c in db.rpc_calls if c[0] == "release_booking"]) == 1
    assert len(db.rows("folio_entries")) == 1
    assert first["alreadyCancelled"] is False
    assert second["alreadyCancelled"] is True
    assert second["inventoryReleased"] is False


@pytest.mark.parametrize("status,fragment", [
    ("checked_in", "arrived"),
    ("checked_out", "finished"),
    ("no_show", "no-show"),
])
def test_a_booking_the_agent_must_not_cancel(monkeypatch, status, fragment):
    """
    §14: a guest who has arrived is a departure, an early checkout or a dispute
    — three things with different money and all of them a person's decision.
    Refused with the real reason, so the agent can say something true instead of
    apologising for something it cannot name.
    """
    db = make_db(monkeypatch, status=status)
    with pytest.raises(HTTPException) as raised:
        run(cancellation.cancel(PROPERTY, RESERVATION, at=NOW))

    assert raised.value.status_code == 409
    assert fragment in raised.value.detail
    assert reservation(db)["status"] == status, "nothing may have changed"
    assert db.rpc_calls == []


def test_an_unknown_booking_is_a_404_and_not_a_guess(monkeypatch):
    db = make_db(monkeypatch)
    with pytest.raises(HTTPException) as raised:
        run(cancellation.cancel(PROPERTY, "b0b0b0b0-9999-4999-8999-999999999999", at=NOW))
    assert raised.value.status_code == 404


def test_a_booking_at_another_property_is_invisible(monkeypatch):
    """
    Tenant isolation, at the one function that both releases stock and moves
    money. Scoped on `property_id` in the read, so there is no path that finds
    the row and then decides.
    """
    db = make_db(monkeypatch)
    with pytest.raises(HTTPException) as raised:
        run(cancellation.cancel("dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                                RESERVATION, at=NOW))
    assert raised.value.status_code == 404
    assert reservation(db)["status"] == "confirmed"


# ── the preview ─────────────────────────────────────────────────────────────

def test_a_preview_changes_nothing(monkeypatch):
    """
    "Cancel karne pe kitna wapas milega?" is a question, and answering it must
    not answer it by doing it. Merging the two would mean the only way to learn
    the refund was to lose the booking.
    """
    db = make_db(monkeypatch)
    out = run(cancellation.preview(PROPERTY, RESERVATION, at=NOW))

    assert out["refundPaise"] == 1000000
    assert out["cancellable"] is True
    assert reservation(db)["status"] == "confirmed"
    assert db.rpc_calls == []
    assert db.rows("folio_entries") == []


def test_the_preview_and_the_cancellation_agree(monkeypatch):
    """
    One computation, so the number a guest is quoted and the number they are
    given cannot drift. A hotel whose AI quotes ₹5,000 and refunds ₹2,500 has a
    complaint, not a rounding difference.
    """
    db = make_db(monkeypatch, policy={"tiers": [
        {"hoursBefore": 168, "refundPct": 100},
        {"hoursBefore": 72, "refundPct": 40},
    ]})
    at = datetime(2026, 9, 8, 10, 0, tzinfo=timezone.utc)

    quoted = run(cancellation.preview(PROPERTY, RESERVATION, at=at))
    done = run(cancellation.cancel(PROPERTY, RESERVATION, at=at))

    assert quoted["refundPaise"] == done["refundPaise"]
    assert quoted["refundPct"] == done["refundPct"]


def test_the_preview_names_why_a_booking_cannot_be_cancelled(monkeypatch):
    db = make_db(monkeypatch, status="checked_in")
    out = run(cancellation.preview(PROPERTY, RESERVATION, at=NOW))

    assert out["cancellable"] is False
    assert out["blockedReason"] == "already_checked_in"
    assert "arrived" in out["blockedExplanation"]


def test_the_policy_the_booking_was_sold_on_wins(monkeypatch):
    """
    A booking is governed by the terms in force the day it was made, not by the
    terms the hotel switched to last week. The snapshot on the reservation beats
    the rate plan's current policy — otherwise a hotel tightening its policy
    would retroactively reduce refunds it had already promised.
    """
    db = make_db(monkeypatch, refundable=True)
    db.rows("reservations")[0]["cancellation_policy"] = {
        "tiers": [{"hoursBefore": 0, "refundPct": 0}], "refundable": False,
    }
    out = run(cancellation.preview(PROPERTY, RESERVATION, at=NOW))

    assert out["refundPct"] == 0, "the plan's current terms overrode the snapshot"
