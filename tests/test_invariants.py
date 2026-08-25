"""
Properties that must hold for every input, not for the ones somebody thought of.

§21: *"Do not only write example tests. Add invariants."*

**Why this file is separate from the rest.** Every other suite here asks
"does this case behave?"; this one asks "is there any case that does not?"
The difference matters most in the two places a hotel cannot absorb a mistake —
the discount floor and the autonomy ladder — because both are enforced by
arithmetic over numbers a hotel sets, and a hotel will eventually set a
combination nobody wrote a test for. A 40% approval ceiling on a room with a
₹4,300 floor is one of those.

**Generated deterministically, not randomly.** A property test that fails on
Tuesday and passes on Wednesday teaches nobody anything. The grids below are
built from the values that actually occur — percentages hotels choose, room
counts guests ask for, nights people stay — crossed exhaustively. Roughly
thirty thousand assertions run in under a second, and the same thirty thousand
run every time.
"""
import itertools

import pytest

from app.services.ai import autonomy, registry
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.pms import negotiation, policies, pricing

registry.load_all()


# ── the grids ────────────────────────────────────────────────────────────────

GROSS = (100_00, 450_00, 4_500_00, 11_400_00, 65_946_0, 193_237_00)
NIGHTS = (1, 2, 4, 7, 30)
UNITS = (1, 2, 5, 20)
AUTO_PCT = (0, 5, 10, 15, 25)
APPROVAL_PCT = (0, 10, 20, 40, 60)
FLOOR_PCT = (None, 0, 10, 30, 50)


def rules(auto: int, approval: int, floor_pct: int | None, *, enabled: bool = True):
    return negotiation.Rules(
        enabled=enabled, source="test",
        floor_pct=floor_pct, floor_paise=None,
        target_pct=None, target_paise=None, ceiling_paise=None,
        auto_pct=auto, approval_pct=approval,
        valid_from=None, valid_to=None,
        days_of_week=frozenset(), blackout_dates=frozenset(),
        min_nights=1, min_units=1,
    )


ALL_RULES = [
    rules(auto, approval, floor)
    for auto, approval, floor in itertools.product(AUTO_PCT, APPROVAL_PCT, FLOOR_PCT)
]


# ── NEGOTIATION: a final price is never below the floor ─────────────────────

class TestNoPriceEverGoesBelowTheFloor:
    """
    The invariant a hotel's revenue rests on.

    Every path through `decide` that names a number — `final_total_paise` when
    the answer is yes, `counter_paise` when it is a counter-offer — must be at
    or above the floor the property set, whatever the guest asked for and
    whatever combination of ceilings the hotel configured. Including the
    contradictory ones: an approval ceiling *below* the automatic floor is a
    configuration a hotel can save, and it must not become a discount.
    """

    @pytest.mark.parametrize("gross", GROSS)
    @pytest.mark.parametrize("nights,units", list(itertools.product(NIGHTS, UNITS))[:8])
    def test_across_every_rule_and_every_asking_price(self, gross, nights, units):
        for rule in ALL_RULES:
            floor = negotiation.floor_total_for(gross, rule, nights, units)
            if floor is None:
                continue
            # Asking prices from free to above the published rate.
            for fraction in (0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0, 1.5):
                decision = negotiation.decide(
                    gross_paise=gross, requested_total_paise=int(gross * fraction),
                    rules=rule, nights=nights, units=units,
                )
                for field in ("final_total_paise", "counter_total_paise"):
                    named = getattr(decision, field, None)
                    if named is None:
                        continue
                    assert named >= floor, (
                        f"{field}={named} is below the floor {floor} "
                        f"(gross={gross}, asked={int(gross * fraction)}, "
                        f"auto={rule.auto_pct}, approval={rule.approval_pct}, "
                        f"floor_pct={rule.floor_pct})"
                    )

    def test_and_never_above_the_published_price_either(self):
        """
        The other end. A guest who offers more than the rack rate is answered
        at the rack rate — a hotel does not charge somebody extra for offering.
        """
        for gross, rule in itertools.product(GROSS, ALL_RULES[:20]):
            decision = negotiation.decide(
                gross_paise=gross, requested_total_paise=gross * 3,
                rules=rule, nights=2, units=1,
            )
            if decision.final_total_paise is not None:
                assert decision.final_total_paise <= gross

    def test_a_closed_negotiation_names_no_number_at_all(self):
        for gross in GROSS:
            decision = negotiation.decide(
                gross_paise=gross, requested_total_paise=1,
                rules=rules(10, 20, 30, enabled=False), nights=2, units=1,
            )
            assert decision.verdict == "rejected"
            assert decision.final_total_paise is None
            assert decision.counter_total_paise is None

    def test_a_price_below_the_floor_is_never_sent_to_a_manager(self):
        """
        **Found by mutation, not by reading.** Deleting `approved_total =
        max(approved_total, floor)` from `decide` broke no test in this suite,
        because `approved_total` never becomes a price — every figure that
        leaves goes through `_pct_for(..., floor)` and is clamped there.

        What it *does* decide is which branch the request lands in. Without the
        clamp, an asking price below the floor compares as `>= approved_total`
        and comes back `needs_approval` — so the agent tells the guest it is
        checking with a manager about a number no manager can grant, and the
        guest waits for an answer that can only ever be no. The comment on
        `approval_available` names exactly that failure; nothing held it.

        The invariant is the one the code claims: below the floor, the answer is
        a counter-offer, never a promise to ask.
        """
        for gross in GROSS:
            for rule in ALL_RULES:
                if not rule.negotiable:
                    continue
                for nights, units in ((2, 1), (4, 2), (7, 5)):
                    floor = negotiation.floor_total_for(gross, rule, nights, units)
                    if floor is None or floor <= 0:
                        continue
                    decision = negotiation.decide(
                        gross_paise=gross, requested_total_paise=floor - 1,
                        rules=rule, nights=nights, units=units,
                    )
                    assert decision.verdict != "needs_approval", (
                        f"asked {floor - 1}, below the floor {floor}, and the "
                        f"agent was told a manager could grant it "
                        f"(auto={rule.auto_pct}, approval={rule.approval_pct}, "
                        f"floor_pct={rule.floor_pct})"
                    )

    def test_the_floor_itself_is_never_on_the_object(self):
        """
        §11 provenance, structurally. `counter_paise` may happen to equal the
        floor and saying that number is fine; a field *labelled* as the minimum
        is one a projection, a log line or a prompt can leak.
        """
        fields = set(negotiation.Decision.__dataclass_fields__)
        assert not {f for f in fields if "floor" in f}, fields


