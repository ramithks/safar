"""
What a conversation remembers between turns, and what it must not.

Three separate things, kept separate on purpose:

    conversation history   what the guest said            conversation_messages
    session state          where the flow has got to      conversations.guest_profile
    database truth         price, stock, payment          Postgres, always

The bugs these tests hold the line on are all cases of one of those three
leaking into another. A preference stored as a message scrolls out of history
and is forgotten. A stay described in prose becomes a stay the model believes in
without a tool. A guest identified by a phone number they do not have is a
stranger forever.
"""
import asyncio

import pytest

from app.services.ai.schemas import ToolOutcome
from app.services.messaging import language, session
from app.services.messaging import service as messaging
from tests.fake_supabase import FakeDb

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
IGSID = "1376629097955726"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    for target in ("app.db.client",
                   "app.services.messaging.service",
                   "app.services.messaging.language",
                   "app.services.messaging.session"):
        monkeypatch.setattr(f"{target}.get_db", lambda: fake)
    fake.seed("properties", {"id": PROPERTY, "partner_id": PARTNER,
                             "name": "Casa Mandala", "city": "Anjuna"})
    return fake


def ok(tool: str, args: dict, output: dict | None = None) -> ToolOutcome:
    return ToolOutcome(tool=tool, status="executed", input=args, output=output)


# ── identity: an Instagram guest is not a phone number ───────────────────────

def test_a_returning_instagram_guest_is_recognised(db):
    """
    The lookup used to be `ilike(phone, '%…%')` against a 17-digit IGSID, which
    matches no phone number that has ever existed. Every returning Instagram
    guest arrived as a stranger.
    """
    db.seed("guests", {"id": "g1", "property_id": PROPERTY, "partner_id": PARTNER,
                       "name": "Sahil", "instagram_id": IGSID, "phone": None})

    conversation = messaging._upsert_conversation(
        property_id=PROPERTY, partner_id=PARTNER, channel="instagram",
        external_id="ig_thread_1", account_id="17841400000000001",
        sender_external_id=IGSID, sender_name=None,
    )
    assert conversation["guest_id"] == "g1"
    assert conversation["guest_name"] == "Sahil"


def test_the_same_handle_at_another_hotel_is_another_guest(db):
    """Property-scoped, like every other guest lookup. Joining them would be a
    cross-property leak dressed up as a convenience."""
    db.seed("guests", {"id": "g1", "property_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                       "partner_id": PARTNER, "name": "Sahil", "instagram_id": IGSID})

    conversation = messaging._upsert_conversation(
        property_id=PROPERTY, partner_id=PARTNER, channel="instagram",
        external_id="ig_thread_1", account_id="17841400000000001",
        sender_external_id=IGSID, sender_name=None,
    )
    assert conversation["guest_id"] is None


def test_whatsapp_still_matches_on_the_phone_number(db):
    db.seed("guests", {"id": "g1", "property_id": PROPERTY, "partner_id": PARTNER,
                       "name": "Nisha", "phone": "+919876543210"})

    conversation = messaging._upsert_conversation(
        property_id=PROPERTY, partner_id=PARTNER, channel="whatsapp",
        external_id="wa_thread_1", account_id="phone-number-id",
        sender_external_id="919876543210", sender_name=None,
    )
    assert conversation["guest_id"] == "g1"


def test_booking_is_what_records_the_handle(db):
    """
    A drive-by "hi" does not create a guest record; a paid booking does. This is
    the one moment where the handle and the guest row are both in scope.
    """
    db.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "instagram", "guest_handle": IGSID, "reservation_id": None,
    })
    db.seed("guests", {"id": "g1", "property_id": PROPERTY, "partner_id": PARTNER,
                       "name": "Sahil", "instagram_id": None})

    run(messaging.link_booking({
        "id": "r1", "conversation_id": CONVERSATION, "guest_id": "g1",
        "guest_name": "Sahil", "ref": "TB-1",
    }))
    assert db.rows("guests")[0]["instagram_id"] == IGSID


