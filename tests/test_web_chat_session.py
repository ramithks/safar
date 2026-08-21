"""
The only thing standing between one visitor's conversation and another's.

A guest on a hotel's booking page has no account, so the session token *is* the
authentication. It is signed rather than random for one specific reason: it
names a property and a conversation, and an unsigned identifier would let
anybody read a stranger's thread by pasting theirs in.

Every test here is an attack. The last one is the subtle one — a token this
service legitimately issued, replayed on a *different* hotel's page. Its
signature is genuinely ours and genuinely valid, which is exactly why the
property has to be checked separately.
"""
import json
import time

import pytest

from app.config import settings
from app.services.messaging import web_chat


HOTEL = "47f4cbf8-7cad-4369-a4c9-a4a70a0c29c0"
OTHER_HOTEL = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
CONVERSATION = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(settings, "supabase_service_key", "test-service-key-not-a-real-one")


def test_a_token_round_trips_to_its_own_conversation():
    token = web_chat.issue_session(property_id=HOTEL, conversation_id=CONVERSATION)
    assert web_chat.read_session(token, property_id=HOTEL) == CONVERSATION


def test_a_token_issued_for_one_hotel_is_refused_on_another():
    """
    The replay that a signature alone does not catch.

    Both pages are served by the same process with the same key, so the HMAC
    verifies. Without the property check a visitor could carry a session from
    hotel A to hotel B and the second hotel would load the first one's thread.
    """
    token = web_chat.issue_session(property_id=HOTEL, conversation_id=CONVERSATION)
    assert web_chat.read_session(token, property_id=OTHER_HOTEL) is None


def test_a_tampered_payload_is_refused():
    token = web_chat.issue_session(property_id=HOTEL, conversation_id=CONVERSATION)
    body, _, sig = token.partition(".")

    forged = json.dumps(
        {"p": HOTEL, "c": "99999999-9999-4999-8999-999999999999", "t": int(time.time())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    swapped = f"{web_chat._b64e(forged)}.{sig}"

    assert web_chat.read_session(swapped, property_id=HOTEL) is None


def test_a_token_signed_with_a_different_key_is_refused(monkeypatch):
    token = web_chat.issue_session(property_id=HOTEL, conversation_id=CONVERSATION)
    monkeypatch.setattr(settings, "supabase_service_key", "a-completely-different-key")
    assert web_chat.read_session(token, property_id=HOTEL) is None


def test_an_expired_token_starts_a_new_conversation(monkeypatch):
    real_time = time.time
    monkeypatch.setattr(web_chat.time, "time", lambda: real_time() - web_chat.SESSION_TTL_SECONDS - 60)
    token = web_chat.issue_session(property_id=HOTEL, conversation_id=CONVERSATION)
    monkeypatch.setattr(web_chat.time, "time", real_time)

    assert web_chat.read_session(token, property_id=HOTEL) is None


@pytest.mark.parametrize("garbage", [None, "", "not-a-token", "no-dot-here", "a.b", "....", "x." * 40])
def test_garbage_never_raises(garbage):
    """
    A malformed token is a new chat, not a 500.

    This runs on a public page. Every rejection path has to end in "start a
    fresh conversation", because the alternative a guest sees is a broken widget
    on the hotel's booking page.
    """
    assert web_chat.read_session(garbage, property_id=HOTEL) is None


def test_the_token_does_not_leak_the_key():
    token = web_chat.issue_session(property_id=HOTEL, conversation_id=CONVERSATION)
    assert settings.supabase_service_key not in token


def test_the_opening_line_names_the_hotel():
    # The one product requirement in this file: the widget must not read as a
    # generic chatbot bolted onto a page.
    assert "Anandi House" in web_chat.opening_line("Anandi House")