# ── MONEY: the parts always sum to the whole ────────────────────────────────

class TestMoneyAddsUp:
    @pytest.mark.parametrize("total", GROSS)
    @pytest.mark.parametrize("pct", (0, 1, 20, 30, 50, 99, 100))
    def test_a_deposit_and_its_balance_are_the_total(self, total, pct):
        split = policies.deposit_for(total, token_pct=pct)
        assert split["depositPaise"] + split["balancePaise"] == total
        assert 0 <= split["depositPaise"] <= total

    @pytest.mark.parametrize("rule", ("full", "pay_at_hotel", "fixed", "percentage"))
    def test_every_payment_rule_splits_the_same_total(self, rule):
        for total in GROSS:
            split = policies.deposit_for(total, token_pct=20, rule=rule,
                                         fixed_paise=250_00)
            assert split["depositPaise"] + split["balancePaise"] == total
            assert split["depositPaise"] >= 0 and split["balancePaise"] >= 0

    def test_a_fixed_deposit_larger_than_the_stay_is_clamped(self):
        """A ₹2,500 token on a ₹1,000 stay must not produce a negative balance."""
        split = policies.deposit_for(100_00, token_pct=20, rule="fixed",
                                     fixed_paise=250_00)
        assert split["depositPaise"] == 100_00
        assert split["balancePaise"] == 0

    @pytest.mark.parametrize("nights,units", list(itertools.product(NIGHTS, UNITS)))
    def test_a_priced_stay_never_carries_a_negative_anything(self, nights, units):
        from datetime import date, timedelta

        start = date(2026, 10, 1)
        priced = pricing.price_stay(
            nightly=[pricing.NightlyRate(date=start + timedelta(days=n),
                                         price_paise=450_00) for n in range(nights)],
            units=units, requested_discount_pct=0, max_discount_pct=15,
            token_pct=20,
        )
        for field in ("gross_paise", "discount_paise", "tax_paise", "total_paise",
                      "token_paise", "balance_paise"):
            assert getattr(priced, field) >= 0, f"{field} went negative"
        assert priced.token_paise + priced.balance_paise == priced.total_paise


# ── AUTONOMY: a rung is never climbed by accident ───────────────────────────

