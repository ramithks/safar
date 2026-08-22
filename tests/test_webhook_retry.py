"""
Razorpay's retry has to be able to reach us.

The whole recovery story for "the guest paid and something went wrong in the
seconds afterwards" is three moving parts:

    payments.release()     puts the order back to 'created'
    the handler 500s       so Razorpay comes back — it retries for 24 hours
    webhook_log.claim()    lets the retry through

The third one did not work. `razorpay_webhook_events.status` defaults to
`'processed'` at insert, Razorpay's retry carries **the same**
`x-razorpay-event-id`, and `claim` answered False — so the retry was told
"already seen", answered 200, and the released order sat at 'created' with a
failure_reason and nobody ever came back for it. A guest charged, with no
booking, permanently, and a 200 in the log saying everything was fine.

These tests are about that one property: **a released event is claimable again,
and a processed one never is.**
"""
import asyncio

import pytest

from app.services.pms import webhook_log
from tests.fake_supabase import FakeDb

EVENT = "evt_QxK9mP2nL4"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    # The real index. Without it this fake accepts the second insert and the
    # test proves nothing — which is exactly how the bug survived.
    fake.unique = {"razorpay_webhook_events": (("event_id",),)}
    monkeypatch.setattr("app.services.pms.webhook_log.get_db", lambda: fake)
    monkeypatch.setattr(
        "app.db.client.run_db",
        lambda fn, *a: asyncio.get_event_loop().run_in_executor(None, fn, *a),
    )
    return fake


def claim(event_id=EVENT, name="payment.captured"):
    return run(webhook_log.claim(event_id, name, {"event": name}, order_id="order_1"))


# ── the fix ─────────────────────────────────────────────────────────────────

def test_a_redelivery_of_a_finished_event_is_still_refused(db):
    """The property that was already right, and must stay right."""
    assert claim() is True
    run(webhook_log.mark(EVENT, "processed"))

    assert claim() is False
    assert claim() is False
    assert len(db.rows("razorpay_webhook_events")) == 1


def test_a_released_event_is_handed_to_the_next_retry(db):
    """
    The bug, as a test.

    Before this, the second `claim` returned False and the retry was answered
    "already seen" — so `payments.release` had nothing to release *to*.
    """
    assert claim() is True

    # Fulfilment failed. The handler releases both halves and 500s.
    run(webhook_log.release(EVENT, "room sold in the seconds between"))

    assert claim() is True, "Razorpay's retry must be allowed to do the work"


def test_releasing_leaves_the_reconciliation_record_intact(db):
    """
    Marked, never deleted.

    This table is the only record of what Razorpay actually sent; by the time
    anybody needs it the event is gone from Razorpay's dashboard. Making a
    retry possible by dropping the row would trade the bug for a blind spot.
    """
    claim()
    run(webhook_log.release(EVENT, "database blinked"))

    rows = db.rows("razorpay_webhook_events")
    assert len(rows) == 1
    assert rows[0]["event_id"] == EVENT
    assert rows[0]["payload"] == {"event": "payment.captured"}
    assert "database blinked" in rows[0]["error"]


def test_only_one_of_two_simultaneous_retries_gets_the_work(db):
    """
    Razorpay redelivers more than once, and sometimes at the same moment.

    The reclaim is a conditional UPDATE for the same reason `payments._claim`
    is: Postgres decides which caller wins, rather than us checking and then
    hoping. Two retries against one released event must produce one handler.
    """
    claim()
    run(webhook_log.release(EVENT, "transient"))

    outcomes = [claim(), claim(), claim()]

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 2


def test_a_released_event_that_then_succeeds_is_closed_for_good(db):
    """One recovery, not an open door."""
    claim()
    run(webhook_log.release(EVENT, "transient"))
    assert claim() is True
    run(webhook_log.mark(EVENT, "processed"))

    assert claim() is False


# ── the surrounding rules, unchanged ────────────────────────────────────────

def test_an_event_with_no_id_is_always_handled(db):
    """
    Razorpay sends `x-razorpay-event-id` on everything current, but a missing
    header must mean "process it". The downstream keys are the real protection,
    and silently discarding money-bearing events because a header changed shape
    is the failure nobody would find.
    """
    assert run(webhook_log.claim(None, "payment.captured", {})) is True
    assert run(webhook_log.claim(None, "payment.captured", {})) is True
    assert db.rows("razorpay_webhook_events") == []

    # And releasing one is a no-op rather than an error.
    run(webhook_log.release(None, "nothing to release"))


def test_a_logging_failure_never_stops_an_event_being_handled(db, monkeypatch):
    """
    Handling twice is safe by construction; not handling at all is not. A
    dropped connection to the audit table must not cost a hotel its booking.
    """
    def explode(*_a, **_k):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(
        "app.services.pms.webhook_log.get_db",
        lambda: type("Boom", (), {"table": staticmethod(explode)})(),
    )
    assert claim() is True


def test_two_different_events_do_not_collide(db):
    assert claim("evt_one") is True
    assert claim("evt_two") is True
    assert len(db.rows("razorpay_webhook_events")) == 2
