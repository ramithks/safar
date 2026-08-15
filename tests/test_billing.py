"""
Credits, and the two things a billing bug actually costs.

The arithmetic lives in Postgres and is tested there — `supabase/tests/
billing.sql`, 88 assertions against a real database, because a bucket walk and
a row lock cannot be meaningfully faked. What is worth testing here is the
Python either side of it, where three decisions live that SQL cannot express:

    · which usage type a piece of work is charged under
    · what happens when the charge itself fails
    · what happens when the wallet cannot be read

The last two are opposite on purpose, and getting either backwards is invisible
until it is a hotel with no AI or a month with no bill.

No database. Every call to `charge_usage` is intercepted.
"""
import asyncio

import pytest

from app.services import billing
from app.services.ai import usage as usage_mod


class _Recorder:
    """Stands in for the RPC. Records the arguments, returns a plausible reply."""

    def __init__(self, reply=None, raises=None):
        self.calls: list[dict] = []
        self.reply = reply
        self.raises = raises

    async def __call__(self, fn, *args):
        # run_db(fn, args) — the billing module always passes exactly one dict.
        payload = args[0] if args else {}
        self.calls.append(payload)
        if self.raises:
            raise self.raises
        if callable(self.reply):
            return self.reply(payload)
        return self.reply if self.reply is not None else {
            "usage_event_id": "11111111-1111-4111-8111-111111111111",
            "credits_charged": 8,
            "balance_after": 992,
            "cost_inr_micros": 89308,
            "status": "charged",
            "duplicate": False,
        }

    @property
    def last(self) -> dict:
        return self.calls[-1]


