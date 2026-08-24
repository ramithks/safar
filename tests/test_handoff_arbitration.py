"""
The failure, end to end, with the model's failing behaviour scripted.

`test_arbiter.py` asserts what the arbiter *decides*. This asserts what the
runtime does about it, and the way it does that is the point: **the scripted
model here calls `request_human_handoff("guest_asked")` on a turn where the
guest asked for a discount.** That is the exact call that lost sales, reproduced
without a model and therefore without a rate limit, a flake or a bill.

What has to be true afterwards, in order of how much it matters:

  * the conversation was **not** handed to a person,
  * the negotiation engine ran anyway,
  * the guest got an answer,
  * and the autonomy ladder still decided whether the negotiation executed.

The last one is the one that would make this a safety hole rather than a
reliability fix, and it has its own test at the bottom.

There is also a whole class of turns that must be untouched, and they get equal
weight here: a guest who asks for a person, a guest who is upset, a guest whose
question nobody can answer. If any of those stops reaching a human, this layer
has made the product worse in a way that is much harder to see than the bug it
fixed.
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
        {"id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
         "city": "Anjuna", "state": "Goa", "country": "India",
         "property_type": "boutique", "check_in_time": "14:00",
         "check_out_time": "11:00", "currency": "INR",
         "timezone": "Asia/Kolkata", "policies": {}, "tax_inclusive": False},
    )
    fake.seed(
        "conversations",
        {"id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
         "channel": "whatsapp", "handler": "ai", "status": "open"},
    )
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in (
        "app.services.ai.runtime", "app.services.ai.context",
        "app.services.ai.tools.read", "app.services.ai.tools.hotel",
        "app.services.ai.tools.booking", "app.services.ai.tools.pricing",
        "app.services.ai.tools.communication",
        "app.services.pms.rates", "app.services.messaging.service",
        "app.services.messaging.pipeline",
    ):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)
    return fake


def make_ctx(*, autonomy_level: int = 3, session: dict | None = None,
             actor: str = "guest") -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala",
        autonomy_level=autonomy_level,
        scopes=frozenset({"tasks", "messaging", "rates", "bookings"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="config-1", config_version=1, model="",
        conversation_id=CONVERSATION, guest_name="Nisha Kapoor",
        channel="whatsapp", actor=actor, session=dict(session or {}),
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def actions(db) -> list[dict]:
    return sorted(db.rows("agent_actions"), key=lambda a: a["seq"])


def tool_rows(db, name: str) -> list[dict]:
    return [a for a in actions(db) if a["tool"] == name]


HANDOFF_ON_A_HAGGLE = [
    [("request_human_handoff", {"reason": "guest_asked",
                                "note": "Guest wants a discount."})],
    "Main dekhti hoon.",
]


# ── the failure, refused ────────────────────────────────────────────────────

def test_the_conversation_is_not_handed_over(db):
    """
    The guest asked for a better price. Nobody asked for a person.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    assert result.handoff is False
    conversation = db.rows("conversations")[0]
    assert conversation["handler"] == "ai", (
        "the thread was parked with a person on a turn the pricing engine "
        "could answer in four seconds"
    )


def test_the_handoff_is_recorded_as_refused_rather_than_hidden(db):
    """
    §21. A handoff the system turned down is exactly as much of an event as one
    that went through, and a record that kept only the successful ones would
    make this layer invisible in production.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    rows = tool_rows(db, "request_human_handoff")
    assert rows, "the attempt itself must be on the record"
    assert rows[0]["status"] == "refused"
    assert "HANDOFF_NOT_SUPPORTED" in (rows[0]["refusal_reason"] or "")

    judgements = result.requirements["handoff_judgements"]
    assert judgements[0]["accepted"] is False
    assert judgements[0]["category"] == "GUEST_REQUESTED_HUMAN"
    assert judgements[0]["instead"] == "negotiate_rate"


def test_the_negotiation_runs_instead(db):
    """
    The whole point. Refusing the handoff is only worth doing if the turn then
    does the thing the guest actually asked for — and here the *model* never
    asks for it, so this is the deterministic layer finishing the job.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    rows = tool_rows(db, "negotiate_rate")
    assert rows, "the guest asked for a price and no price was asked for"
    assert rows[0]["decision_source"] == "application"
    assert rows[0]["intent"] == "NEGOTIATION"
    assert result.requirements["forced"] == ["negotiate_rate"]


def test_the_guest_gets_an_answer(db):
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))
    assert result.reply
    assert "team" not in (result.reply or "").lower()


