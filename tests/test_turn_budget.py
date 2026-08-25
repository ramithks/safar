"""
How long a guest is left watching "typing…".

Two clocks, and only one of them existed.

`hotel_model_timeout_seconds` bounds one HTTP request to Vertex. A turn makes up
to `agent_max_tool_turns` of them with tool calls in between, so what a guest
actually waits was six times a number nobody had multiplied — and a real turn
during this work took **107 seconds** before failing. On Instagram that guest has
closed the app.

`ModelChoice.budget` is the second clock: the whole turn, tools included, per
tier. Spending it is not an error and not a truncation. It stops the loop asking
for *another* round of tools and gives the model one last step to answer with
what it already has, because a slower reply and no reply are different outcomes
and only one of them loses a sale.
"""
import asyncio
import json

import pytest

from app.services.ai import model_router, runtime
from app.services.ai.providers import ModelStep, ToolCall
from tests.test_ai_runtime import db  # noqa: F401 — the runtime fixture, reused


def run(coro):
    return asyncio.run(coro)


# ── the tiers ───────────────────────────────────────────────────────────────

def test_a_classification_does_not_wait_as_long_as_a_negotiation():
    """
    One number for every tier meant waiting forty-five seconds for a
    sub-second call on the cheapest model — a call that is never coming back.
    """
    classify = model_router.select(agent_type="guest", task="intent_classification")
    chat = model_router.select(agent_type="guest", task="guest_conversation")
    reasoning = model_router.select(agent_type="guest", task="cancellation_dispute")

    assert classify.timeout < chat.timeout < reasoning.timeout
    assert classify.budget < chat.budget < reasoning.budget


def test_the_guest_facing_budget_is_under_a_minute():
    """
    Measured, not chosen by taste. Over 1,000 real runs the slowest ordinary
    shape — three tools — averages 20.7 s. Forty seconds is roughly twice that.
    """
    chat = model_router.select(agent_type="guest", task="guest_conversation")
    assert 20 <= chat.budget <= 45


def test_the_reasoning_tier_keeps_room_to_think():
    """
    Not every timeout should come down. The turns that reach this tier are the
    ones where a wrong fast answer costs money.
    """
    assert model_router.select(
        agent_type="guest", task="complex_negotiation",
    ).budget >= 60


def test_an_unlisted_tier_falls_back_to_the_setting_rather_than_to_zero():
    from app.config import settings

    choice = model_router.ModelChoice(
        provider="vertex_ai", model="m", tier="a-tier-nobody-listed",
        thinking_budget=None, temperature=0.3, max_output_tokens=800, reason="",
    )
    assert choice.timeout == settings.hotel_model_timeout_seconds
    assert choice.budget == settings.hotel_turn_budget_seconds


# ── the loop ────────────────────────────────────────────────────────────────

class SlowProvider:
    """
    A provider whose every step advances a fake clock past the budget.

    Faked rather than slept: this test is about the branch, and a suite that
    waits forty real seconds to prove a forty-second rule is one nobody runs.
    """

    name = "slow"

    def __init__(self, clock, steps):
        self.clock = clock
        self.steps = list(steps)
        self.calls = 0
        self.tools_offered = []

    async def step(self, *, model, messages, tools, params):
        self.calls += 1
        self.tools_offered.append(list(tools or []))
        self.clock.advance(45.0)
        return self.steps.pop(0)

    @staticmethod
    def tool_result_message(call, content):
        return {"role": "tool", "name": call.name, "content": content}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


@pytest.fixture
def wired(db, monkeypatch):
    """
    The runtime's own fixture, plus a clock the test can move.

    Reusing `test_ai_runtime.db` rather than seeding a second one: a private
    copy of that fixture would drift, and the shape it seeds is the shape these
    branches run against.
    """
    from tests.test_ai_runtime import make_ctx

    clock = Clock()
    monkeypatch.setattr("app.services.ai.runtime.time.monotonic", clock)
    return db, clock, make_ctx


