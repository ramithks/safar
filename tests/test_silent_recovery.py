"""
The model does not answer. Thirteen ways, and what happens next.

**The number this file exists for, and the correction that came with it.**
`agent_runs.error = 'the model produced no reply'` was 39% of the recorded
failure taxonomy — the largest single reliability signal in the system. Traced
run by run across ninety days of production:

    94 runs recorded         the whole class
    −80 provider = 'echo'    e2e scripts whose scripted provider ran out
    −5  send_message ok      the guest *was* answered; the model declined to
                             repeat itself in prose
    ─────
      9 turns where a guest, or a hotelier, got silence

`EchoProvider` returns `ModelStep(reply="", tokens_in=1, tokens_out=1)` when its
script is exhausted, which is indistinguishable in the telemetry from a model
that answered nothing. So a *test bug* was recorded as a production failure, at
a rate that buried the production failure — 80 of the 94, and the whole reason
the number looked like a crisis.

Nine is the real number and it is what the ladder is written against. Classified
from the rows, they are three different failures:

    MALFORMED   3   finishReason=MALFORMED_FUNCTION_CALL, 4 output tokens
    EMPTY       3   a blank candidate back in 6.5s on the booking task
    NO_PROSE    3   tools did real work; the model then said nothing

Each wants a different answer, which is why `no reply → handoff` was the wrong
shape. §13 asks for <1% unexplained silent responses; every case below is either
recovered or classified.
"""
import asyncio

import pytest

from app.services.ai import recovery, registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.errors import ProviderError
from app.services.ai.providers import ModelStep, ToolCall
from tests.fake_supabase import FakeDb, install

registry.load_all()

PROPERTY = "aaaaaaaa-9000-4000-8000-000000000001"
PARTNER = "bbbbbbbb-9000-4000-8000-000000000001"
CONVERSATION = "cccccccc-9000-4000-8000-000000000001"
MESSAGE = "dddddddd-9000-4000-8000-000000000001"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "city": "Anjuna", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "status": "active", "settings": {},
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
        "guest_profile": {},
    })
    install(monkeypatch, fake)
    return fake


def ctx(*, actor: str = "guest", answering: str | None = MESSAGE) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest" if actor == "guest" else "owner",
        partner_id=PARTNER, property_id=PROPERTY, display_name="Mandala",
        autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk", "revenue"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        conversation_id=CONVERSATION, channel="whatsapp", actor=actor,
        answering=answering,
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


class Scripted:
    """
    A model that answers with whatever it was handed, and records the models it
    was asked on — which is how a fallback is asserted rather than assumed.
    """

    name = "scripted"

    def __init__(self, steps):
        self.steps = list(steps)
        self.models: list[str] = []
        self.calls = 0

    async def step(self, *, model, messages, tools, params):
        self.calls += 1
        self.models.append(model)
        if not self.steps:
            raise AssertionError(
                "the ladder asked for more steps than this test scripted — "
                "which is the exact shape of the bug that polluted production"
            )
        nxt = self.steps.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    def tool_result_message(self, call, content):
        return {"role": "tool", "name": call.name, "tool_call_id": call.id,
                "content": content}


def blank(finish=None, **kw):
    return ModelStep(reply="", finish_reason=finish, tokens_in=1, tokens_out=1, **kw)


def spoke(text="Haan ji, bataiye."):
    return ModelStep(reply=text, tokens_in=10, tokens_out=5)


def calls(tool, args=None):
    return ModelStep(tool_calls=[ToolCall(id="c1", name=tool, arguments=args or {})],
                     raw={"role": "assistant", "content": None},
                     tokens_in=10, tokens_out=5)


def recovery_of(result) -> list[dict]:
    return (result.requirements or {}).get("recovery") or []


# ── 1–7 · what came back, and how it is named ───────────────────────────────

