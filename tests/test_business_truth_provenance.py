"""
Every business fact the model can see, and whether it has to work anything out.

**The principle, from the money pass.** Thirty guardrail-blocked production
replies were traced to the model *rendering* or *deriving* a figure rather than
inventing one: `balancePaise: 3440640` with nothing saying `₹34,406`, and a
`depositPct: 30` beside a total with nothing saying `₹19,784`. The figures were
right. The arithmetic was the model's, and arithmetic one keystroke from
fabrication.

`money.with_display` closed the rendering half at the single door. This file is
the audit §5 asks for over the rest — price, deposit, discount, refund, balance,
payment status, booking status, availability, inventory, quote, reservation
reference — expressed as one mechanical property rather than eleven opinions:

    **A tool result must never contain a percentage and a total without also
    containing their product.**

That is the exact shape of both fabrications, generalised. A percentage on its
own is a quotable fact — "we give 8% off for three rooms or more" is true and
safe to say. A percentage *next to a total* is a multiplication waiting to
happen.
"""
import inspect
import re

import pytest

from app.services.ai import registry
from app.services.pms import money
from tests.test_golden_conversations import db  # noqa: F401 — the hotel fixture

registry.load_all()

# A percentage the model can see, and a rupee total it can see. Both in one
# payload is the shape that produced ₹19,784 and ₹16,803.
PCT_FIELD = re.compile(r"[Pp]ct$")
# The *base* a percentage would be taken of. Deliberately narrow: `balance` and
# `amount` are themselves derived from a total, and pairing a percentage with
# them flagged "30% of the balance" — a product nobody has ever wanted and which
# no model would write. A detector that reports impossible pairs is one somebody
# switches off.
TOTAL_FIELD = re.compile(r"([Tt]otal|[Gg]ross)")


def multipliable(payload: dict) -> list[tuple[str, str]]:
    """
    Percentage/total pairs in one payload whose product is not also present.

    A pair whose product *is* present is fine — that is exactly what the group
    deposit fix did: `depositPct: 30`, `totalPaise: 6594560` and
    `deposit: "₹19,784"` together leave nothing to work out.
    """
    if not isinstance(payload, dict):
        return []
    pcts = {k: v for k, v in payload.items()
            if PCT_FIELD.search(k) and isinstance(v, (int, float))}
    totals = {k: v for k, v in payload.items()
              if TOTAL_FIELD.search(k) and isinstance(v, int) and not isinstance(v, bool)}
    if not pcts or not totals:
        return []

    present = {v for v in payload.values() if isinstance(v, (int, float))}
    present |= {str(v) for v in payload.values() if isinstance(v, str)}

    # **A percentage is satisfied when its product with *any* base is present**,
    # not with every one.
    #
    # The first version required the product against each total-like field, and
    # a live group quote failed it on `depositPct × grossPaise` — thirty per
    # cent of the pre-tax gross, a number nobody wants and no model would write,
    # while `depositPaise` (thirty per cent of the *total*) sat right beside it.
    #
    # The question the rule is actually asking is "has this percentage been
    # resolved into rupees anywhere in this payload". If it has, the model has
    # what it needs and nothing is left to work out.
    unresolved = []
    for pct_name, pct in pcts.items():
        resolved = any(
            round(total * pct / 100) in present
            or money.display(round(total * pct / 100)) in present
            for total in totals.values()
        )
        if not resolved:
            unresolved.append((pct_name, sorted(totals)[0]))
    return unresolved


class TestThePropertyItself:
    def test_a_percentage_beside_a_total_with_no_product_is_flagged(self):
        """The ₹19,784 payload, before it was fixed."""
        assert multipliable({"depositPct": 30, "totalPaise": 6594560})

    def test_and_is_not_flagged_once_the_product_is_there(self):
        """The same payload after — which is what the fix did."""
        assert not multipliable({
            "depositPct": 30, "totalPaise": 6594560,
            "depositPaise": 1978368, "deposit": "₹19,784"})

    def test_a_percentage_on_its_own_is_a_quotable_fact(self):
        """
        "We give 8% off for three rooms or more" is true and safe to say. Only
        a percentage *next to a total* is a multiplication waiting to happen.
        """
        assert not multipliable({"discountPct": 8, "groupFrom": 3})

    def test_a_total_on_its_own_is_fine(self):
        assert not multipliable({"totalPaise": 6594560, "total": "₹65,946"})


class TestTheToolsThatReturnBoth:
    """
    The live payloads, built the way the tools build them and put through the
    same door — `registry.execute` runs `money.with_display` on every result, so
    what is checked here is what the model actually sees.
    """

    def test_a_group_quote_hands_over_its_deposit(self):
        priced = money.with_display({
            "totalPaise": 6594560, "depositPct": 30, "depositPaise": 1978368,
            "balancePaise": 4616192})
        assert not multipliable(priced), (
            "a group quote left the model to multiply, which is the ₹19,784 bug")
        assert priced["deposit"] == "₹19,784"
        assert priced["balance"] == "₹46,162"

    def test_a_single_room_quote_hands_over_its_advance(self):
        """
        The second occurrence, one room short of the group guard: a two-room
        quote at a hotel with a 20% token, where the model wrote ₹16,803.
        """
        priced = money.with_display({
            "totalPaise": 8401600, "depositPaise": 1680320,
            "balancePaise": 6721280})
        assert not multipliable(priced)
        assert priced["deposit"] == "₹16,803"

    def test_group_bands_alone_carry_no_total_to_multiply(self):
        """
        `get_group_rates` genuinely cannot price — it has no room type — so it
        returns percentages and no total, which is safe by construction. What
        makes the *turn* safe is that a group enquiry now also forces a quote;
        see `required_actions._quote_requirement`.
        """
        bands = {"discountPct": 8, "depositPct": 30, "groupFrom": 3, "rooms": 5}
        assert not multipliable(bands)
        assert not [k for k in bands if TOTAL_FIELD.search(k)]


