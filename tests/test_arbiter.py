"""
A handoff is a claim about the conversation, and claims are checkable.

The regression that produced this file looked like a model regression and was
reported as one. A model that reached for `request_human_handoff` more readily
than its predecessor turned revenue turns into "somebody will get back to you",
and the obvious remedy — put the old model back — would have left the system in
the state where *any* model can end a sale by asserting something untrue about
the guest.

So what is asserted here is not "3.x behaves". It is that the system no longer
depends on it behaving:

  * a reason the conversation contradicts is turned down,
  * a reason the conversation supports goes through,
  * a stop is never prevented, only ever delayed by one step,
  * safety stops are never inspected at all, and
  * nothing here executes anything or moves an autonomy gate.

The last two are the ones that would make this layer worse than the bug it
fixes, and they are asserted as hard as the fix itself.
"""
import pytest

from app.services.ai import arbiter
from app.services.ai.context import AgentContext, AgentPrincipal, TurnState
from app.services.ai.schemas import ToolOutcome
from app.services.ai.tools.communication import REASONS

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

GRANTED = ("negotiate_rate", "get_quote", "get_group_rates", "get_stay_options",
           "request_human_handoff", "escalate_to_team", "get_cancellation_quote",
           "get_booking_status", "create_booking_payment_link")


def ctx_for(message: str, *, session: dict | None = None, autonomy: int = 3,
            outcomes: list | None = None, granted=GRANTED) -> AgentContext:
    from app.services.ai import required_actions

    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=autonomy,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    ctx = AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in granted},
    )
    ctx.turn = TurnState(
        message=message,
        requirements=tuple(required_actions.detect(message, session or {})),
        outcomes=list(outcomes or []),
    )
    return ctx


def ran(tool: str, status: str = "executed") -> ToolOutcome:
    return ToolOutcome(tool=tool, status=status)


PRICED = {
    "check_in": "2026-12-01", "check_out": "2026-12-03",
    "adults": 2, "children": 0, "units": 1,
    "rate_plan_id": "20000000-0000-4000-8000-000000000002",
    "room_type_id": "10000000-0000-4000-8000-000000000001",
    "stage": "quoted",
}


# ── the failure this exists for ─────────────────────────────────────────────

def test_the_sentence_that_lost_the_sale():
    """
    *"Best price kya de sakte ho?"* is a guest haggling. It is not a guest
    asking for a person, and the model saying it was does not make it so.
    """
    judgement = arbiter.judge("guest_asked", ctx_for(
        "Best price kya de sakte ho?", session=PRICED,
    ))
    assert judgement.rejected
    assert judgement.category == arbiter.GUEST_REQUESTED_HUMAN
    assert judgement.instead == "negotiate_rate"
    assert judgement.intent == "NEGOTIATION"


@pytest.mark.parametrize("message", [
    "Best price kya de sakte ho?",
    "Yaar thoda kam karo na, 4000 per night mein?",
    "Can you do better on the price?",
    "koi discount milega?",
])
def test_a_haggle_is_never_a_request_for_a_human(message):
    assert arbiter.judge("guest_asked", ctx_for(message, session=PRICED)).rejected


def test_the_model_is_told_which_tool_to_call():
    """
    A refusal the model cannot act on becomes an apology to the guest. The
    instruction names the tool, says nothing was handed over, and says the
    handoff is still available — all three, because a model that thinks it is
    trapped spends the turn trying to escape rather than answering.
    """
    judgement = arbiter.judge("guest_asked", ctx_for(
        "Best price kya de sakte ho?", session=PRICED))
    text = arbiter.instruction_for(judgement)
    assert "negotiate_rate" in text
    assert "not handed over" in text.lower()
    assert "again" in text.lower()


# ── what must still go through ──────────────────────────────────────────────

@pytest.mark.parametrize("message", [
    "I want to talk to a human",
    "manager se baat karao",
    "kisi se baat karni hai",
    "Can I speak to a person please",
    "call me back",
])
def test_a_guest_who_asks_for_a_person_gets_one(message):
    """
    The half that would be a far worse regression than the bug. Derived from
    `required_actions._ESCALATION_ASK` — the same detector that *forces* a
    handoff — so a sentence that would force one can never be one that fails to
    justify one.
    """
    judgement = arbiter.judge("guest_asked", ctx_for(message, session=PRICED))
    assert judgement.valid, judgement.why


