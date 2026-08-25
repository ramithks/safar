"""
A percentage and a total in the same context is a multiplication waiting to
happen.

**Twice now, in live traffic, and the second time was the first fix's blind
spot.**

On 2026-08-24 a real group turn ended with the reply guardrail refusing
₹19,784 — thirty per cent of a ₹65,946 total, multiplied out by the model
because `get_group_rates` returned `depositPct: 30`, `get_quote` returned the
total, and nothing returned the product. `get_quote` was changed to compute it.

On 2026-08-25 `scripts/replay_e2e.py` caught the identical failure on the live
model, on a **two-room** quote against a group minimum of three:

    Pool Villa ke 2 rooms ka total price tax ke sath ₹84,016 hai. Isme se abhi
    aapko sirf 20% advance (₹16,803) dena hoga…

₹84,016 × 0.20 = ₹16,803.20. The first fix was guarded on `units >=
group.min_units`, and the comment justifying that guard said *"a single room
carries no deposit"* — which is false wherever the property has set a token
percentage. Casa Mandala's is 20.

**Nothing had to be computed to close it.** `pms/pricing.price_stay` has always
returned `token_paise` and `balance_paise`, derived from the property's own
`token_pct`; the tool payload simply never carried them. `policies.deposit_for`,
written for exactly this and handling all four payment rules, had no callers at
all.

The rule these tests hold: **wherever a guest can be told a percentage, the tool
must also hand over the rupee figure.** An instruction not to do arithmetic is
not a control — the model complied with the letter of it both times and quoted
a number it had worked out.
"""
import asyncio
from datetime import date, timedelta

import pytest

from app.services.ai import registry
from app.services.ai.context import AgentContext, AgentPrincipal
from tests.fake_supabase import FakeDb, install

registry.load_all()

PROPERTY = "eeeeeeee-1111-4111-8111-111111111111"
PARTNER = "ffffffff-1111-4111-8111-111111111111"
CONVERSATION = "e0e0e0e0-1111-4111-8111-111111111111"
ROOM_TYPE = "22222222-1111-4111-8111-111111111111"
RATE_PLAN = "11111111-1111-4111-8111-111111111111"

CHECK_IN = date.today() + timedelta(days=40)
CHECK_OUT = CHECK_IN + timedelta(days=4)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "city": "Anjuna", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "tax_inclusive": False, "status": "active",
        # The property's own advance. This is the number the model multiplied.
        "settings": {"token_pct": 20},
        "check_in_time": "14:00", "check_out_time": "11:00",
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
        "guest_profile": {},
    })
    fake.seed("room_types", {
        "id": ROOM_TYPE, "property_id": PROPERTY, "partner_id": PARTNER,
        "name": "Pool Villa", "code": "PV", "total_units": 8,
        "max_occupancy": 3, "standard_occupancy": 2, "is_active": True,
    })
    fake.seed("rate_plans", {
        "id": RATE_PLAN, "property_id": PROPERTY, "partner_id": PARTNER,
        "room_type_id": ROOM_TYPE, "code": "PV-HB", "name": "Villa + Half Board",
        "meal_plan": "HB", "audience": "public", "refundable": True,
        "inclusions": None, "base_price_paise": 1050200, "min_stay": 1,
        "derived_from_plan_id": None, "derive_discount_pct": None,
        "valid_from": None, "valid_to": None, "days_of_week": None,
        "allocation_units": None, "is_active": True,
        "cancellation_cutoff_hours": 48, "cancellation_policy": None,
    })

    # The RPCs the availability and hold paths read. A missing stub here does
    # not read as a missing stub: `hold_inventory` returning None produces a
    # 409 whose message is "those dates are no longer available", which looks
    # exactly like a sold-out hotel.
    def _availability(params):
        start = date.fromisoformat(params["p_from"])
        end = date.fromisoformat(params["p_to"])
        out, day = [], start
        while day < end:
            out.append({"room_type_id": ROOM_TYPE, "date": day.isoformat(),
                        "units_left": 8, "stop_sell": False})
            day += timedelta(days=1)
        return out

    fake.rpcs["property_availability"] = _availability
    fake.rpcs["property_rates"] = lambda params: []
    fake.rpcs["hold_inventory"] = lambda params: "40000000-1111-4111-8111-111111111111"
    fake.rpcs["release_hold"] = lambda params: True
    fake.seed("group_policies", {
        "id": "gp-1", "property_id": PROPERTY, "partner_id": PARTNER,
        "min_units": 3, "deposit_pct": 30, "payment_due_days": 14,
        "meal_package": None, "blackout_dates": [], "cancellation": {}, "terms": None,
    })
    install(monkeypatch, fake)
    return fake


