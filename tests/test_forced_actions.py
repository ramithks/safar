"""
What happens when the model does not call the tool the message required.

`test_required_actions.py` asserts what a message *requires*. This asserts what
the runtime does about it, and the way it does that is the point: **the scripted
model here never calls anything.** Every test below hands the provider a script
that goes straight to prose — "ji bilkul, main check karti hoon" — which is
precisely what the live model did on the runs where the replay scored 28 instead
of 44. The assertions are then on the database: a `get_group_rates` row that
exists anyway, with `decision_source='application'`.

Four properties are worth stating, because they are the difference between a
reliability fix and a safety hole:

  * **Forcing goes through the same door.** `registry.execute` — permission,
    autonomy, argument validation, the property's own rules. The autonomy test
    below is the proof: a level-2 agent's forced negotiation is *queued*, not
    executed, exactly as it would have been had the model asked.

  * **It never runs a tool the agent was not granted.** Nothing here can widen
    a permission; the worst it can do is decline to act.

  * **It does not talk over a model that was right.** A turn where the model
    called the tool itself forces nothing, and the action row says `model`.

  * **The guest is told by the model, not by a template.** The forced result
    goes back for the model to phrase, which is what keeps the reply in the
    guest's own language.
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
RATE_PLAN = "11111111-1111-4111-8111-111111111111"
ROOM_TYPE = "22222222-2222-4222-8222-222222222222"

# The session as `messaging/session.py` would have left it after a quote: the
# guest has dates, a room and a rate plan. This is what makes a negotiation
# forceable — without it the layer deliberately declines to act.
QUOTED_SESSION = {
    "check_in": "2026-09-10", "check_out": "2026-09-12",
    "rate_plan_id": RATE_PLAN, "room_type_id": ROOM_TYPE,
    "room_name": "Deluxe Balcony", "units": 1, "adults": 2, "children": 0,
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed(
        "properties",
        {"id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala", "city": "Anjuna",
         "state": "Goa", "country": "India", "property_type": "boutique",
         "check_in_time": "14:00", "check_out_time": "11:00", "currency": "INR",
         "timezone": "Asia/Kolkata", "policies": {}, "tax_inclusive": False},
    )
    fake.seed(
        "conversations",
        {"id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
         "channel": "whatsapp", "handler": "ai", "status": "open"},
    )
    # The hotel's own volume pricing. Three to five rooms at eight per cent is
    # the band the replay seeds, so the numbers here are the numbers a
    # production run would produce.
    fake.seed(
        "group_rate_tiers",
        {"id": "tier-3-5", "property_id": PROPERTY, "partner_id": PARTNER,
         "min_units": 3, "max_units": 5, "discount_pct": 8, "fixed_price_paise": None,
         "negotiable": False, "is_active": True, "room_type_id": None},
        {"id": "tier-6", "property_id": PROPERTY, "partner_id": PARTNER,
         "min_units": 6, "max_units": None, "discount_pct": 12, "fixed_price_paise": None,
         "negotiable": True, "is_active": True, "room_type_id": None},
    )
    fake.seed(
        "group_policies",
        {"id": "gp-1", "property_id": PROPERTY, "partner_id": PARTNER,
         "min_units": 3, "deposit_pct": 30, "payment_due_days": 14,
         "meal_package": None, "blackout_dates": [], "cancellation": {}, "terms": None},
    )

    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in (
        "app.services.ai.runtime",
        "app.services.ai.context",
        "app.services.ai.tools.read",
        "app.services.ai.tools.hotel",
        "app.services.ai.tools.booking",
        "app.services.ai.tools.pricing",
        "app.services.pms.rates",
        "app.services.messaging.service",
        "app.services.messaging.pipeline",
    ):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)
    return fake


def make_ctx(*, autonomy_level: int = 3, session: dict | None = None,
             tools: tuple[str, ...] | None = None,
             scopes: tuple[str, ...] = ("tasks", "messaging", "rates", "bookings"),
             actor: str = "guest") -> AgentContext:
    names = tools if tools is not None else tuple(registry.REGISTRY)
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala",
        autonomy_level=autonomy_level, scopes=frozenset(scopes),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="config-1", config_version=1, model="",
        conversation_id=CONVERSATION, guest_name="Nisha Kapoor",
        channel="whatsapp", actor=actor, session=dict(session or {}),
        tool_grants={t: {"tool_name": t, "enabled": True} for t in names},
    )


def actions(db) -> list[dict]:
    return sorted(db.rows("agent_actions"), key=lambda a: a["seq"])


def tool_rows(db, name: str) -> list[dict]:
    return [a for a in actions(db) if a["tool"] == name]


# ── the group case ──────────────────────────────────────────────────────────

def test_a_group_enquiry_is_priced_even_when_the_model_only_says_it_will_check(db):
    """
    The replay's failing turn, with the model's failing behaviour scripted.

    "Ji bilkul, main check karti hoon" is a sentence, not a price. Before this,
    the turn ended there and the guest — ten people, five rooms, the largest
    enquiry that hotel would get that week — was told somebody would get back to
    them.
    """
    provider = EchoProvider(["Ji bilkul, main abhi check karti hoon."])
    result = run(runtime.run_turn(
        make_ctx(), "Actually 5 kamre chahiye, 10 log aa rahe hain", provider=provider,
    ))

    rows = tool_rows(db, "get_group_rates")
    assert rows, "the group tool did not run at all"
    assert rows[0]["decision_source"] == "application"
    assert rows[0]["intent"] == "GROUP_BOOKING"
    assert rows[0]["status"] == "executed"
    assert rows[0]["input"]["units"] == 5

    # And the hotel's own band, not one the model chose.
    assert rows[0]["output"]["appliedBand"]["fromRooms"] == 3
    assert rows[0]["output"]["depositPct"] == 30

    assert result.required_action_status == "forced"
    assert "get_group_rates" in result.requirements["forced"]


def test_the_run_row_records_what_was_required_and_what_became_of_it(db):
    """§21: every question about a turn answerable from the two tables."""
    provider = EchoProvider(["Main check karti hoon."])
    run(runtime.run_turn(make_ctx(), "5 kamre chahiye", provider=provider))

    row = db.rows("agent_runs")[0]
    assert row["intent"] == "GROUP_BOOKING"
    assert row["required_action_status"] == "forced"
    assert row["required_actions"]["required"] == ["get_group_rates"]
    assert row["required_actions"]["forced"] == ["get_group_rates"]
    assert row["required_actions"]["unmet"] == []


def test_a_model_that_called_the_tool_itself_is_not_overruled(db):
    """
    The good run. Nothing is forced, and the row says the model decided — which
    is the number the evaluation report is actually tracking.
    """
    provider = EchoProvider([
        [("get_group_rates", {"units": 5})],
        "Paanch kamron ke liye humara group rate hai.",
    ])
    result = run(runtime.run_turn(make_ctx(), "5 kamre chahiye", provider=provider))

    rows = tool_rows(db, "get_group_rates")
    assert len(rows) == 1, "the tool ran twice — the application talked over the model"
    assert rows[0]["decision_source"] == "model"
    assert rows[0]["intent"] == "GROUP_BOOKING"
    assert result.required_action_status == "model"
    assert result.requirements["forced"] == []


def test_a_quote_for_the_same_rooms_answers_the_group_question(db):
    """
    A model that went straight to a quote applied the band already. Forcing the
    bands after it would be a second tool call to tell the guest something they
    had just been told better.
    """
    provider = EchoProvider([
        [("get_quote", {"room_type_id": ROOM_TYPE, "rate_plan_id": RATE_PLAN,
                        "check_in": "2026-09-10", "check_out": "2026-09-12", "units": 5})],
        "Paanch kamron ka total ye rahega.",
    ])
    run(runtime.run_turn(make_ctx(), "5 kamre chahiye", provider=provider))

    # The quote itself may fail in this fixture — there are no room types
    # seeded — and that is fine: what is asserted is that a *successful* quote
    # would have discharged it, which is the `satisfied_by` contract.
    from app.services.ai import required_actions as ra
    found = [r for r in ra.detect("5 kamre chahiye", {}) if r.intent == ra.GROUP_BOOKING][0]
    assert "get_quote" in found.satisfied_by


# ── the negotiation case ────────────────────────────────────────────────────

def test_a_haggle_reaches_the_engine_even_when_the_model_answers_in_prose(db):
    """
    The other failing turn, and the more dangerous one. Before this, the model
    answering "4000 mein ho jayega?" in prose either repeated the guest's ₹4,000
    — which the reply guardrail refused, turning a ready buyer into a handoff —
    or invented a discount nobody approved.
    """
    provider = EchoProvider(["Main dekhti hoon kya kar sakte hain."])
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION),
        "Yaar thoda kam karo na, 4000 per night mein ho jayega?",
        provider=provider,
    ))

    rows = tool_rows(db, "negotiate_rate")
    assert rows, "the negotiation engine was never asked"
    assert rows[0]["decision_source"] == "application"
    assert rows[0]["intent"] == "NEGOTIATION"
    # The guest's own figure, passed as a question and not as an answer. §20.
    assert rows[0]["input"]["requested_nightly_rupees"] == 4000
    assert rows[0]["input"]["rate_plan_id"] == RATE_PLAN
    assert rows[0]["input"]["check_in"] == "2026-09-10"
    assert result.requirements["forced"] == ["negotiate_rate"]


def test_a_haggle_with_nothing_to_haggle_over_forces_nothing(db):
    """
    No rate plan on the session. The right answer is the model's — find out
    which room and which nights — and the layer stays out of it rather than
    opening a negotiation against a stay nobody has described.
    """
    provider = EchoProvider(["Kaunse dates ke liye dekh rahe hain aap?"])
    result = run(runtime.run_turn(make_ctx(), "thoda kam karo na", provider=provider))

    assert tool_rows(db, "negotiate_rate") == []
    assert result.requirements["forced"] == []
    # Recorded rather than hidden: the report counts these.
    assert "NEGOTIATION" in result.requirements["observed_unmet"]


def test_autonomy_still_decides_whether_a_forced_negotiation_executes(db):
    """
    **The safety property, stated as a test.** A level-2 agent may not
    transact. The application forcing the call does not change that: the same
    autonomy gate runs, the action is queued for a human, and nothing is
    negotiated. §17 and §20 — the deterministic layer decides *that* a business
    tool is asked, never what it is allowed to answer.
    """
    provider = EchoProvider(["Main dekhti hoon."])
    run(runtime.run_turn(
        make_ctx(autonomy_level=2, session=QUOTED_SESSION),
        "4000 mein kar do", provider=provider,
    ))

    rows = tool_rows(db, "negotiate_rate")
    assert rows, "the attempt must still be recorded"
    assert rows[0]["status"] == "awaiting_approval"
    assert rows[0]["human_approval_required"] is True
    assert rows[0]["autonomy_level"] == 2
    assert rows[0]["decision_source"] == "application"


def test_an_agent_without_the_grant_is_not_forced_into_anything(db):
    """
    Nothing here can widen a permission. An agent that was never given
    `negotiate_rate` does not acquire it because a guest said "discount".
    """
    ctx = make_ctx(session=QUOTED_SESSION,
                   tools=("get_property", "request_human_handoff", "send_message"))
    provider = EchoProvider(["Let me check with the team."])
    run(runtime.run_turn(ctx, "discount milega?", provider=provider))

    assert tool_rows(db, "negotiate_rate") == []


# ── the boundaries ──────────────────────────────────────────────────────────

def test_a_handoff_ends_the_turn_rather_than_being_talked_over(db):
    """
    The conversation has gone to a person. An extra tool call after that is the
    AI carrying on past its own exit — and, on a group enquiry, taking a hold
    the person about to pick this up knows nothing about.
    """
    provider = EchoProvider([
        [("request_human_handoff", {"reason": "guest_asked"})],
        "Someone will be with you.",
    ])
    run(runtime.run_turn(make_ctx(), "5 kamre chahiye, manager se baat karwao",
                         provider=provider))

    assert tool_rows(db, "get_group_rates") == []


def test_a_tool_the_model_already_tried_is_not_run_again(db):
    """
    A `negotiate_rate` that failed is a system problem, and running it again
    inside the same turn takes a second hold on the same rooms. The guard is on
    *attempted*, not on succeeded.
    """
    provider = EchoProvider([
        # Deliberately invalid: check_out before check_in, which the tool
        # refuses. The requirement is therefore still unmet at the end.
        [("negotiate_rate", {"rate_plan_id": RATE_PLAN, "check_in": "2026-09-12",
                             "check_out": "2026-09-10", "units": 1})],
        "Ek minute.",
    ])
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "thoda kam karo", provider=provider,
    ))

    assert len(tool_rows(db, "negotiate_rate")) == 1
    assert result.requirements["forced"] == []
    assert "NEGOTIATION" in result.requirements["unmet"]
    assert result.required_action_status == "unmet"


def test_a_staff_turn_is_never_forced(db):
    """
    A receptionist typing "5 rooms for the Sharma party" into the console is not
    a guest asking for a price. Forcing a pricing tool onto a staff question
    would be the system second-guessing the person it works for.
    """
    provider = EchoProvider(["Noted."])
    ctx = make_ctx(actor="member")
    run(runtime.run_turn(ctx, "5 kamre chahiye", provider=provider, trigger="manual"))

    assert tool_rows(db, "get_group_rates") == []


def test_an_ordinary_turn_forces_nothing_and_costs_nothing(db):
    """The common case. Most of a hotel's inbox requires no business action."""
    provider = EchoProvider(["Namaste! Kaise madad kar sakti hoon?"])
    result = run(runtime.run_turn(make_ctx(), "Hello", provider=provider))

    assert actions(db) == []
    assert result.required_action_status == "none"
    assert db.rows("agent_runs")[0]["required_actions"]["intents"] == []