def test_asking_for_the_manager_while_haggling_still_reaches_the_manager():
    """The compound message, which is the one a narrow rule gets wrong."""
    judgement = arbiter.judge("guest_asked", ctx_for(
        "4000 mein kar do, warna manager se baat karao", session=PRICED))
    assert judgement.valid


@pytest.mark.parametrize("reason", ["complaint"])
def test_a_safety_stop_is_never_inspected(reason):
    """
    §20. Somebody upset, or money in dispute, goes to a person on the model's
    word alone — even on a turn with an unmet negotiation behind it, which is
    exactly the shape this layer is otherwise built to turn down.
    """
    judgement = arbiter.judge(reason, ctx_for(
        "This is unacceptable, best price kya hai", session=PRICED))
    assert judgement.valid
    assert judgement.category == arbiter.SAFETY_ESCALATION


@pytest.mark.parametrize("reason", ["refund", "policy_conflict"])
def test_a_hotels_own_policy_is_not_second_guessed(reason):
    """
    §24 rule 9. Whether a refund conversation goes to a person is the hotel's
    decision, and a layer that overrode it would be changing a customer's
    configuration to improve its own numbers.
    """
    judgement = arbiter.judge(reason, ctx_for(
        "I want a refund, and what is your best price", session=PRICED))
    assert judgement.valid
    assert judgement.category == arbiter.POLICY_REQUIRES_HUMAN


def test_a_handoff_with_nowhere_better_to_go_stands():
    """
    The conservative default, and the reason this is safe to turn on. An
    uncorroborated reason is *not* by itself grounds to refuse — the model may
    be right in a way nothing here can see. It is refused only when the system
    positively knows a business action it could take instead.
    """
    judgement = arbiter.judge("no_knowledge", ctx_for(
        "Do you allow pets in the garden rooms?"))
    assert judgement.valid
    assert "no business action outstanding" in judgement.why


def test_a_tool_the_agent_was_never_granted_is_not_somewhere_better_to_go():
    """Sending the model at a tool it cannot see is a loop, not a fix."""
    judgement = arbiter.judge("guest_asked", ctx_for(
        "Best price kya de sakte ho?", session=PRICED,
        granted=("get_booking_status", "request_human_handoff"),
    ))
    assert judgement.valid


def test_a_tool_already_tried_is_not_somewhere_better_to_go():
    """
    A `negotiate_rate` that came back refused is the *reason* the model is
    stopping. Sending it back there is the loop this must not create.
    """
    judgement = arbiter.judge("guest_asked", ctx_for(
        "Best price kya de sakte ho?", session=PRICED,
        outcomes=[ran("negotiate_rate", "refused")],
    ))
    assert judgement.valid


# ── the termination guarantee ───────────────────────────────────────────────

def test_a_stop_is_delayed_once_and_never_prevented():
    """
    **The property that makes this safe to deploy.** The arbiter's power is to
    make the model try one more thing, and it spends that power once per turn.
    A guest who asked for a person and hit an unlucky detector miss waits one
    extra tool call, not forever.
    """
    ctx = ctx_for("Best price kya de sakte ho?", session=PRICED)
    first = arbiter.judge("guest_asked", ctx)
    assert first.rejected

    # What the precheck does on rejection.
    ctx.turn.handoff_rejected = True

    second = arbiter.judge("guest_asked", ctx)
    assert second.valid
    assert "already turned this turn down once" in second.why


def test_the_second_request_stands_whatever_reason_it_carries():
    ctx = ctx_for("Best price kya de sakte ho?", session=PRICED)
    ctx.turn.handoff_rejected = True
    for reason in REASONS:
        assert arbiter.judge(reason, ctx).valid


# ── autonomy is respected, not bypassed ─────────────────────────────────────

