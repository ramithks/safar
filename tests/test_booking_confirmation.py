"""
Telling the guest their money landed, without being asked.

The conversation this exists for went quiet in the worst possible place: the
guest paid, nothing spoke, and ninety seconds later he was asking a machine that
could not see his own booking whether it existed. A confirmation is not a nicety
on this path — it is the only moment where the product's central promise, that
an AI employee can take a booking end to end, is either kept or visibly broken.

Three properties, and all three are about failure rather than success:

  * it is sent **once**, however many times fulfilment is attempted;
  * it never **raises**, because a booking that is paid for must exist whether
    or not a message can be delivered;
  * it never claims anything the reservation row does not say.
"""
import asyncio

import pytest

from app.services.messaging import language
from app.services.messaging import service as messaging
from app.services.messaging.channels import WINDOW_CLOSED, MessagingError
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"


def run(coro):
    return asyncio.run(coro)


RESERVATION = {
    "id": "r1", "property_id": PROPERTY, "partner_id": PARTNER,
    "conversation_id": CONVERSATION, "ref": "TB-5CECKV",
    "status": "confirmed", "payment_status": "token_paid",
    "check_in": "2026-09-10", "check_out": "2026-09-14", "nights": 4,
    "units": 2, "guest_id": "g1", "guest_name": "Sahil",
    "total_paise": 3584000, "paid_paise": 716800,
}


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    monkeypatch.setattr("app.services.messaging.service.get_db", lambda: fake)
    monkeypatch.setattr("app.services.messaging.language.get_db", lambda: fake)
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "instagram", "external_id": "1376629097955726",
        "guest_handle": "1376629097955726", "handler": "ai", "status": "open",
        "reservation_id": None, "guest_id": None,
        "guest_profile": {"language": "hinglish"},
    })
    return fake


@pytest.fixture
def sent(monkeypatch) -> list:
    """Capture what would have gone out, instead of going out."""
    calls: list = []

    async def fake_send(conversation, **kwargs):
        calls.append({"conversation": conversation, **kwargs})
        return messaging.SentMessage(
            message={"id": "m1", **kwargs}, external_id="mid-1", replayed=False,
        )

    monkeypatch.setattr(messaging, "send", fake_send)
    return calls


# ── linking the thread to the stay ───────────────────────────────────────────

def test_the_thread_learns_about_the_booking(db):
    run(messaging.link_booking(RESERVATION))
    row = db.rows("conversations")[0]
    assert row["reservation_id"] == "r1"
    # The guest record too. `list_guest_reservations` starts from this column,
    # and on Instagram nothing else was ever going to fill it in.
    assert row["guest_id"] == "g1"
    assert row["guest_name"] == "Sahil"


def test_an_existing_booking_on_the_thread_is_not_replaced(db):
    db.rows("conversations")[0]["reservation_id"] = "older"
    run(messaging.link_booking(RESERVATION))
    assert db.rows("conversations")[0]["reservation_id"] == "older"


def test_a_booking_from_the_website_has_no_thread_to_link(db):
    run(messaging.link_booking({**RESERVATION, "conversation_id": None}))
    assert db.rows("conversations")[0]["reservation_id"] is None


# ── the message itself ───────────────────────────────────────────────────────

def test_it_speaks_the_language_the_guest_asked_for(db, sent):
    run(messaging.announce_booking(RESERVATION))
    assert len(sent) == 1
    body = sent[0]["body"]
    assert "Aapki booking confirm ho gayi hai" in body
    assert "TB-5CECKV" in body
    # Devanagari would be the exact regression: the guest asked for Hinglish.
    assert not any("ऀ" <= ch <= "ॿ" for ch in body)


def test_it_states_the_two_amounts_that_matter(db, sent):
    run(messaging.announce_booking(RESERVATION))
    body = sent[0]["body"]
    assert "₹7,168" in body      # taken now
    assert "₹28,672" in body     # due at the hotel
    assert "₹35,840" not in body  # never the total, which is not what was charged


def test_a_fully_paid_stay_owes_nothing(db, sent):
    run(messaging.announce_booking({**RESERVATION, "paid_paise": 3584000}))
    body = sent[0]["body"]
    assert "Poora payment ho gaya" in body
    assert "dena hai" not in body


def test_english_is_the_default(db, sent):
    db.rows("conversations")[0]["guest_profile"] = {}
    run(messaging.announce_booking(RESERVATION))
    assert "Your booking is confirmed" in sent[0]["body"]


def test_hindi_when_hindi_was_asked_for(db, sent):
    db.rows("conversations")[0]["guest_profile"] = {"language": "hi"}
    run(messaging.announce_booking(RESERVATION))
    assert "बुकिंग कन्फर्म" in sent[0]["body"]


def test_it_carries_the_booking_link(db, sent):
    run(messaging.announce_booking(RESERVATION))
    assert "/checkin/TB-5CECKV" in sent[0]["body"]


# ── sent once, and never fatal ───────────────────────────────────────────────

def test_the_reference_is_the_booking_so_a_retry_cannot_duplicate(db, sent):
    run(messaging.announce_booking(RESERVATION))
    run(messaging.announce_booking(RESERVATION))
    assert {c["client_reference"] for c in sent} == {"booking:r1"}


def test_a_closed_window_is_not_an_error(db, monkeypatch):
    async def refuse(conversation, **kwargs):
        raise MessagingError(WINDOW_CLOSED, "the instagram service window closed")

    monkeypatch.setattr(messaging, "send", refuse)
    assert run(messaging.announce_booking(RESERVATION)) is None


def test_a_provider_that_explodes_is_not_an_error(db, monkeypatch):
    async def explode(conversation, **kwargs):
        raise RuntimeError("socket hung up")

    monkeypatch.setattr(messaging, "send", explode)
    assert run(messaging.announce_booking(RESERVATION)) is None


def test_a_booking_with_no_thread_says_nothing(db, sent):
    run(messaging.announce_booking({**RESERVATION, "conversation_id": None}))
    assert sent == []


# ── the preference itself ────────────────────────────────────────────────────

@pytest.mark.parametrize("message,expected", [
    ("Kya AAP hindi m baat kar sakte h?", language.HINGLISH),
    ("Okie 2 kamre karne h aaur hinglish m baat karo please", language.HINGLISH),
    ("M next month 10th Ko aauga 4 din k liye 3 logo k sath", language.HINGLISH),
    ("हाँ, मैं हिंदी में बात कर सकता हूँ।", language.HI),
    ("Can you send me price of hotel?", language.EN),
    ("Do you have photos of the deluxe room", language.EN),
    # No grammar to read. Guessing here is how a preference gets overwritten by
    # a two-letter reply, which is the whole failure mode.
    ("Ok", None),
    ("Sahil", None),
    ("", None),
])
def test_detection(message, expected):
    assert language.detect(message) == expected


def test_a_stored_preference_survives_a_merge(db):
    db.rows("conversations")[0]["guest_profile"] = {"quote_id": "q1"}
    language.remember(CONVERSATION, language.HINGLISH)
    profile = db.rows("conversations")[0]["guest_profile"]
    assert profile["language"] == "hinglish"
    # The session state it shares the column with must survive. A language
    # write that dropped a live quote would be a worse bug than the one it fixes.
    assert profile["quote_id"] == "q1"
