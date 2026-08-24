"""
The guardrail was checking the reply and not the message.

**Found in production on 2026-08-24.** A guest asked about five rooms for ten
people. The model did everything right — `get_group_rates`, then
`search_availability`, then `get_stay_options`, then `get_quote` — and then sent
this, through `send_message`:

    …total price ₹65,946 hai (taxes aur breakfast ke sath). Is group booking ke
    liye 30% deposit (₹19,784) abhi pay karna hoga…

₹19,784 is thirty per cent of ₹65,946. No tool returned it. `get_group_rates`
gave `depositPct: 30`, `get_quote` gave the total, and the model multiplied —
which is the one thing §24 rule 3 says it must never do, and precisely what
`guardrails.check_reply` exists to catch.

It *was* caught. The run ends:

    guardrail: reply quoted ['19784'], which no tool result contains

and escalated. But the check ran on `result.reply`, at the end of the turn, and
the message had gone out through the tool sixteen seconds earlier. The guardrail
refused a paraphrase of a number the guest was already looking at, and a person
inherited a conversation containing a deposit figure the hotel never authorised.

`send_message`'s own description calls it "the only way to say anything to
them". That was true, and it meant the primary channel for saying things was the
one channel the check on what may be said did not cover. A price nobody quoted,
a payment link nobody created, a booking nobody made — all of it was reachable
by putting it in `body` instead of in the reply.

This file is that gap closed, and the shape of the fix matters: **the same
function, the same evidence, one turn earlier.** Not a second guardrail with its
own rules to drift out of step with the first.
"""
import asyncio

import pytest

from app.services.ai import registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.providers import EchoProvider
from tests.fake_supabase import FakeDb

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
        "city": "Anjuna", "timezone": "Asia/Kolkata", "currency": "INR",
        "policies": {}, "tax_inclusive": False,
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
    })
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in ("app.services.ai.runtime", "app.services.ai.context",
                   "app.services.ai.prompts", "app.services.ai.usage",
                   "app.services.ai.tools.communication",
                   "app.services.messaging.service",
                   "app.services.messaging.pipeline",
                   "app.services.billing.service", "app.services.pms.clock"):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)
    return fake


@pytest.fixture
def sent(monkeypatch) -> list:
    """Everything that actually reached the guest."""
    out: list = []

    class Sent:
        def __init__(self, body):
            self.message = {"id": f"m-{len(out)}"}
            self.duplicate = False

    async def _send(conversation, **kwargs):
        out.append(kwargs.get("body"))
        return Sent(kwargs.get("body"))

    monkeypatch.setattr(
        "app.services.ai.tools.communication.messaging.send", _send)
    return out


@pytest.fixture
def priced(monkeypatch):
    """
    `get_quote` answering with what it answered in production.

    The pricing engine needs seasonal rates, rate plans, inventory and a hold to
    produce a number, and none of that is what this file is about. What it is
    about is whether a figure in an outbound message is traceable to a tool
    result — so the tool result is the fixture, and it is the real one, copied
    from the run that exposed the gap.

    Everything else, `send_message` included, goes through `registry.execute`
    untouched: the path under test is not stubbed.
    """
    from app.services.ai.schemas import ToolOutcome

    real = registry.execute

    async def _execute(name, arguments, ctx, **kwargs):
        if name != "get_quote":
            return await real(name, arguments, ctx, **kwargs)
        return ToolOutcome(
            tool="get_quote", status="executed", input=arguments,
            output={
                "rooms": 5, "nights": 4, "total": "₹65,946", "totalPaise": 6594560,
                "grossPaise": 5888000, "taxPaise": 706560, "currency": "INR",
                "roomName": "Garden Standard",
                "quoteId": "5b52681f-72cd-4671-8703-0dde71888979",
                "holdId": "96d6ede7-074d-4e46-b597-05596bb68dda",
                "checkIn": "2026-09-13", "checkOut": "2026-09-17",
                "instruction": "Quote `total` exactly as written and nothing else.",
            },
            result_type="quote",
        )

    monkeypatch.setattr(runtime.registry, "execute", _execute)
    return _execute


def ctx_for() -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"messaging", "rates", "tasks", "bookings"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


# The message as it was actually sent, shortened to the part that matters.
THE_PRODUCTION_MESSAGE = (
    "13 se 17 September ke liye 5 Garden Standard rooms ka total price ₹65,946 "
    "hai (taxes aur breakfast ke sath). Is group booking ke liye 30% deposit "
    "(₹19,784) abhi pay karna hoga."
)


# ── the message that went out ───────────────────────────────────────────────