def test_the_model_is_told_what_to_do_rather_than_only_what_it_did_wrong(db):
    """
    A refusal the model cannot act on becomes an apology to the guest. What it
    receives names the tool.
    """
    provider = EchoProvider(list(HANDOFF_ON_A_HAGGLE))
    run(runtime.run_turn(make_ctx(session=QUOTED_SESSION),
                         "4000 mein kar do na", provider=provider))

    shown = [m for step in provider.seen_messages for m in step
             if m.get("role") == "tool" and m.get("name") == "request_human_handoff"]
    assert shown
    assert "negotiate_rate" in shown[-1]["content"]
    assert "not handed over" in shown[-1]["content"].lower()


# ── what must still reach a person ──────────────────────────────────────────

def test_a_guest_who_asks_for_a_person_still_gets_one(db):
    """
    The regression that would be far worse than the bug. Asserted on the same
    machinery, the same turn shape, the same tool call — only the guest's
    sentence differs.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "mujhe kisi se baat karni hai",
        provider=EchoProvider([
            [("request_human_handoff", {"reason": "guest_asked"})],
            "Ek member aapse baat karenge.",
        ]),
    ))

    assert result.handoff is True
    assert db.rows("conversations")[0]["handler"] == "human_requested"


def test_an_upset_guest_is_never_second_guessed(db):
    """
    §20. A complaint goes to a person on the model's word alone, even on a turn
    with an unmet negotiation behind it — which is exactly the shape this layer
    is otherwise built to turn down.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION),
        "This is unacceptable, aur best price kya hai",
        provider=EchoProvider([
            [("request_human_handoff", {"reason": "complaint"})],
            "Main abhi team ko bata rahi hoon.",
        ]),
    ))
    assert result.handoff is True


def test_a_question_nobody_can_answer_still_stops(db):
    """
    The conservative default. No business action outstanding means nothing
    better to offer, so "I do not know" stands however thin it is.
    """
    result = run(runtime.run_turn(
        make_ctx(), "Do you allow pet snakes in the garden rooms?",
        provider=EchoProvider([
            [("request_human_handoff", {"reason": "no_knowledge"})],
            "Let me get someone from the hotel.",
        ]),
    ))
    assert result.handoff is True


