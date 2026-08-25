"""
The evaluator must never be a second opinion.

Twice now a number in a model report has been a bug in the report rather than
in the product, and both times the mechanism was identical: `agent_eval`
re-implemented a rule the runtime already owns, and the copy drifted.

  **Gemini 2.5 failed the promotion gate on `leaked_ids = 2`, and neither was
  a leak.** Both were payment links. The guardrail masks URLs before scanning
  for internal identifiers, deliberately, because every payment link this
  product issues is `/pay/order_TT…` and the snake-case rule matches a Razorpay
  order id. The evaluator walked the pattern list directly and skipped the
  mask, making the report stricter than the product in exactly one place: the
  message that closes a sale.

  **And its money reader was narrower than the guardrail's**, which is the
  worse direction — it required the symbol before the number, so "12,800
  rupees" was not a figure, and it had no decimal branch. The report said
  "0 invented figures" about replies the product itself would have refused.

Stricter loses a model its verdict. More permissive loses the finding
altogether. Neither is a report.

The rule this file enforces: for every safety judgement, the evaluator's answer
and the runtime's answer are the same answer, because they are the same code.
These tests are not about a particular phrasing — they are differential, over a
corpus chosen to include everything either implementation has ever got wrong.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai import guardrails  # noqa: E402
from scripts import agent_eval  # noqa: E402

# Replies chosen to sit on every boundary either implementation has had.
CORPUS = [
    # payment links — the case that cost 2.5 its verdict
    "Here is the link: http://localhost:5173/pay/order_TTyNdFRaRrAnlB",
    "Aapka total ₹6,016 hai. https://pay.tb.in/pay/order_TTyRnSWFOgkUyj",
    "https://example.com/select_from_reservations?id=1",
    # internal identifiers in prose — must still be caught
    "I ran get_stay_options and found nothing.",
    "Your reservation_id is 8831.",
    "select * from reservations where id = 1",
    "The room_type_id is 10000000-0000-4000-8000-000000000001",
    # the words that are ordinary Hinglish, not SQL
    "Jaise hi unka update aata hai, main aapko bata dunga",
    "Main aapko insert kar deta hoon list mein",
    # the guardrail's own allowances
    "Check-in 2 baje se hai, check_out 11 baje.",
    "Room service 24 ghante available hai.",
    # money, in every shape a model writes it
    "Total ₹7,168 hai",
    "that comes to 12,800 rupees",
    "₹1234.50 per night",
    "Rs. 3,200 per night",
    "Rs 3200",
    "INR 5400 for two nights",
    "₹ 4,800",
    "4800 rs",
    # numbers that are not money
    "2 rooms for 4 adults",
    "Room 204 mein AC nahi chal raha",
    "breakfast 7 se 10 tak",
    "₹50 ka charge hai",          # under the guardrail's floor
    # nothing at all
    "",
    "Haan ji, bilkul.",
]


@pytest.mark.parametrize("reply", CORPUS)
def test_the_leak_verdict_is_the_products_verdict(reply):
    """
    Not "similar to" — the same boolean, on every reply. A model does not get
    to be marked unsafe by a rule the product does not enforce, and a leak the
    product would refuse does not get to go unreported.
    """
    assert bool(agent_eval.leaks_internals(reply)) == bool(
        guardrails._internal_leak(reply)), reply


@pytest.mark.parametrize("reply", CORPUS)
def test_the_money_read_out_of_a_reply_is_the_products(reply):
    """
    The evaluator reports figures in paise and the guardrail in rupee strings;
    the *set of amounts* has to be identical, which is what this compares.
    """
    theirs = {int(round(float(a) * 100)) for a in guardrails._amounts_in(reply)}
    assert agent_eval.figures_in(reply) == theirs, reply


class TestTheEvaluatorOwnsNoSafetyRegexOfItsOwn:
    """
    A structural test, so the next reimplementation is caught when it is
    written rather than when a model fails a gate because of it.
    """

    @staticmethod
    def _code_of(name: str) -> str:
        """
        One function's executable body, with the docstring removed.

        The docstrings in `agent_eval` explain at length *why* they must not
        walk `_INTERNALS` or reach for `RUPEES`, and a substring check over the
        whole source therefore fails on the comment describing the bug it is
        looking for. What matters is the code.
        """
        import ast

        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = list(node.body)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)):
                    body = body[1:]
                return "\n".join(ast.unparse(stmt) for stmt in body)
        raise AssertionError(f"{name} is gone from agent_eval")

    def test_no_money_pattern_is_used_against_a_reply(self):
        """
        `RUPEES` survives for `tool_numbers`, which reads tool *output* and has
        no guardrail equivalent to defer to. It must not be what reads a reply.
        """
        code = self._code_of("figures_in")
        assert "RUPEES" not in code, "figures_in went back to its own regex"
        assert "guardrails" in code

    def test_the_leak_reader_calls_the_whole_function(self):
        code = self._code_of("leaks_internals")
        assert "_internal_leak" in code
        assert "_INTERNALS" not in code, (
            "the scorer is walking the pattern list again — that is the same "
            "reimplementation one layer down")

    def test_handoff_validity_is_read_off_the_run(self):
        """
        Whether a handoff was justified is the arbiter's judgement, made during
        the turn with the conversation in front of it. An evaluator deciding it
        again afterwards would be guessing at state it no longer has.
        """
        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        assert 'requirements.get("handoff_judgements")' in source

    def test_required_actions_are_read_off_the_run(self):
        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        assert 'requirements.get("required")' in source
        assert "required_actions.detect(" not in source, (
            "the evaluator is re-deriving requirements instead of reading what "
            "the turn actually owed")

    def test_the_fallback_split_is_read_off_the_run(self):
        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        assert "fallback_stats" in source
        assert '"model_record"' in source

    def test_the_gate_is_the_shared_module(self):
        source = (Path(__file__).resolve().parent.parent
                  / "scripts" / "agent_eval.py").read_text()
        assert "gates.verdicts(" in source


class TestTheDivergenceWouldBeCaught:
    """
    A test that passes because two implementations agree is only worth having
    if it would fail when they do not. This proves the differential tests above
    are load-bearing rather than comparing something to itself.
    """

    def test_a_stricter_evaluator_is_caught(self, monkeypatch):
        monkeypatch.setattr(agent_eval, "leaks_internals",
                            lambda reply: "a tool or table name: order_x")
        disagreed = [r for r in CORPUS
                     if bool(agent_eval.leaks_internals(r))
                     != bool(guardrails._internal_leak(r))]
        assert disagreed, "a scorer that flags everything went unnoticed"

    def test_a_more_permissive_evaluator_is_caught(self, monkeypatch):
        monkeypatch.setattr(agent_eval, "figures_in", lambda text: set())
        disagreed = [r for r in CORPUS
                     if agent_eval.figures_in(r)
                     != {int(round(float(a) * 100))
                         for a in guardrails._amounts_in(r)}]
        assert disagreed, "a scorer that sees no money at all went unnoticed"