def test_a_turn_that_runs_long_stops_asking_for_more_tools(wired):
    fake, clock, make_ctx = wired
    wants_more = ModelStep(
        reply=None,
        tool_calls=[ToolCall(id="1", name="get_property", arguments={})],
        raw={"role": "assistant", "content": None},
        tokens_in=100, tokens_out=10,
    )
    provider = SlowProvider(clock, [
        wants_more,                         # 45 s gone — past the 40 s budget
        # What the model says when told to stop and answer. If the loop went
        # round again instead, it would consume this as a tool round and the
        # `wants_more` after it, and the assertions below would see three calls.
        ModelStep(reply="Checking that for you.", tool_calls=[], raw=None,
                  tokens_in=100, tokens_out=10),
        wants_more,
    ])

    result = run(runtime.run_turn(make_ctx(), "how much for two nights?",
                                  provider=provider, task="guest_conversation"))

    # Two steps, not six. The first asked for tools and blew the budget; the
    # second was the one last chance to answer, and it was offered no tools at
    # all — which is what makes it the last one.
    assert provider.calls == 2
    assert provider.tools_offered[0] != []
    assert provider.tools_offered[-1] == []
    assert result.reply == "Checking that for you."


def test_the_model_is_told_why_rather_than_being_cut_off(wired):
    """
    The pending tool calls come back as refusals with a reason the model can
    act on. Handing it nothing would leave it retrying the same call.
    """
    fake, clock, make_ctx = wired
    sent = {}

    class Capturing(SlowProvider):
        async def step(self, *, model, messages, tools, params):
            sent["messages"] = list(messages)
            return await super().step(model=model, messages=messages,
                                      tools=tools, params=params)

    provider = Capturing(clock, [
        ModelStep(reply=None,
                  tool_calls=[ToolCall(id="1", name="get_property", arguments={})],
                  raw={"role": "assistant", "content": None}, tokens_in=1, tokens_out=1),
        ModelStep(reply="One moment.", tool_calls=[], raw=None, tokens_in=1, tokens_out=1),
    ])
    run(runtime.run_turn(make_ctx(), "hi", provider=provider, task="guest_conversation"))

    told = [m for m in sent["messages"] if m.get("role") == "tool"]
    assert told, "the pending call must be answered, not dropped"
    body = json.loads(told[-1]["content"])
    assert body["refused"] is True
    assert "taken too long" in body["reason"]
    # And nothing in it is something a guest could be shown.
    assert "budget" not in body["reason"].lower()


def test_a_turn_with_nothing_to_say_hands_over_rather_than_going_silent(wired):
    """
    Out of time *and* out of words. Silence is the one outcome a hotel cannot
    explain to somebody asking why nobody replied.
    """
    fake, clock, make_ctx = wired
    provider = SlowProvider(clock, [
        ModelStep(reply=None,
                  tool_calls=[ToolCall(id="1", name="get_property", arguments={})],
                  raw={"role": "assistant", "content": None}, tokens_in=1, tokens_out=1),
        ModelStep(reply="", tool_calls=[], raw=None, tokens_in=1, tokens_out=1),
    ])

    result = run(runtime.run_turn(make_ctx(), "hi", provider=provider,
                                  task="guest_conversation"))

    assert result.handoff is True
    assert result.status == "escalated"
    assert result.reply
    assert "budget" in (result.error or "")
    # The conversation is left with a person, not with an agent that has given up.
    assert fake.rows("conversations")[0]["handler"] == "human_requested"


def test_an_ordinary_fast_turn_is_untouched(wired):
    """
    The rule must be invisible on the 99% of turns that finish in seconds.
    """
    fake, clock, make_ctx = wired

    class Quick(SlowProvider):
        async def step(self, *, model, messages, tools, params):
            self.calls += 1
            self.tools_offered.append(list(tools or []))
            self.clock.advance(2.0)
            return self.steps.pop(0)

    provider = Quick(clock, [
        ModelStep(reply=None,
                  tool_calls=[ToolCall(id="1", name="get_property", arguments={})],
                  raw={"role": "assistant", "content": None}, tokens_in=1, tokens_out=1),
        ModelStep(reply="Check-in is at 2 PM.", tool_calls=[], raw=None,
                  tokens_in=1, tokens_out=1),
    ])

    result = run(runtime.run_turn(make_ctx(), "what time is check-in?",
                                  provider=provider, task="guest_conversation"))

    assert result.reply == "Check-in is at 2 PM."
    assert result.handoff is False
    # The tools were still on the table for the second step, which is the
    # difference between a normal turn and a curtailed one.
    assert provider.tools_offered[1] != []