def test_a_whatsapp_booking_does_not_invent_an_instagram_handle(db):
    db.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "guest_phone": "+919876543210",
        "guest_handle": None, "reservation_id": None,
    })
    db.seed("guests", {"id": "g1", "property_id": PROPERTY, "partner_id": PARTNER,
                       "name": "Nisha", "instagram_id": None})

    run(messaging.link_booking({
        "id": "r1", "conversation_id": CONVERSATION, "guest_id": "g1",
        "guest_name": "Nisha", "ref": "TB-2",
    }))
    assert db.rows("guests")[0]["instagram_id"] is None


def test_a_thread_that_already_has_an_older_booking_still_records_the_handle(db):
    """
    The link is guarded so a newer stay cannot displace an older one on the
    thread. That guard must not also swallow the identity write — a returning
    guest is exactly the case where the thread already names a booking.
    """
    db.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "instagram", "guest_handle": IGSID, "reservation_id": "older",
    })
    db.seed("guests", {"id": "g1", "property_id": PROPERTY, "partner_id": PARTNER,
                       "name": "Sahil", "instagram_id": None})

    run(messaging.link_booking({
        "id": "r1", "conversation_id": CONVERSATION, "guest_id": "g1",
        "guest_name": "Sahil", "ref": "TB-3",
    }))
    assert db.rows("conversations")[0]["reservation_id"] == "older"
    assert db.rows("guests")[0]["instagram_id"] == IGSID


# ── session state: derived from tools, never from prose ──────────────────────

def test_a_search_records_the_dates_the_guest_gave(db):
    patch = session.patch_from([ok("search_availability", {
        "check_in": "2026-09-10", "check_out": "2026-09-14",
        "adults": 4, "children": 0, "units": 2,
    }, {"roomTypes": []})])
    assert patch["check_in"] == "2026-09-10"
    assert patch["adults"] == 4
    assert patch["units"] == 2
    assert patch["stage"] == "browsing"


def test_a_quote_records_the_ids_the_model_would_otherwise_invent(db):
    patch = session.patch_from([ok("get_quote", {
        "check_in": "2026-09-10", "check_out": "2026-09-14", "units": 2,
        "room_type_id": "rt-1", "rate_plan_id": "rp-1",
    }, {"quoteId": "q1", "holdId": "h1"})])
    assert patch["quote_id"] == "q1"
    assert patch["room_type_id"] == "rt-1"
    assert patch["rate_plan_id"] == "rp-1"
    assert patch["stage"] == "quoted"


def test_the_furthest_stage_on_a_turn_wins():
    patch = session.patch_from([
        ok("search_availability", {"check_in": "2026-09-10"}, {}),
        ok("get_quote", {"room_type_id": "rt-1"}, {"quoteId": "q1"}),
    ])
    assert patch["stage"] == "quoted"


def test_a_refused_tool_establishes_nothing():
    refused = ToolOutcome(tool="get_quote", status="refused", input={"units": 9},
                          output=None, refusal_reason="no")
    assert session.patch_from([refused]) == {}


def test_an_ordinary_question_writes_nothing():
    assert session.patch_from([ok("get_property_knowledge", {"query": "breakfast"},
                                  {"answers": []})]) == {}


def test_the_session_merges_rather_than_replaces(db):
    db.seed("conversations", {"id": CONVERSATION, "guest_profile":
                              {"language": "hinglish"}})
    session.merge(CONVERSATION, {"check_in": "2026-09-10", "stage": "browsing"})
    profile = db.rows("conversations")[0]["guest_profile"]
    assert profile["language"] == "hinglish"
    assert profile["check_in"] == "2026-09-10"


def test_the_prompt_line_needs_both_ends_of_a_stay():
    assert session.describe({"guest_profile": {"check_in": "2026-09-10"}}) is None
    told = session.describe({"guest_profile": {
        "check_in": "2026-09-10", "check_out": "2026-09-14",
        "adults": 4, "units": 2,
    }})
    assert "2026-09-10 to 2026-09-14" in told
    assert "2 room(s), 4 adults" in told