class TestTheFailureIsNamed:
    """
    §1 asks that no case stay 'unknown'. Every shape the provider can return is
    classified before anything decides what to do about it, so the next
    occurrence in production says why it happened.
    """

    @pytest.mark.parametrize("step,expected", [
        (blank(), recovery.EMPTY),
        (ModelStep(reply=None), recovery.EMPTY),
        (blank("STOP"), recovery.EMPTY),
        (blank("MALFORMED_FUNCTION_CALL"), recovery.MALFORMED),
        (blank("MAX_TOKENS"), recovery.TRUNCATED),
        (blank("SAFETY"), recovery.FILTERED),
        (blank("RECITATION"), recovery.FILTERED),
        (blank("PROHIBITED_CONTENT"), recovery.FILTERED),
        (spoke(), recovery.USABLE),
        (calls("get_property"), recovery.USABLE),
        (None, recovery.PROVIDER_ERROR),
    ])
    def test_every_shape_has_a_class(self, step, expected):
        assert recovery.classify(step) == expected

    def test_a_filtered_completion_is_not_retried(self):
        """
        The one non-answer that is not a bad sample. The same prompt filtered
        once will filter again, and asking twice only spends the guest's
        patience on the same answer.
        """
        assert recovery.FILTERED not in recovery.RETRYABLE
        assert recovery.EMPTY in recovery.RETRYABLE
        assert recovery.MALFORMED in recovery.RETRYABLE


# ── 8 · retry succeeds ──────────────────────────────────────────────────────

class TestRungOneAskAgain:
    def test_a_blank_answer_is_asked_again_and_the_guest_gets_a_reply(self, db):
        provider = Scripted([blank(), spoke("Haan, rooms available hain.")])
        result = run(runtime.run_turn(ctx(), "kya rooms hain?", provider=provider,
                                      task="guest_conversation"))
        assert result.reply == "Haan, rooms available hain."
        assert result.handoff is False
        assert result.status == "succeeded"
        assert provider.calls == 2

    def test_a_malformed_tool_call_is_asked_again(self, db):
        """
        Three of the nine. The model tried to emit a tool call and produced
        something Vertex could not parse — it comes back 200, with a candidate,
        a finish reason and no content.
        """
        provider = Scripted([blank("MALFORMED_FUNCTION_CALL"), spoke()])
        result = run(runtime.run_turn(ctx(), "best price?", provider=provider,
                                      task="guest_conversation"))
        assert result.reply
        assert result.handoff is False
        assert recovery_of(result)[0]["failure"] == recovery.MALFORMED
        assert recovery_of(result)[0]["method"] == "retry"

    def test_the_attempt_is_recorded_even_when_it_worked(self, db):
        """
        §10. A recovery that leaves no trace makes the ladder invisible, and
        "how often does this fire" is the number that says whether it earns its
        place.
        """
        provider = Scripted([blank(), spoke()])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert recovery_of(result) == [
            {"failure": recovery.EMPTY, "model": "gemini-3.6-flash", "method": "retry"}
        ]


# ── 9–11 · retry fails, fallback ────────────────────────────────────────────

class TestRungTwoADifferentModel:
    def test_two_blanks_fall_back_to_another_model(self, db):
        provider = Scripted([blank(), blank(), spoke("Namaste!")])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert result.reply == "Namaste!"
        assert result.handoff is False
        assert provider.models[0] == provider.models[1], "the retry is the same model"
        assert provider.models[2] != provider.models[0], "the fallback is not"
        assert [a["method"] for a in recovery_of(result)] == ["retry", "fallback"]

    def test_the_fallback_model_is_recorded(self, db):
        provider = Scripted([blank(), blank(), spoke()])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        fell_back = [a for a in recovery_of(result) if a["method"] == "fallback"][0]
        assert fell_back["model"] == provider.models[2]
        assert fell_back["model"] != provider.models[0]

    def test_a_fallback_that_also_fails_ends_in_a_handoff(self, db):
        provider = Scripted([blank(), blank(), blank(), blank()])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert result.handoff is True
        assert result.status == "escalated"
        assert result.reply, "silence is the one outcome a hotel cannot explain"
        assert recovery_of(result)[-1]["method"] == "handoff"

    def test_a_filtered_completion_goes_straight_to_a_person(self, db):
        """
        No retry, no fallback. Both would produce the same refusal and the guest
        would wait twice as long for it.
        """
        provider = Scripted([blank("SAFETY")])
        result = run(runtime.run_turn(ctx(), "something unsayable",
                                      provider=provider, task="guest_conversation"))
        assert provider.calls == 1
        assert result.handoff is True
        assert recovery_of(result)[0]["method"] == "not retryable"


