"""
The guest nobody came back to, and the hotel finding out.

Three code paths park a conversation with a human — the handoff tool, a turn
that failed, and a conversation locked too long — and until the sweep existed,
none of them told anybody. Two of the three first promise the guest that
somebody is coming, which is what makes the silence worse than no answer: the
hotel has committed to a person who does not know they were volunteered.

What is pinned here is mostly the *negative* side. A sweep that raises tickets
too eagerly is one a hotel mutes, and a muted sweep is the same product as no
sweep at all — so the tests that matter are the ones where nobody is waiting.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services.pms import escalation, waiting
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NOW = datetime(2026, 9, 12, 18, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def conversation(**overrides) -> dict:
    row = {
        "id": "c0c0c0c0-0001-4001-8001-000000000001",
        "property_id": PROPERTY,
        "partner_id": PARTNER,
        "guest_id": None,
        "channel": "instagram",
        "handler": "ai",
        "status": "open",
        "last_inbound_at": at(20),
        "last_message_at": at(20),
        "last_message": "Deluxe Balcony wala book kardo",
        "guest_profile": {},
        "ai_enabled": True,
    }
    row.update(overrides)
    return row


# ── who is actually waiting ──────────────────────────────────────────────────


def test_nothing_sent_since_the_guest_wrote_is_waiting():
    assert waiting.reason_waiting(conversation()) == "nobody has replied"


def test_the_ai_promising_a_person_who_never_came_is_waiting():
    """
    The case every cheap test calls answered.

    There *is* an outbound message newer than the guest's — "someone from the
    hotel will be with you shortly" — so a timestamp comparison says this
    thread was handled. The promise is the outstanding thing, and only
    `handler` records that it was made.
    """
    row = conversation(handler="human_requested", last_message_at=at(19))
    assert waiting.reason_waiting(row) == (
        "the AI handed this to a person and nobody has taken it"
    )


def test_an_answered_conversation_is_not_waiting():
    assert waiting.reason_waiting(conversation(last_message_at=at(19))) is None


def test_a_conversation_a_member_has_taken_over_is_not_waiting():
    """
    `human` and `human_requested` are different states and the difference is the
    whole point: somebody has this one. Escalating it would tell a manager about
    a guest their own colleague is mid-sentence with.
    """
    row = conversation(handler="human", last_message_at=at(19))
    assert waiting.reason_waiting(row) is None


def test_a_conversation_with_no_inbound_message_is_not_waiting():
    assert waiting.reason_waiting(conversation(last_inbound_at=None)) is None


# ── the sweep ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    for target in ("app.db.client", "app.services.pms.waiting",
                   "app.services.pms.escalation"):
        monkeypatch.setattr(f"{target}.get_db", lambda: fake)
    fake.seed("properties", {"id": PROPERTY, "partner_id": PARTNER,
                             "name": "Casa Mandala"})
    fake.seed("hotel_team_members", {
        "id": "11111111-1111-4111-8111-111111111111", "property_id": PROPERTY,
        "role": "front_desk", "name": "Priya", "whatsapp": "919812345678",
        "is_active": True, "sort_order": 1,
    })
    return fake


@pytest.fixture
def sent(monkeypatch) -> list:
    """Every WhatsApp the sweep causes, without a provider."""
    out: list = []

    async def _notify(property_id, recipients, message, escalation_id):
        out.append(message)
        return True

    monkeypatch.setattr(escalation, "_notify", _notify)
    return out


def test_a_waiting_guest_reaches_the_hotels_phone(db, sent):
    db.seed("conversations", conversation(handler="human_requested",
                                          last_message_at=at(19)))

    result = run(waiting.sweep(now=NOW))

    assert result == {"examined": 1, "waiting": 1, "raised": 1,
                      "notified": 1, "demo": 0}
    assert len(sent) == 1
    assert "A guest is waiting" in sent[0]
    assert "Casa Mandala" in sent[0]
    # The guest's own words, so the person picking this up can answer from the
    # notification without opening anything.
    assert "Deluxe Balcony wala book kardo" in sent[0]
    assert db.rows("agent_escalations")[0]["status"] == "notified"


def test_a_message_still_in_flight_is_not_a_wait(db, sent):
    """
    The grace period, which is the difference between a sweep and a nuisance.

    A turn is a classifier call, several tool calls and a model that sometimes
    takes ninety seconds. A guest who wrote four minutes ago is being answered.
    """
    db.seed("conversations", conversation(last_inbound_at=at(4),
                                          last_message_at=at(4)))

    assert run(waiting.sweep(now=NOW)) == {
        "examined": 0, "waiting": 0, "raised": 0, "notified": 0, "demo": 0,
    }
    assert sent == []


def test_yesterdays_silence_is_not_swept(db, sent):
    """
    A guest who wrote fourteen hours ago is not waiting; they have gone. A
    WhatsApp about them the next morning teaches the hotel that these messages
    are history rather than something to act on — and the first run after a
    deploy would otherwise raise one for every unanswered message the inbox has
    ever held.
    """
    db.seed("conversations", conversation(last_inbound_at=at(60 * 14),
                                          last_message_at=at(60 * 14)))

    assert run(waiting.sweep(now=NOW))["waiting"] == 0
    assert sent == []


def test_the_same_waiting_guest_is_raised_once(db, sent):
    """
    §21's *do not repeatedly escalate the same issue*, on a task that runs every
    five minutes. The second sweep finds the same silence and must not send a
    second WhatsApp about it.
    """
    db.seed("conversations", conversation(handler="human_requested",
                                          last_message_at=at(19)))

    first = run(waiting.sweep(now=NOW))
    second = run(waiting.sweep(now=NOW + timedelta(minutes=5)))

    assert first["raised"] == 1
    assert second["waiting"] == 1 and second["raised"] == 0
    assert len(sent) == 1
    assert len(db.rows("agent_escalations")) == 1


def test_the_dates_arriving_late_do_not_raise_a_second_ticket(db, sent):
    """
    The dedupe key for every other kind is the *substance* — the room, the
    dates, the figure — because two arguments about different stays are two
    escalations. Here the substance is the conversation. A guest who is being
    ignored and then finally gives their dates is the same silent thread, and
    raising a second ticket at that moment is the point at which a hotel stops
    reading these.
    """
    db.seed("conversations", conversation(handler="human_requested",
                                          last_message_at=at(19)))
    run(waiting.sweep(now=NOW))

    db.rows("conversations")[0]["guest_profile"] = {
        "check_in": "2026-09-12", "check_out": "2026-09-16", "units": 2,
    }
    run(waiting.sweep(now=NOW + timedelta(minutes=5)))

    assert len(db.rows("agent_escalations")) == 1


def test_a_hotel_that_answered_and_is_ignored_again_is_told_again(db, sent):
    """
    The other half of the dedupe: it holds while the ticket is open, and lets go
    when it closes. A second silence a day later is a second wait, not a repeat.
    """
    db.seed("conversations", conversation(handler="human_requested",
                                          last_message_at=at(19)))
    run(waiting.sweep(now=NOW))

    run(escalation.note_hotel_replied(db.rows("conversations")[0]["id"]))
    run(waiting.sweep(now=NOW + timedelta(minutes=5)))

    assert len(db.rows("agent_escalations")) == 2
    assert len(sent) == 2


def test_what_the_guest_asked_about_is_in_the_message(db, sent):
    """
    Obligations are already tracked per turn so the model does not drop the
    second question in a message. The person picking up a stalled thread needs
    the same list, and it costs no query — it is on the conversation.
    """
    db.seed("conversations", conversation(
        handler="human_requested", last_message_at=at(19),
        guest_profile={"obligations": {"price": 0, "services": 1},
                       "check_in": "2026-09-12", "check_out": "2026-09-16"},
    ))

    run(waiting.sweep(now=NOW))

    assert "Asked about: the price and a transfer or an extra" in sent[0]
    assert "2026-09-12 to 2026-09-16" in sent[0]


def test_one_hotels_failure_does_not_stop_the_others(db, sent, monkeypatch):
    """
    A sweep is one task for every hotel on the deployment. A property whose
    escalation cannot be raised — no team members, a bad row, a timeout — must
    cost that hotel its notification and nobody else theirs.
    """
    other = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    db.seed("conversations",
            conversation(id="c0c0c0c0-0001-4001-8001-000000000001"),
            conversation(id="c0c0c0c0-0002-4002-8002-000000000002",
                         property_id=other))
    db.seed("properties", {"id": other, "partner_id": PARTNER, "name": "Anandi House"})

    real = escalation.raise_escalation

    async def _explode(**kwargs):
        if kwargs["property_id"] == other:
            raise RuntimeError("that hotel's row is broken")
        return await real(**kwargs)

    monkeypatch.setattr(escalation, "raise_escalation", _explode)
    monkeypatch.setattr(waiting.escalation_service, "raise_escalation", _explode)

    result = run(waiting.sweep(now=NOW))

    assert result["waiting"] == 2
    assert result["raised"] == 1
    assert len(sent) == 1


def test_an_escalation_nobody_could_be_reached_for_is_still_recorded(db, monkeypatch):
    """
    A hotel with no team member configured has nobody to notify. The row is
    written anyway — it is what the dashboard shows, and an escalation that
    vanishes because WhatsApp was the problem is the failure `escalation` was
    written to avoid.
    """
    db.rows("hotel_team_members").clear()
    db.seed("conversations", conversation())

    result = run(waiting.sweep(now=NOW))

    assert result["raised"] == 1 and result["notified"] == 0
    assert db.rows("agent_escalations")[0]["status"] == "open"


def test_a_guest_who_asked_us_to_stop_is_not_waiting():
    """
    The one message that looks exactly like an unanswered guest and is the
    opposite of one. Sending somebody to reply to it is the single outcome they
    explicitly asked not to have — and "stop" was sitting in the production
    inbox the day this was written, on a thread that had correctly gone quiet.
    """
    assert waiting.reason_waiting(conversation(last_message="stop")) is None
    assert waiting.reason_waiting(conversation(last_message="STOP")) is None
    # And the bound that keeps prose out of it: this guest is waiting.
    row = conversation(last_message="we had to stop at the airport, reaching late")
    assert waiting.reason_waiting(row) == "nobody has replied"


def test_a_demo_hotels_test_traffic_never_reaches_a_real_phone(db, sent):
    """
    Every waiting conversation in production the day this shipped was on a demo
    property, left there by the e2e suites — and the demo property has a real
    WhatsApp number on its team. This is the same `is_demo` gate that keeps
    those hotels out of the sitemap and out of IndexNow, for the same reason: a
    hotel whose first nine notifications are about fake guests has learned to
    ignore the tenth.
    """
    db.rows("properties")[0]["is_demo"] = True
    db.seed("conversations", conversation(handler="human_requested",
                                          last_message_at=at(19)))

    result = run(waiting.sweep(now=NOW))

    assert result["waiting"] == 1 and result["raised"] == 0
    assert result["demo"] == 1
    assert sent == []
    assert db.rows("agent_escalations") == []
