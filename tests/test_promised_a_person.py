"""
Never tell a guest somebody is coming unless somebody is.

**§12's invariant, and it was breakable two ways — both of which had happened.**

The model can write the promise itself. A live evaluation turn produced *"one of
our team members will get back to you shortly"* having called nothing, with
`handoff` false: the words of a handoff without the handoff. That single turn
failed the promotion gate for Gemini 3.x, and fixing the instruction that caused
it fixed one instance rather than the class.

And the runtime's own handoff sentence is composed *before* `_ensure_human`
runs, which swallowed its own failure. One failed UPDATE left a guest who had
been told a person was coming on a conversation still marked `ai` — where
`waiting.reason_waiting` never looks, because it reads that very column.

**The repair direction matters.** A reply that promises somebody *becomes* a
handoff rather than being refused: a guest told a person is coming should get
one, and taking the sentence away leaves them with nothing. Only when the
handoff genuinely cannot be recorded is the promise withdrawn, and then for
something they can act on themselves.
"""
import asyncio

import pytest

from app.services.ai import prompts, recovery, registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.providers import EchoProvider
from tests.fake_supabase import FakeDb, install

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
        "currency": "INR", "timezone": "Asia/Kolkata", "policies": {},
        "status": "active", "settings": {}})
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
        "guest_profile": {}})
    install(monkeypatch, fake)
    return fake


def ctx() -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates"}))
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY})


def handler(db) -> str:
    return db.rows("conversations")[0]["handler"]


# ── what counts as a promise ────────────────────────────────────────────────

class TestTheDetector:
    @pytest.mark.parametrize("reply", [
        "One of our team members will get back to you shortly.",
        "Someone from the hotel will contact you.",
        "A member of the team will be with you shortly.",
        "I've passed this to the hotel team and someone will be with you shortly.",
        "I'm connecting you through to the team.",
        "Let me get somebody from the hotel to look at it with you.",
        "Main team ko bula raha hoon.",
        "Hamara team aapse baat karega.",
        "Main team ko bhej raha hoon.",
    ])
    def test_these_promise_a_person(self, reply):
        assert recovery.promises_a_person(reply), reply

    @pytest.mark.parametrize("reply", [
        "Haan ji, rooms available hain.",
        "Total ₹43,008 hai.",
        "Aapki booking confirm nahi hui hai abhi tak.",
        "The team serves breakfast until 10.",
        "I will send you the payment link.",
        "Main check karta hoon.",
        "Let me get you the price.",
        "Let me check that for you.",
        "",
    ])
    def test_and_these_do_not(self, reply):
        assert not recovery.promises_a_person(reply), reply

    def test_the_runtimes_own_handoff_sentence_is_recognised(self):
        """
        The one that matters most: if the detector ever stopped matching this,
        the withdrawal path below would never fire.
        """
        assert recovery.promises_a_person(prompts.handoff_reply(ctx()))

    def test_and_the_withdrawal_sentence_promises_nobody(self):
        """
        Otherwise withdrawing the promise would re-make it.
        """
        assert not recovery.promises_a_person(prompts.no_handoff_reply(ctx()))


# ── the promise becomes true ────────────────────────────────────────────────

class TestAPromiseTheModelMadeItself:
    def test_it_creates_the_handoff_rather_than_refusing_the_reply(self, db):
        """
        The live turn that failed the gate, in miniature: the model says
        somebody will follow up and calls nothing.
        """
        provider = EchoProvider([
            "I am checking with our team right now. One of our team members "
            "will get back to you shortly."])
        result = run(runtime.run_turn(ctx(), "rooms for 10 people?",
                                      provider=provider, task="guest_conversation"))

        assert result.handoff is True, "the promise was made and nobody was asked"
        assert handler(db) == "human_requested"
        assert result.reply, "the guest was left with nothing"

    def test_the_turn_is_recorded_as_escalated(self, db):
        provider = EchoProvider(["Someone from the hotel will contact you."])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert result.status == "escalated"

    def test_and_the_repair_is_on_the_record(self, db):
        """
        §17. A turn that had to be repaired is a fact somebody should be able to
        count, not something that quietly self-heals.
        """
        provider = EchoProvider(["A member of the team will be with you shortly."])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        rungs = (result.requirements or {}).get("recovery") or []
        assert any(r["failure"] == "promised_a_person" for r in rungs)

    def test_the_run_row_agrees_with_the_result(self, db):
        """
        **The bug this ordering exists for.** The invariant used to run *after*
        the run row was closed, so the row said `succeeded` while the result
        said `escalated`, and the recovery entry never reached the record at
        all. Two accounts of the same turn, and the one a dashboard reads was
        the wrong one.
        """
        provider = EchoProvider(["Someone from the hotel will contact you."])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        row = db.rows("agent_runs")[0]
        assert row["status"] == result.status == "escalated"
        stored = (row.get("required_actions") or {}).get("recovery") or []
        assert any(r["failure"] == "promised_a_person" for r in stored)

    def test_an_ordinary_reply_is_untouched(self, db):
        """
        The control. A turn that promises nobody must stay with the AI — a
        detector that fired on ordinary prose would escalate the whole product.
        """
        provider = EchoProvider(["Haan ji, Deluxe Balcony available hai."])
        result = run(runtime.run_turn(ctx(), "rooms hain?", provider=provider,
                                      task="guest_conversation"))
        assert result.handoff is False
        assert handler(db) == "ai"