# ── 12 · deterministic ──────────────────────────────────────────────────────

class TestRungThreeSayWhatTheToolsFound:
    """
    Three of the nine. A hotelier asked how last month went,
    `compare_with_previous_period` and `create_revenue_recommendation` both ran,
    and the answer to their question was computed and then never spoken.
    """

    def test_a_staff_turn_whose_tools_worked_is_not_left_silent(self, db, monkeypatch):
        from app.services.ai.schemas import ToolOutcome

        async def _execute(name, _args, _ctx):
            return ToolOutcome(tool=name, status="executed", output={"periods": 2},
                               result_type="recommendation", result_id="r1")

        monkeypatch.setattr(runtime.registry, "execute", _execute)
        provider = Scripted([calls("create_revenue_recommendation"),
                             blank(), blank(), blank()])
        result = run(runtime.run_turn(ctx(actor="member"), "how was last month?",
                                      provider=provider, task="guest_conversation"))

        assert result.handoff is False
        assert result.status == "succeeded"
        assert result.reply and "recommendation" in result.reply
        assert recovery_of(result)[-1]["method"] == "deterministic"

    def test_the_salvaged_reply_never_names_a_tool(self, db):
        """
        **The guardrail caught this on its first outing**, before any of it
        reached a person. The first version of the staff sentence read "That's
        done — get_property", and `check_reply` refused it for leaking an
        internal identifier. A tool name is not a thing a hotelier asked about.
        """
        from app.services.ai.schemas import ToolOutcome

        made = ToolOutcome(tool="create_revenue_recommendation", status="executed",
                           output={"periods": 2}, result_type="recommendation")
        said = recovery.deterministic_reply([made], staff=True)
        assert said and "create_revenue_recommendation" not in said
        assert "_" not in said, "a tool name is the only thing here with an underscore"

    def test_a_turn_that_only_read_things_produces_no_salvage(self):
        """
        "I read something" is not an answer to anything. A turn whose tools only
        read and whose model then said nothing has genuinely produced no answer,
        and a handoff is the honest end of it.
        """
        from app.services.ai.schemas import ToolOutcome

        read = ToolOutcome(tool="get_property", status="executed",
                           output={"name": "Casa Mandala"}, result_type="none")
        assert recovery.deterministic_reply([read], staff=True) is None

    def test_it_quotes_a_display_string_and_never_computes_one(self):
        from app.services.ai.schemas import ToolOutcome

        quoted = ToolOutcome(tool="get_quote", status="executed",
                             result_type="quote",
                             output={"total": "₹11,400", "totalPaise": 1140000})
        said = recovery.deterministic_reply([quoted], staff=False)
        assert "₹11,400" in said
        assert "1140000" not in said, "the paise figure is not for a guest"

    def test_a_result_it_cannot_describe_produces_nothing(self):
        """
        Falling through to a handoff is the right outcome for anything we
        cannot say without guessing. A template that half-fits is how an
        invented sentence gets sent.
        """
        from app.services.ai.schemas import ToolOutcome

        unknown = ToolOutcome(tool="get_experiences", status="executed",
                              result_type="none", output={"things": [1, 2]})
        assert recovery.deterministic_reply([unknown], staff=False) is None

    def test_a_failed_tool_is_never_described_as_done(self):
        from app.services.ai.schemas import ToolOutcome

        failed = ToolOutcome(tool="get_quote", status="failed", result_type="quote",
                             output={"total": "₹11,400"})
        assert recovery.deterministic_reply([failed], staff=True) is None

    def test_a_guest_turn_with_nothing_quotable_still_hands_over(self, db, monkeypatch):
        """
        The staff widening does not apply to guests. A guest owed an answer and
        given "that's done — get_property" has been told nothing.
        """
        from app.services.ai.schemas import ToolOutcome

        async def _execute(name, _args, _ctx):
            return ToolOutcome(tool=name, status="executed", output={"x": 1},
                               result_type="none")

        monkeypatch.setattr(runtime.registry, "execute", _execute)
        provider = Scripted([calls("get_property"), blank(), blank(), blank()])
        result = run(runtime.run_turn(ctx(), "kya hai?", provider=provider,
                                      task="guest_conversation"))
        assert result.handoff is True

    def test_the_salvaged_reply_goes_through_the_reply_guardrail(self, db, monkeypatch):
        """
        The rung produces its reply *after* the guardrail block has already run,
        so it would otherwise be the one thing in the system reaching a guest
        unchecked. Proved by making the check fail.
        """
        from app.services.ai import guardrails
        from app.services.ai.schemas import ToolOutcome

        async def _execute(name, _args, _ctx):
            return ToolOutcome(tool=name, status="executed",
                               output={"total": "₹11,400"}, result_type="quote")

        monkeypatch.setattr(runtime.registry, "execute", _execute)
        monkeypatch.setattr(
            guardrails, "check_reply",
            lambda *a, **k: guardrails.Verdict(False, "refused by this test"))

        provider = Scripted([calls("get_quote"), blank(), blank(), blank()])
        result = run(runtime.run_turn(ctx(), "kitna?", provider=provider,
                                      task="guest_conversation"))
        assert result.handoff is True, "a salvaged reply the guardrail refuses is not sent"


