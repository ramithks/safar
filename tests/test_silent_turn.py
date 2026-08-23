"""
A turn that produces nothing must not leave the guest in silence.

This is a regression test for a defect found by counting `agent_runs.error`
rather than by reading code: *"the model produced no reply"* was the second most
common failure in thirty days of production — 78 runs — and had no guest-visible
symptom anybody had connected to it.

What happened: the model succeeded and returned nothing, twice (the empty
completion retry had already fired). The runtime set `status = failed` and left
`reply` as None *without* setting `handoff`. `pipeline` then skipped delivery on
`if result.reply` and skipped `_ensure_human` on `if handoff`, so the guest was
sent nothing and the conversation stayed nominally with the AI.

Every other failure path in `run_turn` — a provider error, an agent error, an
unexpected exception, a spent budget — produces `handoff_reply` and hands over.
This one did not, and the fix is that it does.
"""
import asyncio

import pytest

from app.services.ai import prompts, registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from tests.fake_supabase import FakeDb

PROPERTY = "aaaaaaaa-0000-4000-8000-00000000000a"
PARTNER = "bbbbbbbb-0000-4000-8000-00000000000b"
CONVERSATION = "cccccccc-0000-4000-8000-00000000000c"

registry.load_all()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    """The smallest fixture a turn needs: a property and a conversation."""
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala", "city": "Anjuna",
        "timezone": "Asia/Kolkata", "currency": "INR", "policies": {},
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
    })
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in ("app.services.ai.runtime", "app.services.ai.context",
                   "app.services.ai.prompts", "app.services.messaging.service",
                   "app.services.messaging.pipeline",
                   # A turn that runs a tool meters it, and metering reads the
                   # billing account. A module missing here does not fail
                   # loudly — it quietly reaches the real database.
                   "app.services.ai.usage", "app.services.billing.service",
                   "app.services.pms.clock"):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)
    return fake


@pytest.fixture
def ctx() -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala",
        autonomy_level=2, scopes=frozenset({"messaging"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={"send_message": {"tool_name": "send_message", "enabled": True}},
    )


class Step:
    """One model step, scripted."""

    def __init__(self, reply=None, tool_calls=None):
        self.reply = reply
        self.tool_calls = tool_calls or []
        self.wants_tools = bool(tool_calls)
        self.tokens_in = 10
        self.tokens_out = 0
        self.tokens_thought = 0
        self.raw = {"role": "assistant", "content": reply}


class Provider:
    """A model that answers with whatever it was handed, in order."""

    name = "scripted"

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    async def step(self, **_kwargs):
        self.calls += 1
        return self._steps.pop(0) if self._steps else Step(reply=None)

    def tool_result_message(self, call, payload):
        return {"role": "tool", "content": payload}


class TestASilentTurn:
    def test_an_empty_reply_hands_over_rather_than_saying_nothing(self, ctx, db):
        """
        The whole defect, in one assertion.

        Two empty steps — the first, then the one the empty-completion retry
        asks for — and the guest still gets a sentence and a person.
        """
        provider = Provider([Step(reply=None), Step(reply=None)])
        result = run(runtime.run_turn(
            ctx, "kya rooms available hain?",
            provider=provider, task="availability",
        ))
        assert result.reply == prompts.handoff_reply(ctx)
        assert result.handoff is True
        assert result.status == "escalated"

    def test_the_retry_still_happens_first(self, ctx, db):
        # Asking again is cheap and usually works. Handing over on the first
        # blank would give up on a model having one bad sample.
        provider = Provider([Step(reply=None), Step(reply="Haan, rooms available hain.")])
        result = run(runtime.run_turn(
            ctx, "kya rooms available hain?",
            provider=provider, task="availability",
        ))
        assert result.reply == "Haan, rooms available hain."
        assert result.handoff is False
        assert provider.calls == 2

    def test_a_tool_that_already_spoke_is_not_silence(self, ctx, db, monkeypatch):
        """
        `send_message` succeeding means the guest has their answer.

        Handing over here would be the opposite bug: a person called in because
        the model declined to repeat, in prose, what a tool had just sent.
        """
        from app.services.ai.schemas import ToolOutcome

        async def _execute(name, _args, _ctx):
            # `ok` is derived from `status`, not a field of its own.
            return ToolOutcome(tool=name, status="executed")

        monkeypatch.setattr(runtime.registry, "execute", _execute)

        class Call:
            name = "send_message"
            arguments: dict = {}
            id = "c1"

        provider = Provider([Step(tool_calls=[Call()]), Step(reply=None), Step(reply=None)])
        result = run(runtime.run_turn(
            ctx, "photos bhejo", provider=provider, task="guest_faq",
        ))
        assert result.handoff is False
        assert result.status == "failed"
        assert result.reply is None
