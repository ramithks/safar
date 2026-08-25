"""
The 131-second turn that was about to be recorded as a model failure.

A 560-turn sweep and an eleven-message journey ran against the same Vertex
project at the same time. The transport rotated regions, backed off and
retried — exactly what it is built to do — and two turns took 131 and 95
seconds, blew the budget, and escalated.

Above the transport those were indistinguishable from a model that would not
answer. One is somebody else's quota; the other is a model-quality signal, and
the report was about to record the second.

This file pins the distinction, and pins the denominators that follow from it:
a model-quality percentage may only be computed over attempts that say
something about a model.
"""
import pytest

from app.services.ai import attempts


def run(**over) -> dict:
    """An `agent_runs` row as the runtime writes one."""
    row = {
        "status": "succeeded", "error": None,
        "required_actions": {"model": {"configured": "m-primary",
                                       "served_by": ["m-primary"],
                                       "finished_on": "m-primary",
                                       "fallback_count": 0, "attempts": []}},
    }
    row.update(over)
    return row


def with_attempts(*records, **over) -> dict:
    row = run(**over)
    row["required_actions"]["model"]["attempts"] = list(records)
    return row


class TestAQuotaProblemIsNeverAModelProblem:

    def test_a_429_the_transport_reported(self):
        got = attempts.classify(run=with_attempts(
            {"failure": "provider_error", "model": "m-primary",
             "provider_status": "quota", "provider_retries": 3},
            status="failed"))
        assert got == attempts.PROVIDER_429

    def test_and_it_is_excluded_from_model_evaluable(self):
        assert attempts.PROVIDER_429 not in attempts.MODEL_EVALUABLE

    def test_a_vertex_quota_exception_by_name(self):
        got = attempts.classify(run=with_attempts(
            {"failure": "provider_error", "error_class": "VertexQuotaExhausted"},
            status="failed"))
        assert got == attempts.PROVIDER_429

    def test_a_five_hundred_is_the_service_not_the_model(self):
        got = attempts.classify(run=with_attempts(
            {"failure": "provider_error", "error_class": "VertexUpstreamError"},
            status="failed"))
        assert got == attempts.PROVIDER_5XX

    def test_a_call_that_never_arrived(self):
        got = attempts.classify(run=with_attempts(
            {"failure": "provider_error", "error_class": "ConnectTimeout"},
            status="failed"))
        assert got == attempts.NETWORK_FAILURE

    def test_a_turn_the_provider_served_despite_retrying_is_still_the_models(self):
        """
        The other side of the rule, and the one that keeps it honest. Three
        region rotations followed by a good answer is a slow success, not a
        provider failure — excluding it would quietly shrink the denominator
        every time the provider was busy.
        """
        got = attempts.classify(run=with_attempts(
            {"failure": "provider_retry", "method": "succeeded",
             "provider_status": "ok", "provider_retries": 3}))
        assert got == attempts.MODEL_SUCCESS


class TestTheModelsOwnFailures:

    def test_an_empty_completion(self):
        got = attempts.classify(run=run(
            status="failed",
            required_actions={"recovery": [{"failure": "empty_completion",
                                            "method": "retry"}],
                              "model": {"fallback_count": 0}}))
        assert got == attempts.MODEL_EMPTY

    def test_a_malformed_tool_call(self):
        got = attempts.classify(run=run(
            status="failed",
            required_actions={"recovery": [{"failure": "malformed_tool_call",
                                            "method": "retry"}],
                              "model": {}}))
        assert got == attempts.MODEL_MALFORMED_TOOL_CALL

    def test_running_out_of_the_turns_budget(self):
        got = attempts.classify(run=run(
            required_actions={"recovery": [{"failure": "empty_completion",
                                            "method": "out of budget"}],
                              "model": {}}))
        assert got == attempts.MODEL_TIMEOUT

    def test_a_turn_that_fell_back_and_answered(self):
        got = attempts.classify(run=run(
            required_actions={"model": {"fallback_count": 1,
                                        "configured": "m-primary",
                                        "served_by": ["m-primary", "m-other"]}}))
        assert got == attempts.FALLBACK_SUCCESS

    def test_a_ladder_that_ran_out(self):
        got = attempts.classify(run=run(
            status="failed",
            required_actions={"recovery": [{"failure": "empty_completion",
                                            "method": "fallback"}],
                              "model": {"fallback_count": 2}}))
        assert got == attempts.FALLBACK_FAILURE

    def test_a_refused_reply(self):
        got = attempts.classify(run=run(
            error="guardrail: reply quoted ['2000']",
            required_actions={"guardrail": [{"reason": "x"}], "model": {}}))
        assert got == attempts.GUARDRAIL_BLOCK

    def test_a_guardrail_block_is_a_model_quality_event(self):
        """
        In the denominator, deliberately. The model wrote a reply the product
        refused — the largest failure class on real traffic — and excluding it
        would make the number that matters most invisible.
        """
        assert attempts.GUARDRAIL_BLOCK in attempts.MODEL_EVALUABLE

    def test_a_tool_that_failed(self):
        got = attempts.classify(run=run(),
                                actions=[{"tool": "get_quote", "status": "failed"}])
        assert got == attempts.TOOL_FAILURE

    def test_an_ordinary_turn(self):
        assert attempts.classify(run=run()) == attempts.MODEL_SUCCESS