class TestEveryFactIsQuotableAsReturned:
    """
    §5's list, each checked for the thing that matters: can the model say it
    without computing?
    """

    @pytest.mark.parametrize("fact,payload,sayable", [
        ("price", {"totalPaise": 1140000}, "total"),
        ("balance", {"balancePaise": 3440640}, "balance"),
        ("deposit", {"depositPaise": 1978368}, "deposit"),
        ("refund", {"refundPaise": 500000}, "refund"),
        ("amount", {"amountPaise": 143360}, "amount"),
    ])
    def test_a_money_fact_arrives_with_the_words_for_it(self, fact, payload, sayable):
        assert money.with_display(payload)[sayable].startswith("₹")

    @pytest.mark.parametrize("payload,field", [
        # Availability is a count, and a count needs no rendering.
        ({"unitsLeft": 4}, "unitsLeft"),
        ({"anyAvailable": True}, "anyAvailable"),
        # A state and a reference are strings the model repeats verbatim.
        ({"state": "CONFIRMED"}, "state"),
        ({"paymentStatus": "token_paid"}, "paymentStatus"),
        ({"ref": "TB-UYDS4B"}, "ref"),
    ])
    def test_a_non_money_fact_is_already_in_a_form_it_can_repeat(self, payload, field):
        """
        These need no conversion, which is why the money rule did not have to
        reach them. Asserted rather than assumed, because a count that started
        arriving as a fraction, or a state as an enum id, would be the same bug
        in a different currency.
        """
        value = money.with_display(payload)[field]
        assert isinstance(value, (int, bool, str))
        assert not isinstance(value, float), (
            f"{field} arrived as a float, which the model would have to round")


class TestTheGuardrailStillBacksAllOfIt:
    def test_a_derived_figure_is_refused_even_now(self):
        """
        The layers are independent on purpose. This one makes the model not
        *need* to derive; the guardrail makes it unable to get away with it.
        """
        from app.services.ai import guardrails
        from app.services.ai.schemas import ToolOutcome

        quoted = ToolOutcome(
            tool="get_quote", status="executed",
            output=money.with_display({"totalPaise": 6594560, "depositPct": 30}))
        assert not guardrails.check_reply("Deposit ₹19,784 dena hoga.",
                                          tool_outputs=[quoted]).ok

    def test_and_the_figure_the_tool_supplies_passes(self):
        from app.services.ai import guardrails
        from app.services.ai.schemas import ToolOutcome

        quoted = ToolOutcome(
            tool="get_quote", status="executed",
            output=money.with_display({"totalPaise": 6594560,
                                       "depositPaise": 1978368}))
        assert guardrails.check_reply("Deposit ₹19,784 dena hoga.",
                                      tool_outputs=[quoted]).ok


class TestAgainstRealToolPayloads:
    """
    **The property applied to what the tools actually return**, not to payloads
    I wrote to demonstrate it. A hand-built example proves the detector works; a
    real one proves the product does.

    `get_quote` is the tool the rule exists for — it is the only one that holds
    a percentage and a total together, and it is where both production
    fabrications happened.
    """

    def test_a_real_group_quote_leaves_nothing_to_multiply(self, db):  # noqa: F811
        import asyncio
        from datetime import date, timedelta

        from app.services.ai import registry as reg
        import tests.test_golden_conversations as golden

        out = asyncio.run(reg.execute("get_quote", {
            "room_type_id": golden.ROOM_TYPE, "rate_plan_id": golden.RATE_PLAN,
            "check_in": str(golden.CHECK_IN), "check_out": str(golden.CHECK_OUT),
            "units": 5, "adults": 10,
        }, golden.ctx_for(db)))
        assert out.ok, out.refusal_reason
        assert out.output.get("depositPct"), "the fixture stopped being a group"
        assert not multipliable(out.output), (
            f"a live group quote left a product to work out: "
            f"{multipliable(out.output)}")

    def test_a_real_single_room_quote_too(self, db):  # noqa: F811
        import asyncio

        from app.services.ai import registry as reg
        import tests.test_golden_conversations as golden

        out = asyncio.run(reg.execute("get_quote", {
            "room_type_id": golden.ROOM_TYPE, "rate_plan_id": golden.RATE_PLAN,
            "check_in": str(golden.CHECK_IN), "check_out": str(golden.CHECK_OUT),
            "units": 1, "adults": 2,
        }, golden.ctx_for(db)))
        assert out.ok, out.refusal_reason
        assert not multipliable(out.output)

    def test_every_money_figure_in_a_live_quote_is_sayable(self, db):  # noqa: F811
        """
        The money rule, checked on a real payload rather than on the converter.
        """
        import asyncio

        from app.services.ai import registry as reg
        import tests.test_golden_conversations as golden

        out = asyncio.run(reg.execute("get_quote", {
            "room_type_id": golden.ROOM_TYPE, "rate_plan_id": golden.RATE_PLAN,
            "check_in": str(golden.CHECK_IN), "check_out": str(golden.CHECK_OUT),
            "units": 2, "adults": 4,
        }, golden.ctx_for(db)))
        for key, value in out.output.items():
            if not key.endswith("Paise") or not isinstance(value, int):
                continue
            spoken = key[: -len("Paise")]
            assert spoken in out.output, (
                f"{key} reached the model with no words for it")
            assert str(out.output[spoken]).startswith("₹")
