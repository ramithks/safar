"""
Every paise a model can see, beside the words for it.

**Thirty guardrail-blocked production runs, classified.** Each one is a reply the
guardrail refused because it carried a rupee figure no tool result contained, and
the largest coherent class was not the model inventing a number. It was the model
*rendering* one:

    get_booking_status → {"balancePaise": 3440640, "totalPaise": 4300800}
    the model wrote    → "₹34,406.40 abhi baaki hai"
    the guardrail saw  → 34406.4, which no tool result contains

The figure was right. 3,440,640 paise *is* ₹34,406.40. What the model had to
invent was the rendering — and it rendered the paise as decimals, which is not
how this hotel writes money and not what the guardrail extracts. The tool's own
instruction made it worse: *"If balancePaise is above zero, say that amount is
due at the hotel"* told the model, in so many words, to say a paise field out
loud.

Inventing a rendering is one keystroke from inventing a number, and on the same
thirty runs the model also produced ₹1,05,100.80 and ₹5,734 by dividing and
taking percentages of figures it had been handed raw.

**`get_quote` was fixed by hand after the first of these.** A survey of the rest
found `frontdesk.py` with ten paise fields and no display calls at all, and
`sales.py` with four. Fixing them one at a time would leave the next tool
somebody writes in exactly the same state.

So it happens once, at the single door: `registry.execute` puts every result
through `money.with_display` on the way out. This file is the rule that keeps it
that way — and neither half of it touches the guardrail, which is unchanged.
"""
import inspect
import json
import re

import pytest

from app.services.ai import registry
from app.services.ai.tools import (  # noqa: F401 — imported so the source is loadable
    booking, communication, frontdesk, hotel, marketing, operations, owner,
    pricing, read, sales, stay,
)
from app.services.pms import money

registry.load_all()

TOOL_MODULES = (booking, communication, frontdesk, hotel, marketing, operations,
                owner, pricing, read, sales, stay)

# `balancePaise`, `totalPaise`, `agreedNightlyPaise` — the shape the rule is about.
PAISE_FIELD = re.compile(r'"([a-zA-Z]+Paise)"')


class TestTheConverterItself:
    def test_it_names_the_sibling_after_the_field(self):
        assert money.with_display({"balancePaise": 3440640})["balance"] == "₹34,406"
        assert money.with_display({"totalPaise": 4300800})["total"] == "₹43,008"

    def test_it_leaves_a_tool_that_formatted_its_own_money_alone(self):
        """
        A group deposit and a cancellation quote already write their own
        wording, and overwriting it would undo a sentence somebody chose.
        """
        kept = money.with_display({"depositPaise": 1978368, "deposit": "30% — ₹19,784"})
        assert kept["deposit"] == "30% — ₹19,784"

    def test_it_reaches_money_nested_two_levels_down(self):
        """
        `get_stay_options` carries its money inside `options[].ratePlans[]`, and
        that is exactly where the model was reaching when it produced
        ₹1,05,100.80.
        """
        out = money.with_display({
            "options": [{"ratePlans": [{"totalPaise": 6256000, "perNightPaise": 312800}]}]
        })
        plan = out["options"][0]["ratePlans"][0]
        assert plan["total"] == "₹62,560"
        assert plan["perNight"] == "₹3,128"

    def test_a_float_in_a_paise_field_is_left_to_be_noticed(self):
        """
        A float there is already a bug — this module is integer paise
        throughout — and rendering it politely would hide one.
        """
        assert "balance" not in money.with_display({"balancePaise": 34406.4})

    def test_a_boolean_is_not_money(self):
        assert "refunded" not in money.with_display({"refundedPaise": True})

    def test_it_does_not_recurse_forever(self):
        deep = current = {}
        for _ in range(40):
            current["next"] = {"totalPaise": 100}
            current = current["next"]
        money.with_display(deep)  # must return rather than blow the stack


class TestTheDoorAppliesIt:
    def test_the_conversion_happens_in_registry_execute(self):
        """
        At the single door rather than in each tool, which is the difference
        between fixing seven files and fixing the next tool somebody writes.
        """
        source = inspect.getsource(registry.execute)
        assert "money.with_display(result)" in source

    def test_it_runs_before_the_outcome_is_built(self):
        source = inspect.getsource(registry.execute)
        assert source.index("money.with_display(result)") < source.index(
            "status=\"executed\"")