def test_the_deposit_the_model_worked_out_never_reaches_the_guest(db, sent, priced):
    """
    The turn as it happened: a real quote, and a deposit the model multiplied.

    `get_quote` is scripted to return the total the production run returned, so
    ₹65,946 is legitimate and traceable and ₹19,784 is neither.
    """
    provider = EchoProvider([
        [("get_quote", {"room_type_id": "10000000-0000-4000-8000-000000000001",
                        "rate_plan_id": "20000000-0000-4000-8000-000000000001",
                        "check_in": "2026-09-13", "check_out": "2026-09-17",
                        "units": 5, "adults": 10, "children": 0})],
        [("send_message", {"body": THE_PRODUCTION_MESSAGE})],
        "Bata diya hai.",
    ])
    run(runtime.run_turn(
        ctx_for(), "Actually 5 kamre chahiye, 10 log aa rahe hain",
        provider=provider,
    ))

    assert not any("19,784" in (m or "") for m in sent), (
        "a deposit figure the model calculated itself was delivered to a guest "
        "on WhatsApp — somebody now has to honour it or take it back"
    )


def test_the_refusal_tells_the_model_what_to_do_about_it(db, sent):
    """
    A refusal the model cannot act on becomes an apology, or a handoff. This one
    names the rule and the remedy: call the tool that produces the number, or
    leave it out.
    """
    provider = EchoProvider([
        [("send_message", {"body": "Deposit ₹19,784 hoga."})],
        "Theek hai.",
    ])
    run(runtime.run_turn(ctx_for(), "5 kamre chahiye", provider=provider))

    shown = [m for step in provider.seen_messages for m in step
             if m.get("role") == "tool" and m.get("name") == "send_message"]
    assert shown
    content = shown[-1]["content"]
    assert "NOT SENT" in content
    assert "Do not calculate" in content


def test_the_action_row_records_the_refusal(db):
    """§21. A message that was stopped is an event, not an absence."""
    provider = EchoProvider([
        [("send_message", {"body": "Deposit ₹19,784 hoga."})],
        "Theek hai.",
    ])
    run(runtime.run_turn(ctx_for(), "5 kamre chahiye", provider=provider))

    rows = [a for a in db.rows("agent_actions") if a["tool"] == "send_message"]
    assert rows and rows[0]["status"] == "refused"
    assert "guardrail" in (rows[0]["refusal_reason"] or "")


# ── what must still get through ─────────────────────────────────────────────

def test_a_figure_a_tool_produced_is_sent(db, sent, priced):
    """
    The half that would be a much worse regression than the bug. If this check
    refused honest messages, the agent would stop being able to quote anything —
    and the failure would look like the model getting worse.
    """
    provider = EchoProvider([
        [("get_quote", {"room_type_id": "10000000-0000-4000-8000-000000000001",
                        "rate_plan_id": "20000000-0000-4000-8000-000000000001",
                        "check_in": "2026-09-13", "check_out": "2026-09-17",
                        "units": 5, "adults": 10, "children": 0})],
        [("send_message", {"body": "Total ₹65,946 hai, 15 minute ke liye hold."})],
        "Bata diya.",
    ])
    run(runtime.run_turn(ctx_for(), "5 kamre chahiye", provider=provider))

    assert sent, "an honest quote was refused"
    assert "65,946" in sent[0]


def test_a_message_with_no_figures_in_it_is_never_touched(db, sent):
    """The common case, and the one this must cost nothing on."""
    provider = EchoProvider([
        [("send_message", {"body": "Ji bilkul, main abhi check karti hoon."})],
        "Ho gaya.",
    ])
    run(runtime.run_turn(ctx_for(), "photos bhejo", provider=provider))

    assert sent == ["Ji bilkul, main abhi check karti hoon."]


def test_the_check_uses_the_same_function_as_the_reply_check(db):
    """
    One definition of what may be said, not two.

    A second guardrail with its own rules is a second set of rules to drift out
    of step with the first — and the drift would be invisible, because each one
    would keep passing its own tests.
    """
    import ast
    import inspect

    from app.services.ai.tools import communication

    source = inspect.getsource(communication._refuse_what_may_not_be_said)
    calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Call)}
    assert any("check_reply" in c for c in calls), (
        "the outbound check does not go through guardrails.check_reply"
    )


def test_it_is_the_tools_that_ran_before_this_one_that_count(db, sent, priced):
    """
    A figure has to come from something that already happened. Evidence from
    *this* call would be circular — the model would be authorising its own
    number by putting it in the message.
    """
    provider = EchoProvider([
        [("send_message", {"body": "Total ₹65,946 hai."})],
        [("get_quote", {"room_type_id": "10000000-0000-4000-8000-000000000001",
                        "rate_plan_id": "20000000-0000-4000-8000-000000000001",
                        "check_in": "2026-09-13", "check_out": "2026-09-17",
                        "units": 5, "adults": 10, "children": 0})],
        "Ho gaya.",
    ])
    run(runtime.run_turn(ctx_for(), "5 kamre chahiye", provider=provider))

    assert not sent, (
        "a price was sent before any tool had produced one, and the quote that "
        "arrived afterwards was read as retrospective authorisation"
    )