def test_a_second_request_in_the_same_turn_always_goes_through(db):
    """
    **The termination guarantee.** The arbiter may delay a stop by one step and
    may never prevent one. A model that insists gets its handoff, which bounds
    the worst case at one extra tool call rather than at a guest who never
    reaches anybody.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider([
            [("request_human_handoff", {"reason": "guest_asked"})],
            [("request_human_handoff", {"reason": "guest_asked"})],
            "Ek member aapse baat karenge.",
        ]),
    ))

    assert result.handoff is True
    rows = tool_rows(db, "request_human_handoff")
    assert [r["status"] for r in rows] == ["refused", "executed"]


# ── autonomy is not bypassed ────────────────────────────────────────────────

def test_a_level_two_agent_still_cannot_transact(db):
    """
    **The safety property, stated as a test.** Turning down a handoff sends the
    model back through `registry.execute`, which runs permission, grant,
    argument validation, the property's own rules and `autonomy.decide` exactly
    as it always did. A level-2 agent's negotiation is queued for a human, not
    executed — the arbiter decides *that* a business tool is asked, never what
    it is allowed to answer.
    """
    run(runtime.run_turn(
        make_ctx(autonomy_level=2, session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    rows = tool_rows(db, "negotiate_rate")
    assert rows, "the attempt must still be recorded"
    assert rows[0]["status"] == "awaiting_approval"


def test_and_the_handoff_that_follows_the_approval_is_valid(db):
    """
    The loop closing. At level 2 the negotiation queues, and *that* corroborates
    the autonomy claim — so the model's next handoff is supported by the
    conversation and goes through. The ladder answers; the arbiter does not
    guess what it would have said.
    """
    result = run(runtime.run_turn(
        make_ctx(autonomy_level=2, session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider([
            [("negotiate_rate", {"rate_plan_id": RATE_PLAN,
                                 "check_in": "2026-09-10", "check_out": "2026-09-12",
                                 "units": 1, "adults": 2, "children": 0,
                                 "requested_nightly_rupees": 4000})],
            [("request_human_handoff", {"reason": "autonomy_limit"})],
            "Team se confirm karke bataati hoon.",
        ]),
    ))

    assert result.handoff is True
    judgements = result.requirements["handoff_judgements"]
    assert judgements[-1]["accepted"] is True
    assert judgements[-1]["category"] == "AUTONOMY_REQUIRES_APPROVAL"


# ── staff turns are not guest turns ─────────────────────────────────────────

def test_a_member_typing_into_the_console_is_not_arbitrated(db):
    """
    Requirements are detected on guest turns only, so a staff-facing agent has
    nothing outstanding and its handoff stands. A receptionist asking a question
    is not a guest haggling, and second-guessing the person the product works
    for would be the system overruling its own operator.
    """
    result = run(runtime.run_turn(
        make_ctx(actor="member", session=QUOTED_SESSION), "best price kya hai",
        provider=EchoProvider([
            [("request_human_handoff", {"reason": "guest_asked"})],
            "Passing this on.",
        ]),
    ))
    assert result.handoff is True


# ── the switch ──────────────────────────────────────────────────────────────

def test_arbitration_can_be_turned_off_without_a_deploy(db, monkeypatch):
    """
    §12. A new layer that changes what a guest is told needs a way back that is
    an environment variable rather than a hotfix at eleven at night.
    """
    monkeypatch.setenv("AGENT_ARBITRATE_HANDOFFS", "false")

    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    assert result.handoff is True, "the switch did not switch anything off"
    assert db.rows("conversations")[0]["handler"] == "human_requested"


def test_the_judgement_is_recorded_even_when_it_is_not_acted_on(db, monkeypatch):
    """
    **What makes the off/on comparison possible at all.** With the layer off,
    the arbiter still says what it would have done and the run still records it
    — so the "before" number can be measured on the same day, against the same
    hotel and the same model, rather than quoted from an audit last week.

    It is also what makes a rollback measurable: a property running with this
    off can still be asked how often it *would* have fired.
    """
    monkeypatch.setenv("AGENT_ARBITRATE_HANDOFFS", "false")

    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    judgements = result.requirements["handoff_judgements"]
    assert judgements, "nothing was recorded, so the off case is unmeasurable"
    assert judgements[0]["enforced"] is False
    assert judgements[0]["instead"] == "negotiate_rate"
    assert judgements[0]["category"] == "GUEST_REQUESTED_HUMAN"


def test_with_the_switch_on_the_same_turn_is_answered(db, monkeypatch):
    """The other half of the comparison, so the pair reads as one statement."""
    monkeypatch.setenv("AGENT_ARBITRATE_HANDOFFS", "true")

    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "4000 mein kar do na",
        provider=EchoProvider(list(HANDOFF_ON_A_HAGGLE)),
    ))

    assert result.handoff is False
    assert result.requirements["forced"] == ["negotiate_rate"]


# ── the counterweight ───────────────────────────────────────────────────────
#
# The arbiter can say "not yet". A system that can say that needs something
# that can say "now", or the only way it can be wrong is the way that matters
# most. Both read `required_actions._ESCALATION_ASK`, so the two agree by
# construction: a sentence that forces a handoff can never be one the arbiter
# turns down.

def test_a_guest_who_asks_for_a_person_gets_one_even_if_the_model_does_nothing(db):
    """
    The model answers in prose and never reaches for the tool — which is the
    behaviour that made every other requirement in `required_actions` necessary,
    and is least defensible on this one. The others are about a hotel losing a
    sale; this is about a person asking for a person.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "mujhe kisi se baat karni hai",
        provider=EchoProvider(["Ji, main aapki kya madad kar sakti hoon?"]),
    ))

    assert result.handoff is True, "the guest asked for a human and kept talking to the AI"
    assert db.rows("conversations")[0]["handler"] == "human_requested"

    rows = tool_rows(db, "request_human_handoff")
    assert rows and rows[0]["decision_source"] == "application"
    assert rows[0]["intent"] == "ESCALATION"
    assert rows[0]["input"]["reason"] == "guest_asked"


def test_the_forced_handoff_is_not_turned_down_by_the_arbiter(db):
    """
    The two layers agreeing, asserted rather than assumed. The application
    forces `request_human_handoff("guest_asked")`, which then goes through the
    arbiter like any other call — and would be a comic failure if the same
    sentence that required it also failed to justify it.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "manager se baat karao",
        provider=EchoProvider(["Ji bilkul."]),
    ))

    assert result.handoff is True
    assert result.requirements["forced"] == ["request_human_handoff"]
    judgement = result.requirements["handoff_judgements"][0]
    assert judgement["accepted"] is True


def test_a_model_that_does_hand_off_is_not_made_to_do_it_twice(db):
    """
    The requirement is discharged by the model's own call, so nothing is forced
    and the thread is parked once.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "mujhe kisi se baat karni hai",
        provider=EchoProvider([
            [("request_human_handoff", {"reason": "guest_asked"})],
            "Ek member aapse baat karenge.",
        ]),
    ))

    assert result.handoff is True
    assert result.requirements["forced"] == []
    assert len(tool_rows(db, "request_human_handoff")) == 1


def test_a_haggle_is_still_not_a_request_for_a_person(db):
    """
    The other side of the same regex. Forcing a stop is safe precisely because
    the vocabulary that triggers it is narrow, and a build that widened it into
    ordinary sales conversation would park every negotiation with a human.
    """
    result = run(runtime.run_turn(
        make_ctx(session=QUOTED_SESSION), "thoda kam karo na",
        provider=EchoProvider(["Main dekhti hoon."]),
    ))
    assert result.handoff is False
    assert "request_human_handoff" not in result.requirements["forced"]
