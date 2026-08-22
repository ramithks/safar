"""
Two deliveries of one Instagram message, arriving together.

`record_inbound` reads before it writes, and that select closes the window for
redeliveries seconds apart — which is almost all of them. It cannot close the
window for two in the same millisecond: both pass the select, both insert, and
`conversation_messages_provider_idx` (UNIQUE on `channel, external_id`) refuses
the second.

That was a 500 back to Meta. Meta redelivers, the redelivery reads as a
duplicate, the guest is answered — so the design *worked*, and left an exception
in the log for something it handles. And a webhook body carries several events:
one racing message took the acknowledgement of every other message in the same
request with it.

**The index was always the real idempotency.** These tests are about reading its
answer rather than crashing on it — so both deliveries produce one message, one
conversation, one agent run and one reply, whichever order they land in.
"""
import asyncio

import pytest

from app.services.messaging import service as messaging
from app.services.messaging.channels import InboundMessage
from tests.fake_supabase import FakeDb

PROPERTY = "p0p0p0p0-0001-4001-8001-000000000001"
PARTNER = "b0b0b0b0-0001-4001-8001-000000000001"
IGSID = "1376629097955726"
MID = "aWc6MTIzNDU2Nzg5"

CONNECTION = {
    "id": "conn-1", "property_id": PROPERTY, "partner_id": PARTNER,
    "instagram_user_id": "17841400000000000", "channel": "instagram",
}


def message(mid: str = MID, body: str = "payment hogyi") -> InboundMessage:
    return InboundMessage(
        channel="instagram", external_id=mid, sender_external_id=IGSID,
        account_id=CONNECTION["instagram_user_id"], message_type="text",
        body=body, media_urls=(), sent_at=None, raw={"mid": mid},
    )


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    # The real index. Without it this fake takes the second insert happily and
    # the tests below prove nothing at all.
    fake.unique = {"conversation_messages": (("channel", "external_id"),)}
    fake.seed("conversations", {
        "id": "conv-1", "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "instagram", "external_id": IGSID, "handler": "ai",
        "status": "open", "unread_count": 0,
    })
    for module in ("app.services.messaging.service",):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake)
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    return fake


def record(inbound=None):
    return asyncio.run(messaging.record_inbound(inbound or message(), CONNECTION))


# ── the ordinary case, unchanged ────────────────────────────────────────────

def test_the_first_delivery_is_recorded(db):
    recorded = record()
    assert recorded.duplicate is False
    assert len(db.rows("conversation_messages")) == 1


def test_a_redelivery_seconds_later_is_caught_by_the_select(db):
    record()
    again = record()

    assert again.duplicate is True
    assert len(db.rows("conversation_messages")) == 1


# ── the race ────────────────────────────────────────────────────────────────

def test_a_delivery_that_loses_the_index_race_reports_a_duplicate(db, monkeypatch):
    """
    The bug. Both callers pass the select — simulated by making the select
    return nothing the first time each of them asks — and the index decides.
    """
    record()   # the winner's row is already there

    # Now force the losing caller's pre-check to miss it, which is exactly what
    # happens when the winner has not committed yet.
    real_table = db.table
    state = {"blinded": True}

    def blinded(name):
        query = real_table(name)
        if name == "conversation_messages" and state["blinded"]:
            state["blinded"] = False
            original = query.execute

            def once():
                original()
                return type("R", (), {"data": []})()
            query.execute = once
        return query

    monkeypatch.setattr("app.services.messaging.service.get_db",
                        lambda: type("D", (), {"table": staticmethod(blinded),
                                               "rpc": db.rpc})())

    recorded = record()

    assert recorded.duplicate is True, "the loser must report a duplicate, not raise"
    assert recorded.message["external_id"] == MID
    assert len(db.rows("conversation_messages")) == 1, "one message, not two"


def test_the_window_is_not_reopened_by_the_loser(db, monkeypatch):
    """
    Why the duplicate path returns before the update rather than after it.

    `last_inbound_at` is the 24-hour service window. A losing caller that fell
    through and stamped it would extend a window on a message already counted —
    and on Instagram that window is the difference between being allowed to
    answer and not.
    """
    record()
    stamped = db.rows("conversations")[0].get("last_inbound_at")
    unread = db.rows("conversations")[0].get("unread_count")

    record()

    assert db.rows("conversations")[0].get("last_inbound_at") == stamped
    assert db.rows("conversations")[0].get("unread_count") == unread


def test_a_real_failure_still_raises(db, monkeypatch):
    """
    Only 23505 is swallowed. A dropped connection must not be reported as "we
    already had that message", which would lose it silently — the one outcome a
    hotel cannot explain.
    """
    real_table = db.table

    def exploding(name):
        query = real_table(name)
        if name == "conversation_messages":
            def boom(*_a, **_k):
                raise RuntimeError("connection reset by peer")
            query.insert = boom
        return query

    monkeypatch.setattr("app.services.messaging.service.get_db",
                        lambda: type("D", (), {"table": staticmethod(exploding),
                                               "rpc": db.rpc})())

    with pytest.raises(RuntimeError):
        record()


def test_five_deliveries_of_one_message_produce_one_message(db):
    """
    §16, as a number. Meta redelivers on a timeout, on a non-2xx and sometimes
    on neither.
    """
    outcomes = [record() for _ in range(5)]

    assert sum(1 for r in outcomes if not r.duplicate) == 1
    assert sum(1 for r in outcomes if r.duplicate) == 4
    assert len(db.rows("conversation_messages")) == 1


def test_two_different_messages_are_both_recorded(db):
    """The rule must not turn a fast-typing guest into one message."""
    record(message("mid-one", "hi"))
    record(message("mid-two", "kitna hoga?"))

    assert len(db.rows("conversation_messages")) == 2
    assert db.rows("conversations")[0]["unread_count"] == 2
