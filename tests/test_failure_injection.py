"""
Inject each failure the provider can produce, and read the row afterwards.

`test_silent_recovery` proves the ladder *behaves* — retries once, falls back
once, salvages, hands over. This asks the question that comes after: when a
failure happened, does the run row say enough for somebody to know what it was?

It matters because the answer used to be no, in two specific ways found this
pass. A provider attempt was recorded only when there was another rung to fall
to, so the last candidate failing — the turn ending, the case an operator most
wants in the row — logged nothing at all. And the whole model record was
written only on turns that went wrong, which makes every rate computed from it
unanchored: a fallback rate needs the turns that did *not* fall back as much as
the ones that did.

So each class below is injected deliberately, and what is asserted is the row,
not the reply.
"""
import asyncio

import pytest

from app.services.ai import fallback_stats, recovery, registry, runtime
from app.services.ai.errors import ProviderError
from app.services.ai.providers import EchoProvider, ModelStep
import tests.test_golden_conversations as golden


class Injecting:
    """
    A provider that fails in a chosen way for the first `times` calls.

    Wraps `EchoProvider` rather than replacing it so the turn that eventually
    succeeds is a real scripted turn — the point is the ladder underneath a
    working conversation, not a conversation made of failures.
    """

    name = "vertex_ai"

    def __init__(self, failure: str, times: int, then: list[str]):
        self.failure = failure
        self.remaining = times
        self.inner = EchoProvider(then)
        self.models_asked: list[str] = []

    async def step(self, *, model, messages, tools, params):
        self.models_asked.append(model)
        if self.remaining > 0:
            self.remaining -= 1
            if self.failure == recovery.EMPTY:
                return ModelStep(reply="", tokens_in=12, tokens_out=0)
            if self.failure == recovery.TRUNCATED:
                return ModelStep(reply="Aapka total ₹", tokens_in=12, tokens_out=4,
                                 finish_reason="MAX_TOKENS")
            if self.failure == recovery.MALFORMED:
                return ModelStep(reply=None, tokens_in=12, tokens_out=4,
                                 finish_reason="MALFORMED_FUNCTION_CALL")
            if self.failure == recovery.FILTERED:
                return ModelStep(reply=None, tokens_in=12, tokens_out=0,
                                 finish_reason="SAFETY")
            if self.failure == recovery.PROVIDER_ERROR:
                failed = ProviderError("Vertex call failed: ReadTimeout: too slow")
                failed.error_class = "ReadTimeout"
                raise failed
            raise AssertionError(f"no injector for {self.failure}")
        return await self.inner.step(model=model, messages=messages,
                                     tools=tools, params=params)


def run_with(monkeypatch, failure, times, then=("Subah 7 se 10 tak.",)):
    registry.load_all()
    fake = golden.db.__wrapped__(monkeypatch)
    provider = Injecting(failure, times, list(then))
    result = asyncio.run(runtime.run_turn(
        golden.ctx_for(fake), "breakfast kitne baje tak?", provider=provider))
    return fake, result, provider


INJECTABLE = [recovery.EMPTY, recovery.TRUNCATED, recovery.MALFORMED,
              recovery.FILTERED, recovery.PROVIDER_ERROR]


class TestEveryInjectedFailureLeavesARecord:

    @pytest.mark.parametrize("failure", INJECTABLE)
    def test_the_turn_still_carries_a_model_record(self, monkeypatch, failure):
        _, result, _ = run_with(monkeypatch, failure, times=1)
        assert (result.requirements or {}).get("model"), (
            f"a turn that hit {failure} wrote no model record")

    @pytest.mark.parametrize("failure", INJECTABLE)
    def test_the_record_names_the_configured_model(self, monkeypatch, failure):
        _, result, _ = run_with(monkeypatch, failure, times=1)
        assert result.requirements["model"]["configured"]

    @pytest.mark.parametrize("failure", INJECTABLE)
    def test_and_reaches_the_row(self, monkeypatch, failure):
        fake, _, _ = run_with(monkeypatch, failure, times=1)
        row = fake.rows("agent_runs")[0]
        assert (row.get("required_actions") or {}).get("model")

    @pytest.mark.parametrize("failure", INJECTABLE)
    def test_a_guest_is_never_left_with_nothing(self, monkeypatch, failure):
        """
        The property underneath all of this. Whatever failed, the turn ends
        with either a reply or a handoff — never silence.
        """
        _, result, _ = run_with(monkeypatch, failure, times=1)
        assert result.reply or result.handoff