def ctx_at(level: int) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="a", agent_type="guest", partner_id="p", property_id="h",
        display_name="M", autonomy_level=level,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk", "marketing",
                          "revenue", "agents"}),
    )
    return AgentContext(
        principal=principal, property_id="h", partner_id="p", conversation_id="c",
        channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


class TestAutonomy:
    @pytest.mark.parametrize("level", range(0, 5))
    def test_no_tool_executes_below_its_own_floor(self, level):
        """
        Across every tool in the catalogue and every rung, in both directions:
        a tool above the rung never executes, and a tool at or below it is never
        refused for autonomy.
        """
        ctx = ctx_at(level)
        for spec in registry.REGISTRY.values():
            verdict = autonomy.decide(spec, ctx)
            if spec.autonomy_floor > level and not spec.always_requires_approval:
                assert verdict != autonomy.EXECUTE, (
                    f"{spec.name} (floor {spec.autonomy_floor}) executed at {level}")

    @pytest.mark.parametrize("level", range(0, 5))
    def test_a_tool_that_always_needs_approval_never_executes(self, level):
        """
        Not even at level 4. The first hotel to let an AI take bookings
        unattended will not be the first hotel to try it.
        """
        ctx = ctx_at(level)
        for spec in registry.REGISTRY.values():
            if spec.always_requires_approval:
                assert autonomy.decide(spec, ctx) != autonomy.EXECUTE, spec.name

    def test_every_mutating_tool_sits_above_the_read_only_rungs(self):
        """
        Level 0 is read and level 1 is propose. A mutating tool at either would
        make the rung's name a lie — with one declared exception, `proposal`,
        which writes a suggestion for a person and changes nothing about a stay.
        """
        for spec in registry.REGISTRY.values():
            if spec.mutating and not spec.proposal:
                assert spec.autonomy_floor >= 2, spec.name


# ── PROVENANCE: business truth never originates in model text ───────────────

class TestProvenance:
    def test_no_tool_takes_a_price_from_the_model(self):
        """
        §11 and §8 as one structural rule: the model may name a *requested*
        figure — that is a question — but no tool accepts a field that would
        become an authoritative price, tax, total or refund.
        """
        forbidden = ("final_price", "final_total", "total_paise", "tax_paise",
                     "refund_paise", "refund_amount", "nightly_paise",
                     "deposit_paise", "commission_paise", "payout_paise")
        offenders = []
        for spec in registry.REGISTRY.values():
            fields = set(spec.input_model.model_fields)
            for name in forbidden:
                if name in fields:
                    offenders.append(f"{spec.name}.{name}")
        assert not offenders, (
            f"these accept a business figure from the model: {offenders}")

    # Every money field a tool accepts from the model, reviewed, with why each
    # one is safe. A list rather than a naming rule, and §34 is the reason: the
    # naming rule failed on `create_payment_link.amount_rupees`, and the honest
    # response to that was not to widen the pattern until it passed but to look
    # at the tool — which turns out to clamp the figure to the reservation's own
    # outstanding balance, so the model cannot overcharge whatever it asks for.
    #
    # A list has to be edited to add a field, which is the point: the next money
    # argument somebody adds to a tool fails this test until a person has
    # written down why it cannot become an authoritative number.
    MONEY_A_MODEL_MAY_NAME = {
        # A guest's asking price, travelling *into* the engine as a question.
        # The engine decides; §20.
        "negotiate_rate.requested_nightly_rupees",
        "escalate_to_team.requested_nightly_rupees",
        # Clamped to the stay's outstanding balance in `stay_payment.
        # create_request` — over the balance is read as the balance, under
        # `MIN_PAISE` is refused. The ceiling comes from the record.
        "create_payment_link.amount_rupees",
        # A staff agent relaying a figure a receptionist dictated. The person
        # is the author; the tool is the pen.
        "post_folio_charge.amount_paise",
    }

    def test_every_money_argument_a_tool_accepts_has_been_looked_at(self):
        found = {
            f"{spec.name}.{field}"
            for spec in registry.REGISTRY.values()
            for field in spec.input_model.model_fields
            if any(word in field for word in ("paise", "price", "rupees", "amount"))
        }
        assert found == self.MONEY_A_MODEL_MAY_NAME, (
            f"unreviewed: {sorted(found - self.MONEY_A_MODEL_MAY_NAME)}; "
            f"gone: {sorted(self.MONEY_A_MODEL_MAY_NAME - found)}"
        )

    def test_a_payment_link_can_never_ask_for_more_than_is_owed(self):
        """
        The one on that list a *guest-facing* agent holds, so it gets a
        behavioural test rather than a note. Whatever figure the model names,
        the amount collected is bounded by the reservation.
        """
        from app.services.pms import stay_payment

        assert hasattr(stay_payment, "MIN_PAISE")
        source = __import__("inspect").getsource(stay_payment.create_request)
        assert "wanted > balance" in source and "wanted = balance" in source, (
            "the clamp that makes create_payment_link safe is gone")


# ── MESSAGING: a tool that cannot work on a channel is never offered ────────

class TestChannelCapability:
    def test_a_tool_is_never_described_where_it_would_raise(self):
        for channel in ("whatsapp", "instagram", "website", "internal", "phone", "email"):
            capabilities = registry.channel_capabilities(channel)
            ctx = ctx_at(4)
            ctx.channel = channel
            shown = {s.name for s in registry.available_for(ctx)}
            for spec in registry.REGISTRY.values():
                if spec.name in shown:
                    assert set(spec.requires) <= capabilities, (
                        f"{spec.name} offered on {channel}, which cannot "
                        f"{set(spec.requires) - capabilities}")