def test_the_model_gets_the_forced_result_to_say_in_its_own_words(db):
    """
    §2: the application decides the action, the model writes the sentence. The
    alternative — a canned reply — would be in the wrong language and the wrong
    voice, and a hotel's guests write in three.
    """
    provider = EchoProvider([
        "Main check karti hoon.",
        "Paanch kamron par humein 8% ka group rate mil jaata hai.",
    ])
    result = run(runtime.run_turn(make_ctx(), "5 kamre chahiye", provider=provider))

    # A second step happened, and it was given the tool's real output.
    assert len(provider.seen_messages) == 2
    handed_back = provider.seen_messages[-1][-1]
    assert handed_back["role"] == "user"
    assert "get_group_rates" in handed_back["content"]
    assert "appliedBand" in handed_back["content"]
    # …and it was asked for prose, not for more tools.
    assert provider.seen_tools[-1] == []
    assert result.reply == "Paanch kamron par humein 8% ka group rate mil jaata hai."


def test_the_forced_step_does_not_tell_the_guest_the_system_intervened(db):
    """
    The instruction the model is handed says so explicitly. A guest being told
    "the system called a tool for me" is the product describing its own
    plumbing, which is the one thing no hotel wants its AI doing.
    """
    provider = EchoProvider(["Main check karti hoon.", "Yeh raha aapka rate."])
    run(runtime.run_turn(make_ctx(), "5 kamre chahiye", provider=provider))

    note = provider.seen_messages[-1][-1]["content"]
    assert "do not mention that any of this happened" in note