# ── 3–4 · provider failures and timeouts ────────────────────────────────────

class TestTheCallItselfFails:
    def test_a_provider_error_falls_back_to_another_model(self, db):
        provider = Scripted([ProviderError("vertex", "503"), spoke("Namaste!")])
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert result.reply == "Namaste!"
        assert provider.models[1] != provider.models[0]

    def test_a_provider_that_never_answers_hands_over_and_says_so(self, db):
        provider = Scripted([ProviderError("vertex", "503")] * 6)
        result = run(runtime.run_turn(ctx(), "hi", provider=provider,
                                      task="guest_conversation"))
        assert result.handoff is True
        assert result.reply
        assert result.status == "failed"

    def test_a_business_refusal_is_not_a_model_failure(self, db, monkeypatch):
        """
        §2 is explicit: a tool rejection, an authorisation failure and a
        business-validation failure are not model failures and must not spend a
        fallback. The model answered; the *tool* said no, and the model is told
        so and carries on.
        """
        from app.services.ai.schemas import ToolOutcome

        async def _execute(name, _args, _ctx):
            return ToolOutcome(tool=name, status="refused",
                               refusal_reason="autonomy level 2 is below floor 3",
                               model_message="Not something I can do here.")

        monkeypatch.setattr(runtime.registry, "execute", _execute)
        provider = Scripted([calls("create_booking"), spoke("Main team se poochta hoon.")])
        result = run(runtime.run_turn(ctx(), "book kar do", provider=provider,
                                      task="guest_conversation"))
        assert provider.calls == 2, "a refused tool must not trigger a model fallback"
        assert recovery_of(result) == []
        assert result.reply == "Main team se poochta hoon."


# ── PART 3 · the ladder must not duplicate a side effect ────────────────────