@pytest.fixture
def rpc(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("app.services.billing.service.run_db", rec)
    # The property → account lookup is cached; keep tests from leaking into
    # each other through it.
    billing.service._account_cache.clear()
    billing.service._account_cache["prop-1"] = "acct-1"
    return rec


def run(coro):
    return asyncio.run(coro)


# ── what a piece of work is charged as ──────────────────────────────────────
#
# An agent is charged for what it IS, not for what it cost. Which model
# answered is a routing decision the router may change tomorrow; "a guest
# conversation" is what the hotel bought, and it is the row on the rate card.

@pytest.mark.parametrize("agent_type,expected", [
    ("guest",     "ai.guest_conversation"),
    ("frontdesk", "ai.frontdesk_assist"),
    ("owner",     "ai.owner_copilot"),
    ("sales",     "ai.sales_assist"),
    ("marketing", "ai.content_generation"),
])
def test_each_agent_is_charged_as_what_it_is(agent_type, expected):
    assert billing.usage_type_for_agent(agent_type) == expected


# ── the one agent whose price depends on how hard the turn was ──────────────
#
# `ai.negotiation` is 20 credits and the rate card describes it as a reasoning-
# tier turn where a price is being argued. Charging every sales turn at it would
# bill 20 credits for "do you have a room on Friday" — two and a half times the
# guest agent's price for the same sentence.

def test_a_sales_turn_on_the_reasoning_tier_is_a_negotiation():
    assert billing.usage_type_for_agent("sales", tier="reasoning") == "ai.negotiation"
    assert billing.usage_type_for_agent("sales", tier="conversation") == "ai.sales_assist"


def test_no_other_agent_is_repriced_by_its_tier():
    # A front desk lookup and a front desk analysis are the same product to the
    # hotel. Only sales has a separately priced hard case already on the card.
    for agent_type in ("guest", "frontdesk", "owner", "marketing"):
        base = billing.usage_type_for_agent(agent_type)
        assert billing.usage_type_for_agent(agent_type, tier="reasoning") == base


def test_the_credit_gate_prices_the_ordinary_case():
    # Consulted before anything is routed. A gate that assumed the expensive
    # type would pause an agent that had the credits for the turn it was about
    # to take.
    assert billing.usage_type_for_agent("sales") == "ai.sales_assist"


def test_an_unknown_agent_falls_back_to_the_cheapest_conversational_type():
    # Not to the most expensive one. A new agent nobody added to the map should
    # under-bill rather than silently charge every hotel negotiation rates.
    assert billing.usage_type_for_agent("something-new") == "ai.guest_conversation"
    assert billing.usage_type_for_agent(None) == "ai.guest_conversation"


def test_swapping_the_model_does_not_change_what_the_hotel_is_charged(rpc):
    for model in ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-pro"):
        run(billing.charge(
            usage_type="ai.guest_conversation", units=1,
            idempotency_key=f"k:{model}", property_id="prop-1",
            provider="google", model=model,
        ))
    types = {c["p_usage_type"] for c in rpc.calls}
    assert types == {"ai.guest_conversation"}
    # The model still reaches the database — it is what the *cost* card
    # resolves on. It just is not what the price resolves on.
    assert {c["p_context"]["model"] for c in rpc.calls} == {
        "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-pro"}


# ── transactional vs proactive ──────────────────────────────────────────────

@pytest.mark.parametrize("purpose", [
    "booking_confirmation", "payment_confirmation", "cancellation",
    "checkin_link", "human_handoff",
])
def test_a_message_the_guest_is_owed_is_critical(purpose):
    assert billing.is_critical(purpose)


@pytest.mark.parametrize("purpose", ["pre_arrival", "follow_up", "reengagement", "other", None])
def test_a_nudge_is_not(purpose):
    assert not billing.is_critical(purpose)


# ── the idempotency key ─────────────────────────────────────────────────────
#
# The one thing that could not have been retro-fitted. Everything else here is
# a rate row; this is the difference between one invoice line and two.

def test_the_turn_key_is_the_run_never_the_clock():
    a = usage_mod.idempotency_key("run-1", "ai.guest_conversation")
    b = usage_mod.idempotency_key("run-1", "ai.guest_conversation")
    assert a == b == "ai.guest_conversation:run-1"


def test_two_usage_types_on_one_run_are_two_keys():
    assert (usage_mod.idempotency_key("run-1", "ai.tool_read")
            != usage_mod.idempotency_key("run-1", "ai.tool_write"))


def test_a_replayed_run_charges_under_the_same_key(rpc):
    for _ in range(3):
        run(usage_mod.record_run(
            run_id="run-9", property_id="prop-1", partner_id="p",
            agent_id="agent-1", agent_type="guest", conversation_id="conv-1",
            usage=usage_mod.TurnUsage(provider="google", model="gemini-2.5-flash"),
        ))
    keys = [c["p_idempotency_key"] for c in rpc.calls]
    assert keys == ["ai.guest_conversation:run-9"] * 3


def test_a_whatsapp_conversation_is_keyed_on_metas_conversation_id(rpc):
    # Meta repeats the conversation block on several status callbacks for one
    # window. Keying on the message id instead would charge per callback.
    for message in ("msg-1", "msg-2", "msg-3"):
        run(usage_mod.record_conversation(
            property_id="prop-1", channel="whatsapp", category="marketing",
            external_conversation_id="wa-conv-77", message_id=message,
            country="IN",
        ))
    assert {c["p_idempotency_key"] for c in rpc.calls} == {
        "whatsapp.marketing:conv:wa-conv-77"}


@pytest.mark.parametrize("category,expected", [
    ("service", "whatsapp.service"),
    ("utility", "whatsapp.utility"),
    ("marketing", "whatsapp.marketing"),
    ("authentication", "whatsapp.authentication"),
])
def test_metas_categories_map_to_their_own_usage_types(rpc, category, expected):
    run(usage_mod.record_conversation(
        property_id="prop-1", channel="whatsapp", category=category,
        external_conversation_id=f"wa-{category}", country="IN",
    ))
    assert rpc.last["p_usage_type"] == expected


def test_a_category_meta_invents_later_is_recorded_not_dropped(rpc):
    # An unmetered conversation is a hole in the margin report. A
    # wrong-but-visible one gets noticed.
    run(usage_mod.record_conversation(
        property_id="prop-1", channel="whatsapp", category="some_new_thing",
        external_conversation_id="wa-new",
    ))
    assert rpc.last["p_usage_type"] == "whatsapp.service"


def test_the_country_becomes_the_rate_dimension(rpc):
    run(usage_mod.record_conversation(
        property_id="prop-1", channel="whatsapp", category="marketing",
        external_conversation_id="wa-1", country="IN",
    ))
    assert rpc.last["p_context"]["dimension"] == {"country": "IN"}


# ── tokens are cost, turns are price ────────────────────────────────────────

def test_tokens_ride_on_the_turn_as_cost_input_not_as_units(rpc):
    turn = usage_mod.TurnUsage(provider="google", model="gemini-2.5-flash")
    turn.add_tokens(1420, 260, 40)
    run(usage_mod.record_run(
        run_id="run-2", property_id="prop-1", partner_id="p",
        agent_id="a", agent_type="guest", conversation_id="c", usage=turn,
    ))
    call = rpc.calls[0]
    # One turn is one unit, whatever it cost us to produce. A hotel billed by
    # the token would pay more for a chatty guest than for a demanding one.
    assert call["p_units"] == 1
    assert call["p_context"]["unit_detail"] == {
        "tokens_in": 1420, "tokens_out": 260, "tokens_thought": 40}


def test_the_billable_turn_is_not_counted_twice_under_its_old_name(rpc):
    # The runtime still counts 'ai.conversation_turn' for its debug line. If
    # that reached the charger alongside the typed turn it would be a duplicate
    # charge wearing a different name.
    turn = usage_mod.TurnUsage(provider="google", model="gemini-2.5-flash")
    turn.count("ai.conversation_turn")
    turn.count("ai.tool_read", 3)
    run(usage_mod.record_run(
        run_id="run-3", property_id="prop-1", partner_id="p",
        agent_id="a", agent_type="guest", conversation_id="c", usage=turn,
    ))
    types = [c["p_usage_type"] for c in rpc.calls]
    assert types == ["ai.guest_conversation", "ai.tool_read"]
    assert "ai.conversation_turn" not in types


def test_every_model_a_run_touched_is_recorded(rpc):
    # A turn that escalated from the workhorse to the reasoning tier cost two
    # different prices, and one `model` field would hide the expensive half.
    turn = usage_mod.TurnUsage(provider="google", model="gemini-2.5-flash")
    turn.used_model("gemini-2.5-flash")
    turn.used_model("gemini-2.5-pro")
    run(usage_mod.record_run(
        run_id="run-4", property_id="prop-1", partner_id="p",
        agent_id="a", agent_type="owner", conversation_id=None, usage=turn,
    ))
    assert rpc.calls[0]["p_context"]["metadata"]["models"] == [
        "gemini-2.5-flash", "gemini-2.5-pro"]


def test_an_outbound_message_is_metered_per_message_and_the_window_per_window(rpc):
    # Meta bills per conversation. The per-message row exists for the count,
    # priced at zero, so the same window is not billed twice.
    run(usage_mod.record_message(message={
        "id": "m1", "channel": "whatsapp", "property_id": "prop-1",
        "conversation_id": "c1", "external_id": "wamid.X",
    }))
    assert rpc.last["p_usage_type"] == "whatsapp.message_sent"

    run(usage_mod.record_message(
        message={"id": "m2", "channel": "whatsapp", "property_id": "prop-1",
                 "conversation_id": "c1", "external_id": "wamid.Y"},
        template_name="booking_confirmation_v1",
    ))
    assert rpc.last["p_usage_type"] == "whatsapp.template_sent"


# ── the two failure policies ────────────────────────────────────────────────

def test_a_metering_failure_never_reaches_the_caller(monkeypatch):
    # A meter that can take down a guest conversation is a worse bug than a
    # meter with a gap in it. The gap is recoverable — that is what
    # reconcile_provider_invoice exists for.
    async def boom(fn, *args):
        raise RuntimeError("supabase said no")

    monkeypatch.setattr("app.services.billing.service.run_db", boom)
    billing.service._account_cache["prop-1"] = "acct-1"

    result = run(billing.charge(
        usage_type="ai.guest_conversation", units=1,
        idempotency_key="k", property_id="prop-1",
    ))
    assert result.status == "error"
    assert result.recorded is False
    assert result.credits_charged == 0


def test_an_unreadable_wallet_lets_the_turn_through(monkeypatch):
    # Fails OPEN, deliberately and in the opposite direction to charge(). A
    # hotelier cannot tell "Supabase is having a minute" from "you have been
    # cut off for non-payment", and only one of those is true.
    async def boom(fn, *args):
        raise RuntimeError("supabase said no")

    monkeypatch.setattr("app.services.billing.service.run_db", boom)
    billing.service._account_cache["prop-1"] = "acct-1"

    decision = run(billing.allowed(usage_type="ai.guest_conversation", property_id="prop-1"))
    assert decision.allowed is True
    assert decision.reason == "check_failed"


def test_a_property_with_no_billing_account_is_not_refused(monkeypatch):
    # Missing provisioning is our bug, not the hotel's balance. Refusing here
    # would take a hotel's AI offline over our own bookkeeping.
    async def none(fn, *args):
        return None

    monkeypatch.setattr("app.services.billing.service.run_db", none)
    billing.service._account_cache.clear()

    decision = run(billing.allowed(usage_type="ai.guest_conversation", property_id="prop-x"))
    assert decision.allowed is True
    assert decision.reason == "no_account"


def test_an_empty_wallet_refuses_the_agent(rpc, monkeypatch):
    monkeypatch.setattr("app.services.billing.service.run_db", _Recorder(reply={
        "allowed": False, "tier": "empty", "reason": "insufficient_credits",
        "balance": 0, "available": 0,
    }))
    billing.service._account_cache["prop-1"] = "acct-1"
    decision = run(billing.allowed(usage_type="ai.guest_conversation", property_id="prop-1"))
    assert decision.allowed is False
    assert decision.tier == "empty"


def test_a_reservation_that_cannot_be_taken_does_not_swallow(monkeypatch):
    # Unlike charge(), reserve() must not return a cheerful default: that is how
    # four thousand campaign messages go out against a wallet that cannot pay.
    async def boom(fn, *args):
        raise RuntimeError("supabase said no")

    monkeypatch.setattr("app.services.billing.service.run_db", boom)
    with pytest.raises(RuntimeError):
        run(billing.reserve(
            billing_account_id="acct-1", usage_type="marketing.campaign_send",
            credits=4000, idempotency_key="campaign-7",
        ))


# ── the account lookup ──────────────────────────────────────────────────────

def test_the_account_is_resolved_once_and_remembered(monkeypatch):
    calls = []

    async def lookup(fn, *args):
        calls.append(args)
        return "acct-7"

    monkeypatch.setattr("app.services.billing.service.run_db", lookup)
    billing.service._account_cache.clear()

    assert run(billing.account_for_property("prop-9")) == "acct-7"
    assert run(billing.account_for_property("prop-9")) == "acct-7"
    # A property's billing account is set by a trigger when the property is
    # created and changes approximately never; a round trip per charge to
    # resolve a constant is a round trip on a guest's critical path.
    assert len(calls) == 1

    billing.forget_account("prop-9")
    run(billing.account_for_property("prop-9"))
    assert len(calls) == 2


def test_an_explicit_account_beats_the_property(rpc):
    run(billing.charge(
        usage_type="ai.report", units=1, idempotency_key="k",
        billing_account_id="acct-explicit", property_id="prop-1",
    ))
    assert rpc.last["p_billing_account"] == "acct-explicit"


def test_empty_context_keys_are_dropped_rather_than_sent_as_nulls(rpc):
    run(billing.charge(
        usage_type="ai.report", units=1, idempotency_key="k", property_id="prop-1",
    ))
    context = rpc.last["p_context"]
    assert "run_id" not in context
    assert "agent_id" not in context
    assert context["property_id"] == "prop-1"


# ── the promise ─────────────────────────────────────────────────────────────

def test_the_pms_never_imports_billing():
    """
    > Running out of credits must never stop a hotel from running the hotel.

    Front desk, reservations, inventory, rates, folios, invoicing, check-in/out,
    housekeeping and tasks are the subscription. The database says so in
    `billing.sql` (19.78, no PMS RPC reads a balance); this says so one layer
    up. A hotel whose PMS stops mid-check-in because a credit balance hit zero
    is a hotel that churns and tells other hoteliers.
    """
    import pathlib

    pms = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "pms"
    offenders = [
        path.name for path in pms.glob("*.py")
        if "services import billing" in path.read_text()
        or "services.billing" in path.read_text()
    ]
    assert offenders == [], f"the PMS reached for the wallet in {offenders}"
