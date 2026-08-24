"""
Who decided this, and under what authority.

**The question the trail could not answer.** `agent_actions` recorded every tool
call and had no column that separated three different things which produce
byte-identical rows:

    the model asked for it                    an unattended decision
    the application ran it because the        a deterministic policy
      model did not
    a manager approved it                     a person's decision

Same tool, same arguments, same output. The difference is who is accountable,
and after the fact nothing could tell them apart. That mattered the moment the
required-action layer shipped — "how often does the model call `negotiate_rate`
by itself" is the number this whole reliability pass is judged on, and it was
not answerable from the database.

§17 asks for four things on every action: the autonomy level, the decision
source, whether a human had to approve it, and what became of that approval.
The first two are new columns; the third is a boolean over a status that already
existed; the fourth *is* the status. This file asserts all four, and asserts the
one property that makes them worth having — that they disagree with each other
correctly.
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
        "city": "Anjuna", "state": "Goa", "country": "India",
        "property_type": "boutique", "check_in_time": "14:00",
        "check_out_time": "11:00", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "tax_inclusive": False,
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
    })
    fake.seed("group_rate_tiers", {
        "id": "tier-3-5", "property_id": PROPERTY, "partner_id": PARTNER,
        "min_units": 3, "max_units": 5, "discount_pct": 8,
        "fixed_price_paise": None, "negotiable": False, "is_active": True,
        "room_type_id": None,
    })
    fake.seed("group_policies", {
        "id": "gp-1", "property_id": PROPERTY, "partner_id": PARTNER,
        "min_units": 3, "deposit_pct": 30, "payment_due_days": 14,
        "meal_package": None, "blackout_dates": [], "cancellation": {}, "terms": None,
    })
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in ("app.services.ai.runtime", "app.services.ai.context",
                   "app.services.ai.tools.read", "app.services.ai.tools.hotel",
                   "app.services.ai.tools.booking", "app.services.pms.rates",
                   "app.services.messaging.service", "app.services.messaging.pipeline"):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)
    return fake


def make_ctx(autonomy_level: int = 3) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala",
        autonomy_level=autonomy_level,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="config-1", config_version=1, model="",
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def only_action(db) -> dict:
    rows = db.rows("agent_actions")
    assert len(rows) == 1, f"expected one action, got {[r['tool'] for r in rows]}"
    return rows[0]


# ── the three sources ───────────────────────────────────────────────────────

def test_a_tool_the_model_asked_for_is_filed_as_the_models_decision(db):
    provider = EchoProvider([
        [("get_group_rates", {"units": 4})],
        "Chaar kamron ke liye ye rate hai.",
    ])
    run(runtime.run_turn(make_ctx(), "4 rooms chahiye", provider=provider))

    row = only_action(db)
    assert row["decision_source"] == "model"
    assert row["autonomy_level"] == 3
    assert row["human_approval_required"] is False
    assert row["status"] == "executed"


def test_a_tool_the_application_ran_is_filed_as_the_applications_decision(db):
    provider = EchoProvider(["Main check karti hoon."])
    run(runtime.run_turn(make_ctx(), "4 rooms chahiye", provider=provider))

    row = only_action(db)
    assert row["decision_source"] == "application"
    assert row["intent"] == "GROUP_BOOKING"
    assert row["autonomy_level"] == 3


def test_the_two_are_distinguishable_after_the_fact(db):
    """
    The point of the column. Two turns, the same tool, the same arguments, the
    same output — and a query can still tell which one the model chose.
    """
    run(runtime.run_turn(make_ctx(), "4 rooms chahiye",
                         provider=EchoProvider([[("get_group_rates", {"units": 4})],
                                                "Yeh raha."])))
    run(runtime.run_turn(make_ctx(), "4 rooms chahiye",
                         provider=EchoProvider(["Main check karti hoon."])))

    rows = [r for r in db.rows("agent_actions") if r["tool"] == "get_group_rates"]
    assert len(rows) == 2
    assert {r["decision_source"] for r in rows} == {"model", "application"}
    assert rows[0]["input"] == rows[1]["input"], (
        "the rows must be otherwise identical, or this test proves nothing"
    )


def test_an_approved_action_is_filed_as_a_persons_decision():
    """
    §17's fourth field. The row is written as `model` — the model is what
    proposed it — and that stops being true the moment somebody approves it.

    An unattended discount and a discount a manager pressed a button for are the
    same tool with the same arguments, and only this column separates them when
    somebody asks who authorised it.
    """
    import pathlib

    from app.services.ai import approvals

    source = pathlib.Path(approvals.__file__).read_text()
    # The settle that *executed* it, not the one that refused it — `_settle` is
    # called on both paths and only the success path is a person's decision.
    executed = source[source.index('"status": "executed",'):]
    block = executed[:executed.index("})")]

    assert '"decision_source": "human"' in block, (
        "an approved action still files itself as the model's decision, so "
        "nothing separates an unattended discount from one a manager authorised"
    )
    assert '"autonomy_level": ctx.principal.autonomy_level' in block
    assert '"approved_by": member.profile_id' in block


# ── autonomy, recorded against the decision it permitted ────────────────────

def test_an_action_records_the_autonomy_that_permitted_it(db):
    """
    Against the configuration in force at the time, not against today's. A
    hotel that moves from level 2 to level 3 next week must not make last
    week's queued actions look like unattended ones.
    """
    provider = EchoProvider([
        [("get_group_rates", {"units": 4})],
        "Yeh raha.",
    ])
    run(runtime.run_turn(make_ctx(autonomy_level=2), "4 rooms chahiye",
                         provider=provider))

    assert only_action(db)["autonomy_level"] == 2


def test_a_queued_action_says_it_is_waiting_for_a_human(db):
    """
    `human_approval_required` answers the same question as
    `status = 'awaiting_approval'` without the reader having to know that
    'awaiting_approval' is the string that means it — which is the difference
    between a column an operations dashboard can group by and a string somebody
    has to look up.
    """
    provider = EchoProvider([
        [("get_quote", {"room_type_id": "22222222-2222-4222-8222-222222222222",
                        "rate_plan_id": "11111111-1111-4111-8111-111111111111",
                        "check_in": "2026-09-10", "check_out": "2026-09-12"})],
        "Ek minute.",
    ])
    run(runtime.run_turn(make_ctx(autonomy_level=2), "kitna hoga?", provider=provider))

    quotes = [r for r in db.rows("agent_actions") if r["tool"] == "get_quote"]
    assert quotes, "the attempt must be recorded even when it is refused"
    assert quotes[0]["status"] == "awaiting_approval"
    assert quotes[0]["human_approval_required"] is True
    assert quotes[0]["autonomy_level"] == 2


def test_a_refusal_is_not_an_approval_request(db):
    """
    A level-0 agent does not queue; it refuses. The two are different columns
    and different operational problems — one is a configuration somebody should
    widen, the other is one they should not.
    """
    provider = EchoProvider([
        [("get_quote", {"room_type_id": "22222222-2222-4222-8222-222222222222",
                        "rate_plan_id": "11111111-1111-4111-8111-111111111111",
                        "check_in": "2026-09-10", "check_out": "2026-09-12"})],
        "Ek minute.",
    ])
    run(runtime.run_turn(make_ctx(autonomy_level=0), "kitna hoga?", provider=provider))

    quotes = [r for r in db.rows("agent_actions") if r["tool"] == "get_quote"]
    assert quotes[0]["status"] == "refused"
    assert quotes[0]["human_approval_required"] is False


# ── the run row: §21's questionnaire ────────────────────────────────────────

def test_every_question_section_21_asks_is_answerable_from_the_two_tables(db):
    """
    §21, item by item, against one turn's rows. Not a style check — each of
    these was a question somebody actually had to answer during this audit by
    reading logs, and every one of them is now a column.
    """
    provider = EchoProvider(["Main check karti hoon.", "Paanch kamre — yeh rate."])
    result = run(runtime.run_turn(
        make_ctx(), "5 kamre chahiye, 10 log aa rahe hain", provider=provider,
    ))

    run_row = db.rows("agent_runs")[0]
    action = only_action(db)

    # What did the guest ask?
    assert run_row["summary"] is not None or run_row["trigger"]
    # What intent was detected?
    assert run_row["intent"] == "GROUP_BOOKING"
    # What action was required, and which tool was expected?
    assert run_row["required_actions"]["required"] == ["get_group_rates"]
    # Did the model call it? If not, did the application force it?
    assert run_row["required_actions"]["forced"] == ["get_group_rates"]
    assert action["decision_source"] == "application"
    # What did the tool return?
    assert action["output"]["rooms"] == 5
    # What did the model tell the guest?
    assert result.reply == "Paanch kamre — yeh rate."
    # Did the guardrail modify or refuse it?  (null error == it did not)
    assert run_row["error"] is None
    # Was a handoff created?
    assert result.handoff is False
    assert run_row["status"] == "succeeded"
    # How long did it take?
    assert run_row["latency_ms"] >= 0
    assert action["latency_ms"] >= 0


def test_a_turn_that_required_nothing_still_says_so(db):
    """
    'none' rather than null. A null would be indistinguishable from a run
    written before this column existed, and the first thing anybody does with
    this data is count the turns that had a requirement.
    """
    provider = EchoProvider(["Namaste!"])
    run(runtime.run_turn(make_ctx(), "Hello", provider=provider))

    row = db.rows("agent_runs")[0]
    assert row["required_action_status"] == "none"
    assert row["required_actions"]["intents"] == []
    assert row["intent"] is None
