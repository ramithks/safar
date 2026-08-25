"""
Every way a turn can end badly, and the one state they all have to reach.

**Why this file is about a state and not about a notification.** §11 lists four
paths that must reach a human:

    a handoff the model asked for
    a reply the guardrail refused
    a turn that failed outright
    a conversation nobody has answered

The first three are different failures with different causes, and it would be
easy to write three notification paths for them — three places that send a
WhatsApp, three chances for one of them to be missed. `runtime` does not do
that. All three end at `_ensure_human`, which moves the conversation to
`human_requested`, and `waiting.reason_waiting` treats that state as a guest
waiting whatever put them there. One mechanism, four entrances.

So what has to be asserted is the *funnel*: that each failure actually lands in
the state the sweep looks for. A path that fails and leaves the conversation on
`ai` is a guest sitting in silence with nothing scheduled to notice — which was
78 runs in thirty days before the silent-turn fix, and is the failure this
whole shape exists to make impossible.

`test_waiting_guests.py` owns the other half: that the sweep finds that state,
notifies once, does not spam, clears when staff reply, and never reaches a real
phone from a demo hotel.
"""
import asyncio

import pytest

from app.services.ai import registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.errors import ProviderError
from app.services.ai.providers import EchoProvider
from app.services.pms import waiting
from tests.fake_supabase import FakeDb

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "city": "Anjuna", "state": "Goa", "country": "India",
        "property_type": "boutique", "check_in_time": "14:00",
        "check_out_time": "11:00", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "tax_inclusive": False,
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
    })
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in ("app.services.ai.runtime", "app.services.ai.context",
                   "app.services.ai.tools.read", "app.services.ai.tools.hotel",
                   "app.services.ai.tools.booking", "app.services.pms.rates",
                   "app.services.messaging.service", "app.services.messaging.pipeline"):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)
    return fake


def make_ctx() -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="config-1", config_version=1, model="",
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def handler(db) -> str:
    return db.rows("conversations")[0].get("handler")


def is_waiting(db) -> str | None:
    """
    Would the five-minute sweep pick this conversation up?

    The conversation is asked the same question `waiting.sweep` asks it, with a
    timeline that puts the guest's message before the agent's reply — which is
    the *hard* case: something was sent, so "nobody has replied" does not fire,
    and only the handler state can save it.
    """
    row = dict(db.rows("conversations")[0])
    row.setdefault("last_inbound_at", "2026-08-24T10:00:00+00:00")
    row.setdefault("last_message_at", "2026-08-24T10:00:30+00:00")
    return waiting.reason_waiting(row)


# ── the four entrances ──────────────────────────────────────────────────────

def test_a_handoff_the_model_asked_for_reaches_a_person(db):
    provider = EchoProvider([
        [("request_human_handoff", {"reason": "guest_asked"})],
        "Ek minute, main team ko bhej rahi hoon.",
    ])
    result = run(runtime.run_turn(make_ctx(), "manager se baat karwao",
                                  provider=provider))

    assert result.handoff is True
    assert result.status == "escalated"
    assert handler(db) == "human_requested"
    assert is_waiting(db) == "the AI handed this to a person and nobody has taken it"


def test_a_reply_the_guardrail_refused_reaches_a_person(db):
    """
    The guardrail doing its job is not the end of the story. It replaces the
    reply with an honest one and hands over — because a refused answer means
    the guest asked something the agent could not safely answer, and leaving
    them with a polite sentence and nobody coming is the failure the refusal
    was trying to prevent.
    """
    provider = EchoProvider([
        # A payment link nothing produced. §20's canonical fabrication.
        "Sure! Pay here: https://evil.example.com/pay/12345 and you're booked.",
    ])
    result = run(runtime.run_turn(make_ctx(), "payment link bhejo", provider=provider))

    assert result.handoff is True
    assert (result.error or "").startswith("guardrail:")
    assert "evil.example.com" not in (result.reply or "")
    assert handler(db) == "human_requested"
    assert is_waiting(db)


