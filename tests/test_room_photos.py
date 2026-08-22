"""
"Photos hain?" — and whether anything can answer it.

A real guest asked, mid-purchase, on Instagram. The reply was:

    I'm sorry, main aapko rooms ki photos nahi dikha sakta. Main sirf text mein
    information de sakta hoon.

Three separate things had to be true before that could stop being the answer,
and none of them was: a hotel needed somewhere to put a photograph, a channel
needed a way to send one, and the agent needed a tool that was not "paste a
link". This covers the third, and the property that makes it safe:

**The model names a room. It never names a URL.** The photographs come out of
`room_types.photos` for this property, so there is no argument through which a
model — or a guest talking to one — can make a hotel send an arbitrary image.
The guardrail that catches invented links in prose has no equivalent for
attachments, and this is the reason it does not need one.
"""
import asyncio

import pytest

from app.services.ai import registry
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.messaging import service as messaging
from tests.fake_supabase import FakeDb

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
DELUXE = "10000000-0000-4000-8000-000000000001"
GARDEN = "10000000-0000-4000-8000-000000000002"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    for target in ("app.db.client", "app.services.ai.tools.communication",
                   "app.services.messaging.service"):
        monkeypatch.setattr(f"{target}.get_db", lambda: fake)
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "instagram", "guest_handle": "igsid_1", "handler": "ai",
    })
    fake.seed(
        "room_types",
        {"id": DELUXE, "property_id": PROPERTY, "is_active": True, "sort_order": 1,
         "name": "Deluxe Balcony",
         "photos": ["https://cdn.example/d1.jpg", "https://cdn.example/d2.jpg",
                    "https://cdn.example/d3.jpg", "https://cdn.example/d4.jpg",
                    "https://cdn.example/d5.jpg"]},
        {"id": GARDEN, "property_id": PROPERTY, "is_active": True, "sort_order": 2,
         "name": "Garden Standard", "photos": ["https://cdn.example/g1.jpg"]},
    )
    return fake


@pytest.fixture
def sent(monkeypatch) -> list:
    calls: list = []

    async def fake_send(conversation, **kwargs):
        calls.append(kwargs)
        return messaging.SentMessage(message={"id": "m"}, external_id="x", template_name=None)

    monkeypatch.setattr(messaging, "send", fake_send)
    return calls


def ctx(**over) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Agent", autonomy_level=3,
        scopes=frozenset({"messaging", "tasks", "rates"}),
    )
    base = AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        model="test", conversation_id=CONVERSATION, actor="guest",
        run_id="run-1",
        tool_grants={"send_room_photos": {"tool_name": "send_room_photos", "enabled": True}},
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def call(args: dict, context: AgentContext | None = None):
    return run(registry.execute("send_room_photos", args, context or ctx()))


# ── what actually goes out ───────────────────────────────────────────────────

def test_one_room_sends_several_of_it(db, sent):
    outcome = call({"room_type_id": DELUXE})
    assert outcome.ok, outcome.refusal_reason or outcome.error
    assert outcome.output["sent"] == 4
    # Four, not five. A guest whose phone fills with photographs of a bathroom
    # has been sent a problem rather than an answer.
    assert len(sent) == 4
    assert [c["image_url"] for c in sent] == [
        "https://cdn.example/d1.jpg", "https://cdn.example/d2.jpg",
        "https://cdn.example/d3.jpg", "https://cdn.example/d4.jpg",
    ]


def test_no_room_named_sends_one_of_each(db, sent):
    """"Show me the rooms" is a menu, not a gallery."""
    outcome = call({})
    assert outcome.output["sent"] == 2
    assert [c["image_url"] for c in sent] == [
        "https://cdn.example/d1.jpg", "https://cdn.example/g1.jpg",
    ]


def test_the_caption_rides_on_the_first_picture_only(db, sent):
    call({"room_type_id": DELUXE, "caption": "Yeh raha Deluxe Balcony 😊"})
    assert sent[0]["body"] == "Yeh raha Deluxe Balcony 😊"
    assert all(c["body"] is None for c in sent[1:])


def test_without_a_caption_the_room_name_is_used(db, sent):
    call({"room_type_id": DELUXE})
    assert sent[0]["body"] == "Deluxe Balcony"