# ── language: a preference, not a message ────────────────────────────────────

def test_a_preference_survives_a_reply_that_carries_no_signal(db):
    db.seed("conversations", {"id": CONVERSATION, "guest_profile":
                              {"language": "hinglish"}})
    # "Ok" is not a language. Storing what it looks like would overwrite a real
    # request with a two-letter reply.
    assert language.detect("Ok") is None
    assert language.of_conversation(db.rows("conversations")[0]) == "hinglish"


def test_the_current_message_overrides_what_was_stored(db):
    db.seed("conversations", {"id": CONVERSATION, "guest_profile":
                              {"language": "hinglish"}})
    spoken = language.detect("Could you send me the cancellation policy please")
    assert spoken == language.EN
    language.remember(CONVERSATION, spoken)
    assert db.rows("conversations")[0]["guest_profile"]["language"] == "en"


def test_only_a_remembered_preference_reaches_the_prompt():
    assert language.instruction(language.EN) is None
    assert "Hinglish" in language.instruction(language.HINGLISH)
    assert "Devanagari" in language.instruction(language.HINGLISH)


def test_an_unrecognised_stored_value_is_ignored():
    assert language.of_conversation({"guest_profile": {"language": "klingon"}}) == "en"
    assert language.normalise("KLINGON") is None


# ── the room they already chose ─────────────────────────────────────────────
#
# It was on the session and never in the prompt. A guest who picked a room and
# then changed the number of rooms was asked which room they wanted — a
# question they had answered two turns earlier, and one of the nine failures
# the reference transcript opened with.

def test_the_room_this_turn_was_about_is_remembered_whichever_tool_was_asked():
    """
    Not only `get_quote`. The model naming a room to `search_availability` or
    `send_room_photos` is the same fact arriving earlier, and the turns between
    choosing a room and pricing it are exactly where the choice was lost.
    """
    patch = session.patch_from([
        ok("search_availability", {"room_type_id": "rt-1", "check_in": "2026-09-13"}),
    ])
    assert patch["room_type_id"] == "rt-1"


def test_a_priced_room_outranks_a_discussed_one():
    patch = session.patch_from([
        ok("send_room_photos", {"room_type_id": "rt-1"}),
        ok("get_quote", {"room_type_id": "rt-2", "rate_plan_id": "rp-9"},
           {"quoteId": "q-1", "roomName": "Deluxe Balcony"}),
    ])
    assert patch["room_type_id"] == "rt-2"
    assert patch["rate_plan_id"] == "rp-9"
    assert patch["room_name"] == "Deluxe Balcony"


def test_the_prompt_names_the_room_and_carries_the_id():
    """
    Both, and for different readers. The name is what the reply says back to the
    guest; the id is what a tool takes — without it the model either asks again
    or invents one from a room name, which is how it once produced
    `rate_plan_id = "breakfast_included"`.
    """
    told = session.describe({"guest_profile": {
        "check_in": "2026-09-13", "check_out": "2026-09-17", "adults": 3, "units": 2,
        "room_type_id": "rt-2", "rate_plan_id": "rp-9", "room_name": "Deluxe Balcony",
    }})
    assert "Deluxe Balcony" in told
    assert "rt-2" in told and "rp-9" in told
    assert "do not ask which room again" in told


def test_a_guest_who_has_chosen_nothing_is_described_without_a_room():
    told = session.describe({"guest_profile": {
        "check_in": "2026-09-13", "check_out": "2026-09-17", "adults": 3, "units": 2,
    }})
    assert "room_type_id" not in told
    assert "2026-09-13 to 2026-09-17" in told


def test_new_dates_take_the_chosen_room_with_them():
    """
    A guest who comes back in October about different nights has not chosen
    October's room. `room_name` has to be transactional for the same reason
    `room_type_id` is — a name left behind is worse than an id, because the
    agent says it out loud.
    """
    assert "room_name" in session._TRANSACTIONAL
    assert "room_type_id" in session._TRANSACTIONAL