def test_a_turn_that_failed_outright_reaches_a_person(db):
    """
    The model is gone. The guest is told something true and a person picks up.

    Enough failures to exhaust the degradation ladder, deliberately. One is no
    longer a failed turn — the runtime walks down to a smaller model and carries
    on, which is the right behaviour and means a single error proves nothing
    about this path. What is asserted here is what happens when there is nowhere
    left to walk to.
    """
    provider = EchoProvider([ProviderError("vertex call failed: 503")] * 8)
    result = run(runtime.run_turn(make_ctx(), "price kya hai", provider=provider))

    assert result.status == "failed"
    assert result.handoff is True
    assert result.reply, "a guest must never be left with nothing"
    assert handler(db) == "human_requested"
    assert is_waiting(db)


def test_a_turn_that_said_nothing_at_all_reaches_a_person(db):
    """
    The silent turn: the model succeeded and returned nothing, twice. This was
    the second most common failure in production and had no guest-visible
    symptom anybody had connected to it — the pipeline skipped delivery on an
    empty reply and skipped the handoff because nothing had set it, so the
    conversation sat with the AI nominally on it and nothing sent.
    """
    # Three blanks, not two, and the count is the recovery ladder rather than a
    # detail: the model is asked, asked again, and then asked on a different
    # model before anybody fetches a person. A turn reaches this test's ending
    # only when all three come back empty — which is the point of the ladder and
    # is what makes the handoff below the last resort rather than the first.
    provider = EchoProvider(["", "", ""])
    result = run(runtime.run_turn(make_ctx(), "price kya hai", provider=provider))

    assert result.handoff is True
    assert result.reply
    assert "no reply" in (result.error or "")
    assert handler(db) == "human_requested"
    assert is_waiting(db)


# ── and the ones that must not ──────────────────────────────────────────────

def test_an_ordinary_answered_turn_leaves_the_ai_on_it(db):
    """
    The control. If every turn ended at `human_requested` the sweep would
    notify a hotel about every guest it successfully answered, and a hotel that
    gets a WhatsApp per conversation stops reading them.
    """
    provider = EchoProvider(["Breakfast 8 se 10:30 tak milta hai."])
    result = run(runtime.run_turn(make_ctx(), "breakfast kab tak?", provider=provider))

    assert result.handoff is False
    assert handler(db) == "ai"
    assert is_waiting(db) is None


def test_a_guest_who_asked_us_to_stop_is_never_swept(db):
    """
    §11: "guest explicitly says stop → NOT treated as waiting". Raising a ticket
    that sends somebody to reply to them is the one outcome they explicitly
    asked not to have.
    """
    db.rows("conversations")[0]["handler"] = "human_requested"
    db.rows("conversations")[0]["last_message"] = "STOP"
    assert is_waiting(db) is None


def test_a_conversation_a_person_has_actually_taken_is_not_waiting(db):
    """
    `human` and `human_requested` are different states and the difference is the
    whole point: one means somebody is typing, the other means nobody has looked.
    """
    db.rows("conversations")[0]["handler"] = "human"
    assert is_waiting(db) is None


# ── the funnel itself ───────────────────────────────────────────────────────

def test_every_failure_path_goes_through_one_function(db):
    """
    Stated as a test because the alternative design is the dangerous one.

    Three failures could each have grown their own notification. They did not:
    all of them call `_ensure_human`, and `waiting.reason_waiting` reads the
    state it leaves. One mechanism with four entrances beats four mechanisms,
    because the fifth failure somebody adds next month inherits it for free.
    """
    import inspect

    source = inspect.getsource(runtime.run_turn)
    # The *call*, not the word — the failure it describes is also written out
    # in a comment a few lines above, and counting mentions would make this
    # test fail on an edit to the prose.
    assert source.count("run_db(_ensure_human, ctx)") == 1, (
        "a second notification path has appeared; the point of this shape is "
        "that a new failure mode inherits the existing one"
    )
    assert "if handoff and ctx.conversation_id" in source