def test_a_retried_turn_cannot_send_the_same_photo_twice(db, sent):
    call({"room_type_id": DELUXE})
    references = [c["client_reference"] for c in sent]
    assert references == [f"photo:run-1:{i}" for i in range(4)]
    assert len(set(references)) == len(references)


# ── the safety property ──────────────────────────────────────────────────────

def test_the_model_cannot_name_a_url(db):
    """
    There is no argument for one. The whole input model is a room id and a
    caption, so a compromised model has no path to making a hotel send an
    arbitrary image — which is why the link guardrail needs no attachment twin.
    """
    spec = registry.REGISTRY["send_room_photos"]
    fields = set(spec.input_model.model_fields)
    assert fields == {"room_type_id", "caption"}


def test_another_hotels_room_sends_nothing(db, sent):
    db.seed("room_types", {
        "id": "theirs", "property_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "is_active": True, "sort_order": 1, "name": "Theirs",
        "photos": ["https://cdn.example/secret.jpg"],
    })
    outcome = call({"room_type_id": "10000000-0000-4000-8000-00000000ffff"})
    assert outcome.output["sent"] == 0
    assert sent == []


def test_an_inactive_room_is_not_offered(db, sent):
    db.rows("room_types")[0]["is_active"] = False
    call({})
    assert [c["image_url"] for c in sent] == ["https://cdn.example/g1.jpg"]


# ── the answer that matters most ─────────────────────────────────────────────

def test_a_hotel_with_no_photos_gets_an_honest_answer(db, sent):
    """
    The exact failure this whole chain exists for. "The hotel has not put photos
    up yet" is true and fixable; "I am unable to show you photos" reads as the
    product being incapable, and it is what a real guest was told.
    """
    for room in db.rows("room_types"):
        room["photos"] = []
    outcome = call({})
    assert outcome.ok
    assert outcome.output["sent"] == 0
    instruction = outcome.output["instruction"].lower()
    assert "has not uploaded photos" in instruction
    assert "do not say you are unable to show photos" in instruction
    assert sent == []


def test_one_failed_send_does_not_lose_the_others(db, monkeypatch):
    """Three photographs and one failure beats nothing, and the guest can see
    what arrived."""
    calls: list = []

    async def flaky(conversation, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("provider hiccup")
        return messaging.SentMessage(message={"id": "m"}, external_id="x", template_name=None)

    monkeypatch.setattr(messaging, "send", flaky)
    outcome = call({"room_type_id": DELUXE})
    assert outcome.ok
    assert outcome.output["sent"] == 3


def test_every_send_failing_is_a_refusal_the_guest_hears_about(db, monkeypatch):
    async def broken(conversation, **kwargs):
        raise RuntimeError("nothing works")

    monkeypatch.setattr(messaging, "send", broken)
    outcome = call({"room_type_id": DELUXE})
    assert not outcome.ok


def test_a_run_with_no_conversation_refuses(db, sent):
    outcome = call({}, ctx(conversation_id=None))
    assert not outcome.ok
    assert sent == []


# ── the row that gets written ────────────────────────────────────────────────

def _queued(db, **over) -> dict:
    spec = {
        "body": "Checking that for you.", "template": None, "author": "agent",
        "author_name": "Agent", "author_member": None, "agent_action_id": None,
        "client_reference": "ref-1",
    }
    spec.update(over)
    row, replayed = messaging._queue_outbound(db.rows("conversations")[0], spec)
    assert not replayed
    return row


def test_a_text_message_still_writes_an_empty_media_list(db):
    """
    `conversation_messages.media_urls` is NOT NULL with a `{}` default. Adding
    the key to this insert at all means a text message now sends *something* for
    it, and an explicit null overrides a default rather than falling back to it
    — so the first plain reply after the photo feature landed failed to persist,
    which the live Instagram run caught and no unit test could have.
    """
    assert _queued(db)["media_urls"] == []


def test_a_photo_message_carries_the_url_and_says_it_is_an_image(db):
    row = _queued(db, image_url="https://cdn.example/d1.jpg", client_reference="ref-2")
    assert row["media_urls"] == ["https://cdn.example/d1.jpg"]
    assert row["message_type"] == "image"