class TestRecoveryIsSideEffectSafe:
    """
    §3. A retry re-issues the *model* call, not the tools — but a retried or
    fallen-back model can perfectly well ask for a tool it already called, and
    a hotel cannot absorb two bookings because a model stuttered.
    """

    def test_a_retry_that_repeats_a_mutating_call_runs_it_once(self, db):
        booking = ("create_task", {"title": "Extra towels please",
                                   "category": "housekeeping"})
        provider = Scripted([
            calls(*booking),          # the model raises a ticket
            blank(),                  # then says nothing
            calls(*booking),          # the retry asks for the same thing
            spoke("Ho jayega."),
        ])
        result = run(runtime.run_turn(ctx(), "towels bhejo", provider=provider,
                                      task="guest_conversation"))
        assert result.reply == "Ho jayega."
        assert len(db.rows("tasks")) == 1, "the ticket was raised twice"

    def test_a_fallback_model_repeating_a_call_runs_it_once(self, db):
        booking = ("create_task", {"title": "Extra pillows please",
                                   "category": "housekeeping"})
        provider = Scripted([
            calls(*booking),
            blank(), blank(),         # retry also blank → fallback
            calls(*booking),          # the *other* model asks for the same thing
            spoke("Ho jayega."),
        ])
        result = run(runtime.run_turn(ctx(), "pillows bhejo", provider=provider,
                                      task="guest_conversation"))
        assert len(db.rows("tasks")) == 1
        assert provider.models[3] != provider.models[0], "it really did fall back"

    def test_the_whole_turn_replayed_after_recovery_still_does_it_once(self, db):
        """
        The two guards composed: the ladder inside one turn, and the
        message-scoped idempotency key across turns. A Celery redelivery of a
        turn that already recovered must not raise a second ticket.
        """
        booking = ("create_task", {"title": "Extra soap please",
                                   "category": "housekeeping"})
        for _ in range(2):
            provider = Scripted([calls(*booking), blank(), spoke("Ho jayega.")])
            run(runtime.run_turn(ctx(), "soap bhejo", provider=provider,
                                 task="guest_conversation"))
        assert len(db.rows("tasks")) == 1

    def test_recovery_never_sends_two_messages(self, db, monkeypatch):
        from app.services.ai.schemas import ToolOutcome

        sent: list[str] = []

        async def _execute(name, args, _ctx):
            if name == "send_message":
                sent.append(args.get("body", ""))
            return ToolOutcome(tool=name, status="executed", output={"ok": True},
                               result_type="message", result_id="m1")

        monkeypatch.setattr(runtime.registry, "execute", _execute)
        provider = Scripted([
            calls("send_message", {"body": "Haan ji."}),
            blank(),
            calls("send_message", {"body": "Haan ji."}),
            spoke("Aur kuch?"),
        ])
        run(runtime.run_turn(ctx(), "hi", provider=provider, task="guest_conversation"))
        assert len(sent) == 1, f"the guest was messaged twice: {sent}"


# ── PART 1 · the pollution that made this look like a crisis ────────────────

class TestAScriptedRunIsNotAProductFailure:
    """
    80 of the 94 recorded no-reply runs were `provider = 'echo'` — e2e scripts
    whose scripted provider had run out mid-conversation. An exhausted script is
    a *test* bug; recorded as "the model produced no reply", it was
    indistinguishable from a production failure, and it outnumbered the real
    ones nine to one.
    """

    def test_the_taxonomy_does_not_count_scripted_runs(self):
        from app.services.ai import taxonomy

        scripted = {"status": "failed", "provider": "echo",
                    "error": "the model produced no reply"}
        assert taxonomy.diagnose(scripted).category == taxonomy.NONE
        assert "scripted" in taxonomy.diagnose(scripted).why

    def test_but_a_real_one_is_still_counted(self):
        from app.services.ai import taxonomy

        real = {"status": "failed", "provider": "vertex_ai",
                "error": "the model produced no reply"}
        assert taxonomy.diagnose(real).category == taxonomy.MODEL_FAILURE
