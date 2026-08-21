"""
"Hello" is not an availability query.

Two things went wrong on the first guest message a real hotel ever received on
Instagram, and they had one cause. The guest sent one word; the classifier spent
2.3 seconds on a model call to route it, decided `availability` — the Sales
agent's first candidate — and the hotel's AI answered:

    Hello! To help you find a room, could you please tell me your desired
    arrival and departure dates, and how many adults and children will be
    staying?

Deaf rather than wrong. Nobody opens with "hi" and expects an intake form.

So an opening message skips the classifier entirely: it carries no routing
signal, the model cannot extract one, and the 2.3 seconds bought a worse answer
than the free one.
"""
import pytest

from app.services.ai import classify


@pytest.mark.parametrize("greeting", [
    "Hello", "hello", "HELLO", "  hello  ", "Hi", "hii", "hey", "Hey there",
    "good morning", "Good Evening", "namaste", "hola", "hi!", "hello.",
])
def test_an_opening_is_recognised(greeting):
    assert classify.is_opening(greeting) is True


@pytest.mark.parametrize("real", [
    "no", "when?", "how much", "do you have a room",
    "hello, do you have a room on the 5th?",
    "hi can i cancel", "", "   ", None,
])
def test_anything_carrying_meaning_is_not(real):
    """
    Deliberately conservative. Missing an opening costs one classifier call —
    the behaviour we already had. Treating a real question as an opening would
    route it wrongly, which is the thing being fixed.
    """
    assert classify.is_opening(real) is False


def test_a_greeting_to_the_sales_agent_is_not_an_availability_query():
    """The transcript this file exists for."""
    assert classify.default_task("sales") == "availability"
    assert classify.opening_task("sales") != "availability"
    assert classify.opening_task("sales") in classify.candidates("sales")


def test_the_guest_agent_opens_conversationally():
    assert classify.opening_task("guest") == "guest_conversation"


@pytest.mark.parametrize("agent_type", ["guest", "sales", "frontdesk", "owner", "marketing"])
def test_every_agent_has_a_valid_opening_task(agent_type):
    """
    Including the ones with no entry in the table: they fall back to their
    default, and the answer must still be a task that agent can actually be
    routed to.
    """
    assert classify.opening_task(agent_type) in classify.candidates(agent_type)


def test_an_unknown_agent_still_gets_an_answer():
    assert classify.opening_task("something-new") in classify.candidates("something-new")


class TestTheShortCircuit:
    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_an_opening_never_calls_the_model(self):
        """
        The latency half. A provider that raises proves nothing was called —
        stronger than timing, and it does not flake on a slow machine.
        """
        class Exploding:
            async def step(self, **_):
                raise AssertionError("the classifier called the model for a greeting")

        result = self._run(
            classify.ModelClassifier(provider=Exploding()).classify(
                agent_type="sales", message="Hello", history=[],
            )
        )
        assert result.source == "default"
        assert result.task == classify.opening_task("sales")

    def test_a_greeting_mid_conversation_still_goes_to_the_model(self):
        """
        Once a thread has history, a bare "hi" may be an answer to something.
        That is exactly what the classifier is for, and skipping it there would
        trade a latency win for a wrong route.
        """
        called = {"n": 0}

        class Counting:
            async def step(self, **_):
                called["n"] += 1
                raise RuntimeError("stop here — the call is the assertion")

        self._run(
            classify.ModelClassifier(provider=Counting()).classify(
                agent_type="sales", message="Hello",
                history=[{"role": "user", "content": "are you open?"}],
            )
        )
        assert called["n"] == 1

    def test_a_real_question_still_goes_to_the_model(self):
        called = {"n": 0}

        class Counting:
            async def step(self, **_):
                called["n"] += 1
                raise RuntimeError("stop here")

        self._run(
            classify.ModelClassifier(provider=Counting()).classify(
                agent_type="sales", message="do you have a room on 5 October?",
                history=[],
            )
        )
        assert called["n"] == 1