def test_enforcement_can_be_switched_off_without_losing_the_measurement(db, monkeypatch):
    """
    The rollback, and the way the improvement is measured.

    With `AGENT_FORCE_REQUIRED_ACTIONS=false` nothing is forced — but detection
    still runs, so `agent_runs.required_actions` still records that a group
    enquiry went unanswered. That is the whole point: the off position has to
    produce the *before* number, not silence.
    """
    monkeypatch.setenv("AGENT_FORCE_REQUIRED_ACTIONS", "false")
    provider = EchoProvider(["Ji bilkul, main check karti hoon."])
    result = run(runtime.run_turn(make_ctx(), "5 kamre chahiye", provider=provider))

    assert tool_rows(db, "get_group_rates") == []
    assert result.requirements["forced"] == []
    assert result.requirements["intents"] == ["GROUP_BOOKING"]
    assert result.required_action_status == "unmet"
    assert db.rows("agent_runs")[0]["required_action_status"] == "unmet"


def test_enforcement_is_on_unless_somebody_turns_it_off(monkeypatch):
    from app.services.ai import required_actions as ra

    monkeypatch.delenv("AGENT_FORCE_REQUIRED_ACTIONS", raising=False)
    assert ra.enforcement_enabled() is True
    for off in ("false", "FALSE", "0", "no", "off"):
        monkeypatch.setenv("AGENT_FORCE_REQUIRED_ACTIONS", off)
        assert ra.enforcement_enabled() is False, off
    monkeypatch.setenv("AGENT_FORCE_REQUIRED_ACTIONS", "true")
    assert ra.enforcement_enabled() is True
