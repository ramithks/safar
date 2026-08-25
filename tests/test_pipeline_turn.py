"""
The function that orchestrates every guest turn, tested as a whole.

**It had no test.** `process_conversation` is what runs when a message arrives:
it decides whether the AI may answer, routes to an agent, checks the credit
gate, detects the language, runs the turn, marks the message answered, merges
the session and delivers the reply. Every one of those steps is covered
somewhere. Their composition was covered by `scripts/*_e2e.py` — which need
Vertex, credits and four minutes — and by nothing that runs on a commit.

That is not an abstract gap. Adding the room-name resolution to this function
meant adding an import, and the import went to the wrong place: the module
referenced `room_names` without importing it, and the whole suite stayed green.
The `NameError` would have fired on the first guest message in production.

So this file asserts the composition: that the steps happen, that they happen in
the order that makes them safe, and that a guest correcting their room in prose
reaches the session — the end-to-end version of
`tests/test_room_name_resolution.py`.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.ai import registry
from app.services.ai.providers import EchoProvider
from app.services.messaging import pipeline
from tests.fake_supabase import FakeDb, install

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
AGENT = "a9a9a9a9-0001-4001-8001-000000000001"
DELUXE = "22222222-2222-4222-8222-222222222222"
GARDEN = "33333333-3333-4333-8333-333333333333"

CHECK_IN = date.today() + timedelta(days=30)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "city": "Anjuna", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "status": "active", "settings": {},
        "check_in_time": "14:00", "check_out_time": "11:00",
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
        "guest_profile": {}, "ai_enabled": True,
        "guest_phone": "+919999900011",
        # The 24-hour service window is open because the guest just wrote.
        "last_inbound_at": datetime.now(timezone.utc).isoformat(),
    })
    fake.seed("agents", {
        "id": AGENT, "property_id": PROPERTY, "partner_id": PARTNER,
        "agent_type": "guest", "status": "active", "name": "Mandala",
        "autonomy_level": 3, "active_config_id": "config-1", "limits": {},
        # Which channels this agent answers on — routing filters on it, and
        # an agent with none is "no agent for this channel".
        "channels": ["whatsapp", "instagram"],
    })
    fake.seed("agent_configs", {
        "id": "config-1", "agent_id": AGENT, "version": 1, "model": "",
        "provider": "vertex_ai", "params": {}, "system_prompt": None,
    })
    fake.seed("agent_permissions",
              {"id": "p1", "agent_id": AGENT, "scope": "rates", "granted": True},
              {"id": "p2", "agent_id": AGENT, "scope": "messaging", "granted": True})
    for name in ("get_stay_options", "get_quote", "send_message",
                 "search_availability", "get_group_rates"):
        fake.seed("agent_tools", {"id": f"g-{name}", "agent_id": AGENT,
                                  "tool_name": name, "enabled": True})
    fake.seed("room_types",
              {"id": DELUXE, "property_id": PROPERTY, "partner_id": PARTNER,
               "name": "Deluxe Balcony", "code": "DLX", "total_units": 6,
               "max_occupancy": 3, "is_active": True},
              {"id": GARDEN, "property_id": PROPERTY, "partner_id": PARTNER,
               "name": "Garden Standard", "code": "STD", "total_units": 4,
               "max_occupancy": 2, "is_active": True})
    # **A connected WhatsApp, in sandbox.** `credentials._build_adapter` reads
    # `is_sandbox` off the connection row and returns `FakeWhatsAppAdapter`, so
    # the whole delivery path runs — window rule, idempotency, the message row,
    # the audit trail — and stops at the boundary where Meta would be. That is
    # the seam the E2E suite already uses; without a connection at all,
    # `_deliver` raises CHANNEL_NOT_CONNECTED and every turn ends in a handoff,
    # which would test the failure path and call it the happy one.
    fake.seed("whatsapp_connections", {
        "id": "wc-1", "property_id": PROPERTY, "partner_id": PARTNER,
        "status": "connected", "phone_number_id": "109876543210987",
        "waba_id": "waba-1", "display_phone_number": "+919812345678",
        "is_sandbox": True, "credentials_ref": "default",
    })
    install(monkeypatch, fake)
    # The room list is cached for a minute; a test that ran after another would
    # otherwise read the previous test's hotel.
    from app.services.messaging.whatsapp.fake import FakeWhatsAppAdapter
    from app.services.pms import room_names

    room_names._CACHE.clear()
    FakeWhatsAppAdapter.reset()
    return fake


def say(db, text: str, script: list) -> dict:
    """One inbound message, answered the way a webhook would answer it."""
    now = datetime.now(timezone.utc).isoformat()
    db.seed("conversation_messages", {
        "id": f"m-{len(db.rows('conversation_messages'))}",
        "conversation_id": CONVERSATION, "property_id": PROPERTY,
        "partner_id": PARTNER, "channel": "whatsapp", "direction": "inbound",
        "body": text, "message_type": "text", "sent_at": now, "created_at": now,
        "agent_run_id": None, "delivery_status": "delivered",
    })
    return run(pipeline.process_conversation(
        CONVERSATION, provider=EchoProvider(list(script))))


def profile(db) -> dict:
    return db.rows("conversations")[0].get("guest_profile") or {}


class TestTheTurnHappensAtAll:
    def test_a_message_is_answered_and_marked(self, db):
        """
        The composition, and the thing a missing import breaks. Every assertion
        here is about a *different* step of `process_conversation`.
        """
        out = say(db, "Hello", ["Namaste! Kaise madad karun?"])
        assert out["processed"] == 1
        assert out["runs"], "no run was opened"
        answered = db.rows("conversation_messages")[0]
        assert answered["agent_run_id"] == out["runs"][0], (
            "the message was not stamped, so the next drain would answer it again"
        )

    def test_the_reply_reaches_the_guest(self, db):
        say(db, "Hello", ["Namaste!"])
        outbound = [m for m in db.rows("conversation_messages")
                    if m["direction"] == "outbound"]
        assert outbound and outbound[-1]["body"] == "Namaste!"

    def test_a_second_call_with_nothing_pending_does_nothing(self, db):
        say(db, "Hello", ["Namaste!"])
        again = run(pipeline.process_conversation(
            CONVERSATION, provider=EchoProvider(["should not run"])))
        assert again["processed"] == 0
        assert again["reason"] == "nothing pending"

    def test_a_conversation_a_human_took_over_is_left_alone(self, db):
        db.rows("conversations")[0]["handler"] = "human_requested"
        out = say(db, "Hello", ["should not run"])
        assert out["processed"] == 0
        assert not db.rows("agent_runs"), "a run was opened on a taken-over thread"


class TestTheGuestsRoomNameReachesTheSession:
    """
    The end-to-end half of `tests/test_room_name_resolution.py`. The resolver is
    unit-tested; this asserts the wiring, which is where the import went missing.
    """

    def test_a_room_named_in_prose_lands_with_no_tool_call(self, db):
        say(db, "Actually Garden Standard chahiye", ["Theek hai, dekhta hoon."])
        now = profile(db)
        assert now["room_type_id"] == GARDEN
        assert now["room_name"] == "Garden Standard"

    def test_it_is_marked_for_re_checking_rather_than_promised(self, db):
        """
        A room named in prose is a preference, not an availability check — the
        same rule a date change follows.
        """
        say(db, "Garden Standard chahiye", ["Dekhta hoon."])
        assert profile(db)["room_needs_recheck"] is True

    def test_a_typo_still_lands(self, db):
        say(db, "gardern standrd chahiye", ["Dekhta hoon."])
        assert profile(db)["room_type_id"] == GARDEN

    def test_a_room_this_hotel_does_not_have_changes_nothing(self, db):
        say(db, "Presidential Suite chahiye", ["Woh humare paas nahi hai."])
        assert "room_type_id" not in profile(db)

    def test_switching_rooms_drops_the_old_rooms_price(self, db):
        """
        The two mechanisms composed: the name resolves to an id, and the
        correction graph drops what was derived from the room that was there
        before. Without the first, the second never fires.
        """
        db.rows("conversations")[0]["guest_profile"] = {
            "check_in": str(CHECK_IN), "adults": 2,
            "room_type_id": DELUXE, "rate_plan_id": "rp-deluxe",
            "quote_id": "q-deluxe", "stage": "quoted",
        }
        say(db, "Nahi, Garden Standard chahiye", ["Theek hai."])
        now = profile(db)
        assert now["room_type_id"] == GARDEN
        assert "quote_id" not in now, "the Deluxe's price survived the switch"
        assert "rate_plan_id" not in now, "the Deluxe's rate plan survived"

    def test_an_ambiguous_name_asks_rather_than_choosing(self, db):
        """
        Two rooms both plausibly match. The session carries the question for the
        prompt and — the part that matters — does not pick one.
        """
        from app.services.pms import room_names

        db.rows("room_types")[1]["name"] = "Deluxe Garden"
        room_names._CACHE.clear()
        say(db, "the deluxe one chahiye", ["Kaunsa wala?"])
        now = profile(db)
        assert "room_type_id" not in now, "the resolver guessed between two rooms"
        assert "Deluxe Balcony" in now["room_ambiguous"]
        assert "Deluxe Garden" in now["room_ambiguous"]

    def test_and_the_question_reaches_the_prompt(self, db):
        from app.services.messaging import session
        from app.services.pms import room_names

        db.rows("room_types")[1]["name"] = "Deluxe Garden"
        room_names._CACHE.clear()
        say(db, "the deluxe one chahiye", ["Kaunsa wala?"])
        told = session.describe({"guest_profile": profile(db)})
        assert "not clear which" in told
        assert "do not choose for them" in told

    def test_the_question_is_dropped_once_they_choose(self, db):
        """
        Otherwise the prompt goes on asking a guest to choose between two rooms
        one turn after they chose, which reads as not listening.
        """
        from app.services.pms import room_names

        db.rows("room_types")[1]["name"] = "Deluxe Garden"
        room_names._CACHE.clear()
        say(db, "the deluxe one", ["Kaunsa wala?"])
        assert profile(db).get("room_ambiguous")

        say(db, "Deluxe Garden", ["Theek hai."])
        now = profile(db)
        assert now["room_type_id"] == db.rows("room_types")[1]["id"]
        assert "room_ambiguous" not in now


class TestARoomChosenBeforeAnyDates:
    """
    **A gap the pipeline test found, not the resolver test.**

    `session.describe` returned None unless both dates were known, so
    everything it says about the *room* was invisible on any turn before dates
    were settled. A guest whose first message is "Deluxe Balcony chahiye" — an
    entirely ordinary opening — had the room recorded on the session and never
    mentioned in the prompt, and the agent asked which room they wanted one turn
    after they had said.
    """

    def test_the_room_reaches_the_prompt_with_no_dates_yet(self, db):
        from app.services.messaging import session

        say(db, "Deluxe Balcony chahiye", ["Achha, kaunse dates?"])
        told = session.describe({"guest_profile": profile(db)})
        assert told, "the session said nothing about a room the guest had chosen"
        assert "Deluxe Balcony" in told
        assert "do not ask which room again" in told

    def test_and_so_does_an_ambiguous_one(self, db):
        from app.services.messaging import session
        from app.services.pms import room_names

        db.rows("room_types")[1]["name"] = "Deluxe Garden"
        room_names._CACHE.clear()
        say(db, "the deluxe one", ["Kaunsa wala?"])
        told = session.describe({"guest_profile": profile(db)})
        assert told and "not clear which" in told

    def test_an_empty_session_still_says_nothing(self, db):
        """
        The control. A header with no facts under it is noise in every prompt,
        and the whole point of the early return was to avoid that.
        """
        from app.services.messaging import session

        assert session.describe({"guest_profile": {}}) is None
        assert session.describe({"guest_profile": {"language": "hinglish"}}) is None

    def test_a_stay_with_no_room_is_described_as_it_always_was(self, db):
        from app.services.messaging import session

        told = session.describe({"guest_profile": {
            "check_in": "2026-09-10", "check_out": "2026-09-12", "adults": 2}})
        assert told and "2026-09-10 to 2026-09-12" in told