def ctx() -> AgentContext:
    principal = AgentPrincipal(
        agent_id="a", agent_type="guest", partner_id=PARTNER, property_id=PROPERTY,
        display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"rates", "messaging", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def quote(units: int):
    return run(registry.execute("get_quote", {
        "room_type_id": ROOM_TYPE, "rate_plan_id": RATE_PLAN,
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
        "units": units, "adults": 2, "children": 0,
    }, ctx()))


class TestTheFigureIsAlwaysThere:
    def test_a_two_room_quote_carries_its_deposit(self, db):
        """
        The exact shape of the live failure: below the group minimum, above
        zero rooms, and a property that takes an advance.
        """
        out = quote(2)
        assert out.ok, out.refusal_reason
        assert out.output["depositPaise"] > 0
        assert out.output["deposit"].startswith("₹")

    def test_a_single_room_carries_it_too(self, db):
        """
        The sentence the first fix rested on — "a single room carries no
        deposit" — was simply untrue for this property.
        """
        assert quote(1).output["depositPaise"] > 0

    @pytest.mark.parametrize("units", [1, 2, 3, 5])
    def test_the_deposit_and_balance_always_sum_to_the_total(self, db, units):
        """
        The invariant §21 asks for, and the one that makes the figures safe to
        quote side by side.
        """
        out = quote(units).output
        assert out["depositPaise"] + out["balancePaise"] == out["totalPaise"]

    @pytest.mark.parametrize("units", [1, 2, 3, 5])
    def test_nothing_the_model_is_shown_needs_working_out(self, db, units):
        """
        Every rupee figure in the payload is present as a display string too,
        so there is no arithmetic left for the model to do. This is the rule;
        the two production failures were both what happens without it.
        """
        out = quote(units).output
        for paise_field, display_field in (("totalPaise", "total"),
                                           ("depositPaise", "deposit"),
                                           ("balancePaise", "balance")):
            assert paise_field in out
            assert display_field in out, f"{paise_field} has no display form"


class TestTheGroupRuleStillWins:
    def test_at_the_group_minimum_the_group_percentage_takes_over(self, db):
        out = quote(3).output
        assert out["depositPct"] == 30
        assert out["depositPaise"] == round(out["totalPaise"] * 30 / 100)

    def test_below_it_the_property_token_applies_instead(self, db):
        """
        And the group percentage is *not* announced, because it does not apply
        — a `depositPct: 30` on a two-room quote is a number the model would
        quote at a guest who is not getting it.
        """
        out = quote(2).output
        assert "depositPct" not in out
        assert out["depositPaise"] == round(out["totalPaise"] * 20 / 100)


class TestAPropertyThatTakesNothingUpFront:
    def test_gets_no_deposit_field_rather_than_a_zero_one(self, db):
        """
        The half of the original reasoning that was right, and the reason these
        fields are conditional: a `deposit: ₹0` on every quote is a field the
        model would eventually mention to somebody.
        """
        db.rows("properties")[0]["settings"] = {"token_pct": 0}
        db.rows("group_policies").clear()
        out = quote(1).output
        assert "deposit" not in out and "depositPaise" not in out
        assert "balance" not in out


class TestTheGuardrailWouldStillHaveCaughtIt:
    def test_the_invented_figure_is_refused_when_no_tool_produced_it(self, db):
        """
        The backstop that caught both production failures, asserted here so the
        two layers are known to be independent. The fix above means the model
        never needs to invent it; this means it still cannot.
        """
        from app.services.ai import guardrails

        out = quote(2)
        verdict = guardrails.check_reply(
            "Total ₹84,016 hai, abhi 20% advance ₹16,803 dena hoga.",
            tool_outputs=[out],
        )
        assert not verdict.ok
        assert "16803" in verdict.reason or "16,803" in verdict.reason

    def test_and_the_real_figure_passes(self, db):
        from app.services.ai import guardrails

        out = quote(2)
        reply = (f"Total {out.output['total']} hai, abhi "
                 f"{out.output['deposit']} advance dena hoga.")
        assert guardrails.check_reply(reply, tool_outputs=[out]).ok