class TestTheRecordSaysWhatHappened:

    def test_a_recoverable_failure_names_its_class(self, monkeypatch):
        _, result, _ = run_with(monkeypatch, recovery.EMPTY, times=1)
        recovery_log = result.requirements.get("recovery") or []
        assert any(a.get("failure") == recovery.EMPTY for a in recovery_log)

    def test_a_transport_failure_carries_the_upstream_exception_type(self, monkeypatch):
        """
        `recovery.classify` collapses a hundred upstream errors into a handful
        of behaviours, which is right for deciding what to do next and lossy
        for deciding whether this is the provider's problem or ours.
        """
        _, result, _ = run_with(monkeypatch, recovery.PROVIDER_ERROR, times=1)
        attempts = result.requirements["model"]["attempts"]
        assert any(a.get("error_class") == "ReadTimeout" for a in attempts), attempts

    def test_and_what_the_failed_attempt_cost(self, monkeypatch):
        _, result, _ = run_with(monkeypatch, recovery.PROVIDER_ERROR, times=1)
        attempts = result.requirements["model"]["attempts"]
        assert all(isinstance(a.get("latency_ms"), int) for a in attempts)

    def test_a_filtered_completion_is_not_retried_and_says_so(self, monkeypatch):
        """
        The same prompt filtered once will filter again. Asking a second time
        spends the guest's patience on the same answer.
        """
        _, result, provider = run_with(monkeypatch, recovery.FILTERED, times=1)
        recovery_log = result.requirements.get("recovery") or []
        assert any(a.get("method") == "not retryable" for a in recovery_log)


class TestTheLastCandidateFailingIsRecorded:
    """
    The bug this class exists for: a provider attempt was appended inside the
    "there is somewhere else to go" branch, so the *final* failure — the one
    that ends the turn — was the one case that left nothing behind.
    """

    def test_a_turn_where_every_model_fails_still_records_the_attempts(self, monkeypatch):
        _, result, provider = run_with(monkeypatch, recovery.PROVIDER_ERROR,
                                       times=50)
        attempts = result.requirements["model"]["attempts"]
        assert attempts, "every model failed and the row recorded no attempt"
        assert any(a.get("method") == "exhausted" for a in attempts), (
            f"nothing marked the last candidate: {attempts}")

    def test_and_the_guest_is_handed_to_a_person(self, monkeypatch):
        _, result, _ = run_with(monkeypatch, recovery.PROVIDER_ERROR, times=50)
        assert result.handoff


class TestThePopulationSplitIsRightUnderInjection:

    def test_a_clean_turn_is_primary_only(self, monkeypatch):
        _, result, _ = run_with(monkeypatch, recovery.EMPTY, times=0)
        assert fallback_stats.assign(result.requirements["model"]) == (
            fallback_stats.PRIMARY_ONLY)

    def test_a_turn_that_climbed_the_ladder_is_not(self, monkeypatch):
        """
        Two empties drive the fallback. Whatever the exact population, it must
        not be counted as a clean reading of the configured model — that was
        the defect that made a quarter of one evaluation column somebody
        else's work.
        """
        _, result, _ = run_with(monkeypatch, recovery.EMPTY, times=2)
        assert fallback_stats.assign(result.requirements["model"]) != (
            fallback_stats.PRIMARY_ONLY)

    def test_the_summary_counts_them_apart(self, monkeypatch):
        turns = []
        for times in (0, 0, 0, 2, 2):
            _, result, _ = run_with(monkeypatch, recovery.EMPTY, times=times)
            turns.append({"model": result.requirements["model"]})
        counts = fallback_stats.summarise(turns)["counts"]
        assert counts[fallback_stats.PRIMARY_ONLY] == 3
        assert sum(counts[p] for p in fallback_stats.POPULATIONS) == 5


class TestInjectionDoesNotDuplicateSideEffects:
    """
    §22's real question. Recovery re-runs a model step, and a model that asked
    to send a message once may ask again — the second attempt must not send a
    second message.
    """

    def test_a_recovered_turn_sends_one_message(self, monkeypatch):
        fake, result, _ = run_with(
            monkeypatch, recovery.EMPTY, times=1,
            then=["Subah 7 se 10 tak."])
        sent = [a for a in result.actions if a.tool == "send_message"]
        assert len(sent) <= 1