class TestOurOwnFailuresAreOurs:

    def test_a_broken_harness_is_not_a_model_result(self):
        got = attempts.classify(run=None, harness_error="fixture blew up")
        assert got == attempts.EVALUATOR_FAILURE
        assert attempts.EVALUATOR_FAILURE not in attempts.MODEL_EVALUABLE

    def test_a_missing_run_is_a_harness_failure_not_a_pass(self):
        assert attempts.classify(run=None) == attempts.EVALUATOR_FAILURE

    def test_a_runtime_crash_is_not_charged_to_the_model(self):
        """
        Counting our own crash against a model is how a build teaches itself
        that a model is unreliable.
        """
        got = attempts.classify(run=run(status="failed", error="KeyError: 'x'",
                                        required_actions={"model": {}}))
        assert got == attempts.RUNTIME_FAILURE
        assert attempts.RUNTIME_FAILURE not in attempts.MODEL_EVALUABLE


class TestTheThreeDenominators:

    @pytest.fixture
    def sweep(self):
        return (
            [attempts.MODEL_SUCCESS] * 40
            + [attempts.FALLBACK_SUCCESS] * 6
            + [attempts.GUARDRAIL_BLOCK] * 3
            + [attempts.MODEL_EMPTY] * 2
            + [attempts.PROVIDER_429] * 8
            + [attempts.NETWORK_FAILURE] * 1
            + [attempts.EVALUATOR_FAILURE] * 2
        )

    def test_all_three_are_reported(self, sweep):
        found = attempts.summarise(sweep)
        assert found["all_attempts"] == 62
        assert found["provider_success_attempts"] == 51
        assert found["model_evaluable_attempts"] == 51

    def test_the_quota_failures_are_out_of_the_model_denominator(self, sweep):
        found = attempts.summarise(sweep)
        assert found["excluded"][attempts.PROVIDER_429] == 8

    def test_no_blended_accuracy_is_offered(self, sweep):
        """
        A caller wanting one number has to take `model_evaluable_attempts` and
        divide by it, which forces the denominator to be named rather than
        assumed.
        """
        assert "accuracy" not in attempts.summarise(sweep)

    def test_the_denominator_shrinking_is_visible(self, sweep):
        """
        Eleven attempts never reached a model. A report that showed only the
        percentage would look *better* under contention, because the failures
        it dropped were the slow ones.
        """
        found = attempts.summarise(sweep)
        assert found["all_attempts"] - found["model_evaluable_attempts"] == 11
        assert sum(found["excluded"].values()) == 11

    def test_an_empty_sweep_says_nothing_rather_than_zero_percent(self):
        found = attempts.summarise([])
        assert found["all_attempts"] == 0
        assert found["model_evaluable_attempts"] == 0

    def test_every_class_is_either_evaluable_or_explicitly_not(self):
        for name in attempts.CLASSES:
            assert (name in attempts.MODEL_EVALUABLE) or (
                name in attempts.NOT_PROVIDER_SUCCESS) or (
                name == attempts.RUNTIME_FAILURE), name


class TestTheRuntimeRecordsWhatThisReads:
    """
    The classifier is only as good as the fields it reads, and two of them were
    added for it: the transport's retry count and its final status, which had
    been recorded inside `vertex.CallTrace` since it was written and never
    reached anything above the transport.
    """

    def test_a_provider_attempt_can_carry_the_transports_account(self):
        from app.services.ai import recovery

        record = recovery.Attempt(
            failure="provider_error", model="m", method="fallback",
            error_class="VertexQuotaExhausted", latency_ms=9100,
            provider_retries=4, provider_status="quota").as_dict()
        assert record["provider_retries"] == 4
        assert record["provider_status"] == "quota"

    def test_and_an_ordinary_attempt_does_not_carry_empty_fields(self):
        from app.services.ai import recovery

        record = recovery.Attempt("empty_completion", "m", "retry").as_dict()
        assert "provider_retries" not in record
        assert "provider_status" not in record

    def test_the_step_type_has_somewhere_to_put_them(self):
        from app.services.ai.providers import ModelStep

        step = ModelStep(reply="ok")
        assert step.provider_retries == 0
        assert step.provider_status == "ok"
        assert step.locations_tried == ()
