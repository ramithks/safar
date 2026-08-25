"""
Sixteen words the whole system agrees on, checked against real rows.

§20: *"Every failed evaluation should map to one."* A vocabulary nothing
produces is a vocabulary nobody queries, so `taxonomy.diagnose` reads the rows
that already exist — `agent_runs.error`, `agent_actions.status` and
`refusal_reason`, the guardrail's verdict, the arbiter's judgement — and names
the failure without anything being instrumented.

**Every case below is a real string from production.** The classifier was
written first and then run against thirty days of `agent_runs`, which is how
three of its rules turned out to be wrong. Those three have their own tests:

  · "OPENAI_API_KEY is not configured" filed as MODEL_FAILURE. It is a
    deployment that is not set up, and the model never came into it.
  · "get_group_rates arguments rejected — units: Field required" filed as
    INVENTORY_CONFLICT, because `units` was in the sold-out vocabulary. A word
    that appears in both a business outcome and a schema error is evidence of
    neither.
  · 93 runs — 41% of everything — filed as HANDOFF_ERROR on the strength of
    `status = 'escalated'` alone. That is what a *correct* handoff looks like.
    Counting those as errors would have made the arbiter appear to fail 93
    times in the report written to measure it.
"""
import pytest

from app.services.ai import taxonomy as t


def run(status="succeeded", error=None, **extra):
    return {"status": status, "error": error, **extra}


def action(tool="get_quote", status="failed", refusal_reason=None, error=None):
    return {"tool": tool, "status": status,
            "refusal_reason": refusal_reason, "error": error}


class TestTheThreeRulesRealDataCorrected:
    def test_a_missing_key_is_a_configuration_error_not_a_model_failure(self):
        d = t.diagnose(run("failed", "OPENAI_API_KEY is not configured"))
        assert d.category == t.CONFIGURATION_ERROR
        assert d.owner == "onboarding"

    def test_a_rejected_argument_is_not_a_sold_out_room(self):
        d = t.diagnose(run("failed"), [action(
            "get_group_rates", "refused",
            "get_group_rates arguments rejected — units: Field required")])
        assert d.category == t.BUSINESS_RULE_FAILURE

    def test_a_plain_escalation_is_not_an_error(self):
        """
        The guest asked for a person and got one. `why` says so rather than
        leaving an empty category to be guessed at.
        """
        d = t.diagnose(run("escalated"))
        assert d.category == t.NONE
        assert "handed to a person" in d.why
        assert not d

    def test_but_an_escalation_the_arbiter_turned_down_is(self):
        d = t.diagnose(run("escalated"), [action(
            "request_human_handoff", "refused",
            "HANDOFF_NOT_SUPPORTED: the model claimed UNSUPPORTED_REQUEST on a "
            "turn the system reads as QUOTE_REQUIRED")])
        assert d.category == t.HANDOFF_ERROR
        assert d.owner == "services/ai/arbiter"

    def test_and_so_is_one_carrying_a_rejected_judgement(self):
        d = t.diagnose(run("escalated", required_actions={
            "handoff_judgements": [{"valid": False, "why": "no ask for a person"}]}))
        assert d.category == t.HANDOFF_ERROR


class TestTheOrderIsMostSpecificFirst:
    def test_a_guardrail_block_outranks_everything(self):
        """
        The one category that is the system working. A turn can be a guardrail
        block *and* an escalation — it is always both, because a refused reply
        hands over — and filing it as the escalation would lose the cause.
        """
        d = t.diagnose(run("escalated", "guardrail: reply quoted ['10200']"),
                       [action("get_quote", "executed")])
        assert d.category == t.GUARDRAIL_BLOCK

    def test_an_upstream_timeout_is_not_a_bug_in_the_tool_that_hit_it(self):
        d = t.diagnose(run("failed"), [action("get_quote", "failed",
                                              error="ReadTimeout")])
        assert d.category == t.INTEGRATION_ERROR

    def test_a_tool_refusal_outranks_the_run_row(self):
        """
        A refusal carries its own reason, and that reason is better evidence
        than anything the run row has.
        """
        d = t.diagnose(run("escalated"), [action(
            "create_booking", "rejected", "autonomy level 2 is below floor 3")])
        assert d.category == t.AUTONOMY_ERROR

    def test_an_unmet_requirement_is_named_rather_than_folded_away(self):
        """
        The number the whole required-action layer exists to move. Folding it
        into a generic failure would hide it.
        """
        d = t.diagnose(run("succeeded", required_action_status="unmet",
                           required_actions={"unmet": ["negotiate_rate"]}))
        assert d.category == t.TOOL_SELECTION_ERROR
        assert "negotiate_rate" in d.why


class TestTheEvidenceIsAlwaysQuoted:
    @pytest.mark.parametrize("row,acts", [
        (run("failed", "the model produced no reply (finishReason=STOP)"), None),
        (run("escalated", "guardrail: reply quoted ['4000']"), None),
        (run("failed"), [action("get_quote", "failed",
                                error="409: Those dates are no longer available")]),
    ])
    def test_a_category_without_its_sentence_is_an_assertion(self, row, acts):
        d = t.diagnose(row, acts)
        assert d.why, "a diagnosis nobody can disagree with is not evidence"
        assert d.category in t.ALL


class TestBothRowShapes:
    def test_it_reads_a_dataclass_as_well_as_a_dict(self):
        """
        The evaluation holds `RunResult` and `ToolOutcome` objects; a dashboard
        query holds dicts. Neither should have to convert to ask this.
        """
        from app.services.ai.schemas import RunResult, ToolOutcome

        result = RunResult(run_id="r", status="failed", reply=None,
                           error="the model produced no reply",
                           actions=[ToolOutcome(tool="get_quote", status="executed")])
        assert t.diagnose(result).category == t.MODEL_FAILURE


class TestTally:
    def test_the_order_is_fixed_so_two_runs_can_be_read_side_by_side(self):
        rows = [
            (run("failed", "the model produced no reply"), None),
            (run("escalated"), None),
            (run("failed", "the model produced no reply"), None),
        ]
        counts = t.tally(rows)
        assert counts == {t.MODEL_FAILURE: 2, t.NONE: 1}
        assert list(counts) == [name for name in t.ALL if name in counts]

    def test_every_category_has_an_owner(self):
        """
        The second question after "what kind" is always "whose problem", and
        answering it here stops every triage rediscovering the mapping.
        """
        assert set(t.OWNER) == set(t.ALL)
        assert all(t.OWNER[c] for c in t.ALL if c != t.NONE)