class TestNoInstructionNamesAPaiseField:
    """
    The other half of the same root cause. `get_booking_status` told the model
    *"If balancePaise is above zero, say that amount is due at the hotel"* — an
    instruction to speak a paise figure, which the model followed.
    """

    @pytest.mark.parametrize("module", TOOL_MODULES, ids=lambda m: m.__name__.split(".")[-1])
    def test_no_tool_tells_the_model_to_quote_paise(self, module):
        source = inspect.getsource(module)
        offenders = []
        for line in source.splitlines():
            if not PAISE_FIELD.search(line) and "Paise" not in line:
                continue
            # `state` is deliberately not in this list. It reads as a speech verb
            # and is a variable name everywhere in these modules, which made the
            # rule fire on `state['pricePaise']` — a real thing to fix, but not
            # an instruction to the model.
            spoken = re.search(r"\b(say|quote|tell)\b", line, re.I)
            if spoken and re.search(r"\b\w+Paise\b", line):
                offenders.append(line.strip()[:110])
        assert not offenders, (
            f"{module.__name__} tells the model to speak a paise field:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_booking_status_instruction_names_the_display_field(self):
        # `_GUIDANCE` is module-level, so the function body does not carry it.
        source = inspect.getsource(booking)
        assert "`balance`" in source
        assert "balancePaise is above zero, say" not in source


class TestEveryMoneyToolNowSpeaksRupees:
    """
    The end-to-end property, checked against the shapes the thirty blocked runs
    actually returned rather than against a synthetic payload.
    """

    @pytest.mark.parametrize("payload,expected", [
        # get_booking_status, from the run that produced 34406.4 and 8601.6
        ({"ref": "TB-UYDS4B", "paidPaise": 860160, "totalPaise": 4300800,
          "balancePaise": 3440640},
         {"paid": "₹8,602", "total": "₹43,008", "balance": "₹34,406"}),
        # create_booking_payment_link, from the run that produced 5734
        ({"amountPaise": 143360, "balancePaise": 573440},
         {"amount": "₹1,434", "balance": "₹5,734"}),
        # get_stay_options, from the run that produced 105100.8
        ({"options": [{"ratePlans": [{"totalPaise": 10510080}]}]}, None),
    ])
    def test_the_figures_the_model_reached_for_are_now_handed_to_it(
            self, payload, expected):
        out = money.with_display(payload)
        if expected is None:
            # Western grouping, which is what `money.display` has always used
            # and what every figure already in the database and in the
            # guardrail's matching is written with. Indian grouping would be a
            # better read for this market and is a product decision, not a bug
            # to slip into a reliability pass — changing it here would move
            # every existing figure at once.
            assert out["options"][0]["ratePlans"][0]["total"] == "₹105,101"
            return
        for key, value in expected.items():
            assert out[key] == value

    def test_nothing_it_produces_carries_a_decimal(self):
        """
        The decimals were the tell: `.4`, `.6`, `.8` in a system that is integer
        paise throughout. A hotel quotes in rupees, and "₹34,406.40" on
        Instagram reads like a machine doing arithmetic — which is exactly what
        it was.
        """
        out = money.with_display({"aPaise": 3440640, "bPaise": 860160,
                                  "cPaise": 10510080, "dPaise": 1})
        for key in ("a", "b", "c", "d"):
            assert "." not in out[key], out[key]


class TestTheGuardrailIsUntouched:
    """
    The point of all of the above is that the guardrail should stop firing
    because the model no longer needs to invent anything — not because the rule
    was loosened. Asserted, because "preserve the guardrail unchanged" is the
    kind of promise that decays quietly.
    """

    def test_an_invented_figure_is_still_refused(self):
        from app.services.ai import guardrails
        from app.services.ai.schemas import ToolOutcome

        quoted = ToolOutcome(tool="get_booking_status", status="executed",
                             output=money.with_display({"totalPaise": 4300800}))
        assert not guardrails.check_reply("Aapka total ₹50,000 hai.",
                                          tool_outputs=[quoted]).ok

    def test_and_the_figure_the_tool_now_hands_over_passes(self):
        from app.services.ai import guardrails
        from app.services.ai.schemas import ToolOutcome

        quoted = ToolOutcome(
            tool="get_booking_status", status="executed",
            output=money.with_display({"totalPaise": 4300800, "paidPaise": 860160,
                                       "balancePaise": 3440640}))
        reply = ("Aapka total ₹43,008 hai, ₹8,602 mil chuke hain aur ₹34,406 "
                 "hotel par dena hoga.")
        verdict = guardrails.check_reply(reply, tool_outputs=[quoted])
        assert verdict.ok, verdict.reason