# ── the promise nobody kept ─────────────────────────────────────────────────

class TestOutOfTimeWithNothingToShow:
    """
    **Found on a live evaluation turn, not by a test.**

    A guest asked for rooms for ten people. The turn spent its budget, the
    model was handed the out-of-time note, and it replied:

        I am checking the group rates for 10 people next month with our team
        right now. One of our team members will get back to you shortly with
        the best options.

    It called nothing. `handoff` stayed false, the conversation stayed with the
    AI, and the guest was left waiting for a person nobody had asked for. The
    run recorded `status='succeeded'` and `required_action_status='unmet'`, and
    it was the single turn that failed the promotion gate for Gemini 3.x.

    **The cause was an instruction naming an action the step could not take.**
    The note read *"…or say you are checking and call request_human_handoff"*,
    and the step is called with `tools=[]`. The model did as much of that as it
    could: it produced the words of the handoff without the handoff.

    Two halves to the fix, and neither is a prompt plea. The sentence no longer
    names a tool. And the handoff is decided by the runtime on a fact the
    runtime owns — whether any tool on this turn actually succeeded — because
    running out of time is the runtime's fact about the turn, not the model's.
    """

    def test_a_turn_that_ran_out_with_no_results_hands_over(self, wired):
        fake, clock, make_ctx = wired
        provider = SlowProvider(clock, [
            ModelStep(reply=None,
                      tool_calls=[ToolCall(id="1", name="get_property", arguments={})],
                      raw={"role": "assistant", "content": None},
                      tokens_in=1, tokens_out=1),
            # The holding sentence, exactly as the live model wrote it.
            ModelStep(reply="I am checking the group rates with our team right "
                            "now. One of our team members will get back to you "
                            "shortly.",
                      tokens_in=1, tokens_out=1),
        ])
        result = run(runtime.run_turn(make_ctx(), "we need rooms for 10 people",
                                      provider=provider, task="guest_conversation"))

        assert result.handoff is True, (
            "the guest was promised a person and nobody was asked"
        )
        assert fake.rows("conversations")[0]["handler"] == "human_requested"

    def test_the_note_no_longer_names_a_tool_the_step_cannot_call(self):
        import inspect

        source = " ".join(inspect.getsource(runtime.run_turn).split())
        note = source[source.index("This turn has taken too long"):][:400]
        assert "request_human_handoff" not in note, (
            "the out-of-time note names a tool, and the step below it is given "
            "none — the model can only produce the words"
        )
        assert "no tools left" in note

    def test_but_a_turn_with_real_results_still_answers_for_itself(self, wired):
        """
        The other half, and the reason this is not a blanket escalation. A turn
        that ran three tools and blew its budget on the fourth has real results
        to summarise, and the model's answer stands.
        """
        fake, clock, make_ctx = wired

        class Paced(SlowProvider):
            """
            `SlowProvider` advances 45s a step against a 40s budget, so the very
            first tool round is already over the line and nothing ever runs.
            This one is quick until it is not — which is the shape of the turn
            being tested: real work, then the budget.
            """

            def __init__(self, clock, steps, advances):
                super().__init__(clock, steps)
                self.advances = list(advances)

            async def step(self, *, model, messages, tools, params):
                self.calls += 1
                self.tools_offered.append(list(tools or []))
                self.clock.advance(self.advances.pop(0) if self.advances else 45.0)
                return self.steps.pop(0)

        provider = Paced(clock, [
            ModelStep(reply=None,
                      tool_calls=[ToolCall(id="1", name="get_property", arguments={})],
                      raw={"role": "assistant", "content": None},
                      tokens_in=1, tokens_out=1),
            ModelStep(reply=None,
                      tool_calls=[ToolCall(id="2", name="get_property", arguments={})],
                      raw={"role": "assistant", "content": None},
                      tokens_in=1, tokens_out=1),
            ModelStep(reply="Casa Mandala is in Anjuna, Goa.",
                      tokens_in=1, tokens_out=1),
        ], advances=[5.0, 45.0, 1.0])
        result = run(runtime.run_turn(make_ctx(), "where is the hotel?",
                                      provider=provider, task="guest_conversation"))

        assert any(o.ok for o in result.actions), "the fixture must run one tool"
        assert result.handoff is False
        assert result.reply == "Casa Mandala is in Anjuna, Goa."