# ── the promise is withdrawn when it cannot be kept ─────────────────────────

class TestWhenTheHandoffCannotBeRecorded:
    def _break_the_update(self, monkeypatch):
        def _fails(_ctx):
            return False

        monkeypatch.setattr(runtime, "_ensure_human", _fails)

    def test_the_promise_is_withdrawn_rather_than_left_false(self, db, monkeypatch):
        """
        The database blip. The guest was about to be told somebody is coming,
        on a conversation the sweep will never pick up because the column it
        reads is the one that did not get written.
        """
        self._break_the_update(monkeypatch)
        provider = EchoProvider(["Someone from the team will contact you shortly."])
        result = run(runtime.run_turn(ctx(), "manager se baat karao",
                                      provider=provider, task="guest_conversation"))

        assert not recovery.promises_a_person(result.reply), (
            f"a promise nobody can keep was sent: {result.reply!r}")
        assert result.reply, "the guest was left with nothing at all"

    def test_and_they_are_given_something_they_can_act_on(self, db, monkeypatch):
        self._break_the_update(monkeypatch)
        provider = EchoProvider(["Someone from the team will contact you."])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert "call the hotel" in result.reply.lower()

    def test_and_the_failure_is_on_the_run(self, db, monkeypatch):
        self._break_the_update(monkeypatch)
        provider = EchoProvider(["Someone from the team will contact you."])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert "handoff could not be recorded" in (result.error or "")

    def test_a_handoff_that_worked_keeps_its_promise(self, db):
        """
        **The control the withdrawal path needs, and mutation testing found it
        missing.** Every test above provokes a failure; none asserted that the
        ordinary case still says somebody is coming.

        Breaking `_ensure_human` so it returns None on success — a one-word
        edit — made the withdrawal fire on *every* handoff, and the whole suite
        stayed green. Every guest asking for a manager would have been told to
        ring the hotel themselves.
        """
        provider = EchoProvider([
            [("request_human_handoff", {"reason": "guest_asked"})],
            "Main team ko bula raha hoon, koi aapse baat karega."])
        result = run(runtime.run_turn(ctx(), "manager se baat karao",
                                      provider=provider, task="guest_conversation"))

        assert result.handoff is True
        assert handler(db) == "human_requested"
        assert recovery.promises_a_person(result.reply), (
            f"a handoff that worked withdrew its own promise: {result.reply!r}")
        assert "call the hotel" not in result.reply.lower()

    def test_the_runtimes_own_handoff_sentence_survives_a_working_handoff(self, db):
        """
        The same control on the path that composes the sentence itself: a
        provider outage hands over and says so, and that sentence must reach
        the guest intact.
        """
        from app.services.ai.errors import ProviderError

        provider = EchoProvider([ProviderError("vertex", "503")] * 8)
        result = run(runtime.run_turn(ctx(), "price kya hai", provider=provider,
                                      task="guest_conversation"))
        assert result.handoff is True
        assert handler(db) == "human_requested"
        assert recovery.promises_a_person(result.reply)

    def test_a_reply_that_promised_nothing_is_left_alone(self, db, monkeypatch):
        """
        A failed UPDATE on a turn that made no promise is a problem for the
        sweep, not for the guest — and rewriting an honest answer into "call
        the hotel" would make it one.
        """
        self._break_the_update(monkeypatch)
        provider = EchoProvider(["Haan ji, available hai."])
        result = run(runtime.run_turn(ctx(), "rooms hain?", provider=provider,
                                      task="guest_conversation"))
        assert result.reply == "Haan ji, available hai."