# ── and the reason the model was multiplying ────────────────────────────────
#
# Stopping the message is the safety half. It leaves the guest without a
# deposit figure, which is a worse conversation than the one the hotel wanted
# even if it is a much better one than a wrong number.
#
# The model was multiplying because nothing gave it the answer.
# `get_group_rates` returns `depositPct: 30`, `get_quote` returns the total, and
# a percentage and a total together in a model's context is a multiplication
# waiting to happen. §24 rule 2 again: the fix for that is not a firmer
# instruction, it is the number.

class TestTheQuoteCarriesItsOwnDeposit:

    def test_a_group_quote_says_what_the_deposit_is(self):
        """
        In the shape the model is allowed to quote — a string it copies, beside
        the paise it must never convert. The same pattern as `total`.
        """
        from app.services.pms import money

        total_paise = 6594560
        deposit_paise = round(total_paise * 30 / 100)
        assert money.display(deposit_paise) == "₹19,784"
        assert money.display(total_paise - deposit_paise) == "₹46,162"

    def test_the_figure_the_model_reached_for_is_now_one_a_tool_produced(self):
        """
        ₹19,784 was refused because no tool result contained it. With the quote
        carrying its own deposit, the identical sentence passes — which is the
        point: the guest was owed that number, and the objection was never to
        the number but to where it came from.
        """
        from app.services.ai import guardrails
        from app.services.ai.schemas import ToolOutcome

        quoted = ToolOutcome(
            tool="get_quote", status="executed",
            output={"total": "₹65,946", "totalPaise": 6594560,
                    "deposit": "₹19,784", "depositPaise": 1978368,
                    "balance": "₹46,162", "depositPct": 30},
        )
        verdict = guardrails.check_reply(
            THE_PRODUCTION_MESSAGE, tool_outputs=[quoted], trusted_hosts=set(),
        )
        assert verdict.ok, verdict.reason

    def test_a_single_room_quote_carries_no_deposit_at_all(self):
        """
        Absent rather than zero. A `deposit: ₹0` on every quote is a field the
        model would eventually mention to somebody who is booking one room.
        """
        import ast
        import inspect

        from app.services.ai.tools import pricing

        source = inspect.getsource(pricing)
        tree = ast.parse(source)
        # The block is guarded on the booking actually being a group.
        guarded = [
            ast.unparse(node.test) for node in ast.walk(tree)
            if isinstance(node, ast.If) and "deposit" in ast.unparse(node).lower()
        ]
        assert any("min_units" in g and "units" in g for g in guarded), (
            "the deposit is not conditional on the booking being a group"
        )


# ── the path a human has already approved ───────────────────────────────────

class TestTheApprovalPath:
    """
    **A regression this check would have caused, found by asking who else calls
    `registry.execute`.**

    `approvals.execute_action` runs a tool a human queued and then approved, on
    a context built outside `run_turn`. `ctx.turn` is therefore empty — and to a
    check that reads `turn.outcomes` for its evidence, an empty turn looks
    exactly like a turn on which no tool has run. Every figure in the message
    would be refused.

    At autonomy 1 the agent *proposes* its messages and a person approves them.
    That is the configuration the most careful hotels run in, and prices are
    exactly what those messages are about — so the check would have broken
    outbound messaging for precisely the hotels that asked for the most
    supervision.

    "There is no evidence" and "the evidence says no" are opposite answers, and
    they were about to get the same one. `TurnState.live` is the distinction,
    and a person who has read the message and pressed approve is a better
    authority on its contents than this function.
    """

    def test_a_context_outside_a_turn_is_not_second_guessed(self):
        from app.services.ai.tools import communication

        ctx = ctx_for()
        assert ctx.turn.live is False
        # A price with no tool behind it, which a live turn would refuse.
        run(communication._refuse_what_may_not_be_said("Total ₹65,946 hai.", ctx))

    def test_a_live_turn_still_is(self):
        import pytest as _pytest

        from app.services.ai.errors import OutOfScope
        from app.services.ai.tools import communication

        ctx = ctx_for()
        ctx.turn.live = True
        with _pytest.raises(OutOfScope):
            run(communication._refuse_what_may_not_be_said("Total ₹65,946 hai.", ctx))

    def test_the_runtime_is_what_marks_a_turn_live(self, db, sent):
        """
        Asserted through a real turn rather than by reading the flag, so the two
        cannot drift: if `run_turn` stopped setting it, the check would silently
        stop running on every guest message and every test above would still
        pass.
        """
        provider = EchoProvider([
            [("send_message", {"body": "Deposit ₹19,784 hoga."})],
            "Theek hai.",
        ])
        run(runtime.run_turn(ctx_for(), "5 kamre chahiye", provider=provider))
        assert sent == [], "a live guest turn did not get the outbound check"