def test_nothing_here_executes_anything():
    """
    §5, and the line between this layer and a rewrite. The arbiter returns a
    judgement. Every gate — permission, grant, argument validation, the
    property's own rules, `autonomy.decide` — is `registry.execute`'s, and
    sending the model back to a tool puts it in front of all five again.
    """
    import ast
    import inspect

    # The *code*, not the prose. The module explains at length what it delegates
    # to `registry.execute`, and a substring search over the file would fail on
    # its own documentation — which would be a test that punished the module for
    # being explained.
    tree = ast.parse(inspect.getsource(arbiter))

    calls = {
        ast.unparse(node.func) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("registry.execute", "get_db", "run_db",
                      "autonomy.decide", "permissions.check"):
        assert not any(forbidden in call for call in calls), (
            f"the arbiter calls {forbidden} — it decides, it does not act, and "
            f"every gate it might duplicate belongs to registry.execute"
        )

    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Await)], (
        "the arbiter awaited something. It runs inside a precheck in front of a "
        "waiting guest and must read only what the turn already holds"
    )
    assert not [n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)]


def test_a_low_autonomy_agent_is_still_sent_back_to_the_ladder():
    """
    Autonomy is deliberately *not* filtered on. At level 2 `negotiate_rate`
    comes back `awaiting_approval`, which is the correct business outcome, is
    honest to the guest, and then corroborates the handoff the model asks for
    next. Guessing what the ladder would say would be a second autonomy system
    disagreeing with the first.
    """
    judgement = arbiter.judge("guest_asked", ctx_for(
        "Best price kya de sakte ho?", session=PRICED, autonomy=1))
    assert judgement.rejected
    assert judgement.instead == "negotiate_rate"


def test_the_approval_that_follows_makes_the_next_handoff_valid():
    """The loop closing, which is the whole reason it is safe not to filter."""
    ctx = ctx_for("Best price kya de sakte ho?", session=PRICED, autonomy=2,
                  outcomes=[ran("negotiate_rate", "awaiting_approval")])
    judgement = arbiter.judge("autonomy_limit", ctx)
    assert judgement.valid
    assert judgement.category == arbiter.AUTONOMY_REQUIRES_APPROVAL
    assert "awaiting_approval" in judgement.why


def test_an_autonomy_claim_with_no_queued_action_behind_it_is_not_taken_on_trust():
    """
    The reason 3.x actually gave in the first forensics capture. "I am not
    allowed to" is checkable: either something was queued or refused on this
    turn, or nothing was.
    """
    judgement = arbiter.judge("autonomy_limit", ctx_for(
        "Best price kya de sakte ho?", session=PRICED))
    assert judgement.rejected


# ── the vocabulary ──────────────────────────────────────────────────────────

def test_every_reason_the_model_may_give_has_a_category():
    """
    Total over the model's own enum. An unmapped reason falls to
    UNSUPPORTED_REQUEST, which is *checkable* — so a reason that should have
    been a safety stop and was never mapped would be one this layer could turn
    down. That is the one direction this must not fail in.
    """
    for reason in REASONS:
        assert reason in arbiter._CATEGORY_OF, (
            f"{reason!r} is offered to the model and has no category, so it "
            f"would be treated as an unsupported request and could be refused"
        )
        assert arbiter._CATEGORY_OF[reason] in arbiter.CATEGORIES


def test_an_invented_reason_gets_a_handoff_rather_than_an_argument():
    assert arbiter.categorise("because I felt like it") == arbiter.DEFAULT_CATEGORY
    assert arbiter.categorise(None) == arbiter.DEFAULT_CATEGORY
    assert arbiter.judge("nonsense", ctx_for("Do you have parking?")).valid


def test_only_three_categories_are_ever_checked():
    """
    Stated as a set rather than left implicit, because widening it is how this
    layer would start overriding the stops that must never be overridden.
    """
    assert arbiter.CHECKABLE == {
        arbiter.GUEST_REQUESTED_HUMAN,
        arbiter.AUTONOMY_REQUIRES_APPROVAL,
        arbiter.UNSUPPORTED_REQUEST,
    }


def test_the_handoff_tool_can_never_nominate_itself():
    """
    A guest asking for a person creates an ESCALATION requirement whose tool
    *is* `request_human_handoff`. Without excluding it, the arbiter would turn
    a handoff down and instruct the model to hand off.
    """
    ctx = ctx_for("I want to talk to a human")
    assert "ESCALATION" in [r.intent for r in ctx.turn.requirements]
    judgement = arbiter.judge("guest_asked", ctx)
    assert judgement.valid
    assert judgement.instead is None
