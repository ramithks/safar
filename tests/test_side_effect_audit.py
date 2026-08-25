"""
Every action that changes something, and what stops it happening twice.

§3 asks for an audit of every side-effecting operation against four questions:
what happens if the process dies after the effect but before the result, what
happens if the turn is retried, what happens if a fallback model repeats the
call, and can a duplicate order, deduction, cancellation, message or approval be
created.

**Written as a test rather than a document** because a document goes stale the
next time somebody adds a tool, and this is exactly the list that must not. A new
mutating tool fails this file until a person has said which guard covers it.

There are three durable guards in the system, and between them they have to
cover every mutating tool:

    the turn        `runtime.called_writes` — the same call twice inside one
                    turn is one call. Dies with the turn; covers the model
                    stuttering, a retry, and a fallback model repeating itself.

    the message     `registry.idempotency_key` — the same call in service of the
                    same inbound guest message is one call. Durable, survives
                    process death, and covers a Celery redelivery.

    the claim       `approvals.claim` — a compare-and-set on the action row.
                    Covers the path that has no guest message: a human pressing
                    a button, twice or in two places at once.

Anything a guest turn can reach is covered by the first two. Anything that always
requires approval is covered by the third. A tool covered by none of them is a
duplicate waiting for a bad afternoon.
"""
import inspect

import pytest

from app.services.ai import approvals, registry, runtime

registry.load_all()

MUTATING = sorted(s.name for s in registry.REGISTRY.values() if s.mutating)


def test_there_are_mutating_tools_to_audit():
    """A guard against this file quietly passing because the list went empty."""
    assert len(MUTATING) > 20, MUTATING


class TestTheThreeGuardsExist:
    def test_the_turn_guard_never_reopens_for_a_write(self):
        """
        Reads reopen after a mutation — the correct shape for a verification
        step is read → write → read, and the second read has the same arguments
        as the first by definition. Writes never do: that is the whole safety
        property, and a `called_writes.clear()` anywhere would remove it.
        """
        source = inspect.getsource(runtime.run_turn)
        assert "called_writes.add(fingerprint)" in source
        assert "called_reads.clear()" in source
        assert "called_writes.clear()" not in source

    def test_the_message_guard_is_below_every_gate(self):
        """
        A replay is a shortcut past the *work*, never past a rule. If the guard
        moved above the autonomy verdict, a grant revoked between two attempts
        would stop being enforced on the second.
        """
        source = inspect.getsource(registry.execute)
        gate = source.index("verdict = autonomy.EXECUTE")
        guard = source.index("if spec.mutating and ctx.answering")
        assert gate < guard, "the idempotency guard moved above the autonomy gate"

    def test_the_message_guard_only_replays_a_success(self):
        """
        A refusal must stay reachable — the next attempt may run at a different
        autonomy level or after a grant was fixed, and answering it from a stale
        refusal would hide the fix until the guest wrote again.
        """
        source = inspect.getsource(registry._already_done)
        assert '.eq("status", "executed")' in source

    def test_the_claim_is_a_compare_and_set(self):
        source = inspect.getsource(approvals.claim)
        assert '.eq("status", "awaiting_approval")' in source, (
            "the claim stopped being conditional, so two presses both win"
        )


class TestEveryMutatingToolIsCovered:
    @pytest.mark.parametrize("name", MUTATING)
    def test_it_is_reachable_only_through_the_single_door(self, name):
        """
        The guards live in `registry.execute`. A tool body that could be called
        around it would be outside all three.
        """
        spec = registry.REGISTRY[name]
        body = inspect.getsource(spec.fn)
        assert "registry.execute" not in body, (
            f"{name} calls the door it is supposed to come through"
        )

    @pytest.mark.parametrize("name", MUTATING)
    def test_it_sits_high_enough_on_the_ladder_to_be_gated(self, name):
        """
        Level 0 reads and level 1 proposes. A mutating tool below level 2 would
        execute for an agent whose rung promises it cannot — with the one
        declared exception, a proposal, which writes a suggestion for a person
        and changes nothing about a stay.
        """
        spec = registry.REGISTRY[name]
        floor = 1 if spec.proposal else 2
        assert spec.autonomy_floor >= floor, f"{name} at floor {spec.autonomy_floor}"

    # Money tools whose amount is bounded by a record the agent did not create,
    # and which therefore sit below the transactional rung on purpose.
    #
    # **This list exists because the rule below was written too coarsely and
    # `create_payment_link` failed it.** The first version grouped by result
    # type, which puts "collect a balance this guest already owes" and "price
    # and sell a new stay" in the same bucket. They are not alike:
    # `create_booking_payment_link` prices a stay and sits at floor 3 beside
    # `get_quote` and `create_booking`; `create_payment_link` asks for money
    # that is already owed, on a reservation that already exists, clamped in
    # `stay_payment.create_request` to the outstanding balance — over it reads
    # as the balance, under `MIN_PAISE` is refused. It takes no inventory, bills
    # nothing new, and cannot overcharge whatever the model asks for.
    #
    # A list rather than a widened rule, so the next payment tool somebody adds
    # fails this test until a person has written down which case it is in.
    BOUNDED_BY_AN_EXISTING_RECORD = {"create_payment_link"}

    @pytest.mark.parametrize("name", MUTATING)
    def test_the_money_and_inventory_tools_are_transactional_or_approved(self, name):
        """
        The heavy end of the list. A tool that moves stock, or that bills or
        collects money the agent itself decided the size of, either sits at the
        transactional rung — a hotel has to have said the AI may do this
        unattended — or always requires a person. Several do both.
        """
        spec = registry.REGISTRY[name]
        heavy = spec.result_type in ("reservation", "folio_entry", "payment_link")
        if not heavy:
            pytest.skip(f"{name} moves neither stock nor money")
        if name in self.BOUNDED_BY_AN_EXISTING_RECORD:
            assert spec.autonomy_floor >= 2, name
            return
        assert spec.autonomy_floor >= 3 or spec.always_requires_approval, name

    def test_the_bounded_ones_really_are_bounded(self):
        """
        The exemption above is only safe while the clamp is there. Asserted
        rather than trusted, because the exemption outlives whoever wrote it.
        """
        from app.services.pms import stay_payment

        source = inspect.getsource(stay_payment.create_request)
        assert "wanted > balance" in source and "wanted = balance" in source
        assert "MIN_PAISE" in source


class TestWhatTheAnswersActuallyAre:
    """
    §3's four questions, answered against the code rather than asserted in prose.
    Each of these would be a duplicate booking if it stopped being true.
    """

    def test_a_process_that_dies_before_the_mark_replays_into_the_guard(self):
        """
        Q1 and Q2. `_mark_answered` is a database write inside a task declared
        `acks_late` with `autoretry_for=(Exception,)`, so the turn can run twice
        — and the second run finds every mutating call already recorded under
        the same message id.
        """
        from app.services.messaging import pipeline

        source = inspect.getsource(pipeline.process_conversation)
        assert "ctx.answering = message[\"id\"]" in source, (
            "the turn no longer carries the message it is answering, so the "
            "durable guard has no scope"
        )
        assert source.index("ctx.answering") < source.index("runtime.run_turn")

    def test_a_fallback_model_repeating_a_call_meets_the_turn_guard(self):
        """
        Q3. The ladder re-issues the *model* call; the tool results already in
        the message list are not re-executed. If the second model asks for a
        tool the first already called, the fingerprint is still in
        `called_writes` — the set outlives the recovery because it is scoped to
        the turn, not to the attempt.
        """
        source = inspect.getsource(runtime.run_turn)
        writes = source.index("called_writes: set")
        ladder = source.index("async def _answering_step")
        assert writes < ladder, "the ladder was declared outside the turn's dedup scope"

    def test_an_approved_action_cannot_be_run_twice(self):
        """Q8, and the one the claim was written for."""
        source = inspect.getsource(approvals.execute_approved)
        claim_at = source.index("_claim_for")
        execute_at = source.index("registry.execute")
        assert claim_at < execute_at, "the tool runs before the action is claimed"

    def test_an_outbound_message_carries_a_client_reference(self):
        """
        Q7. The pipeline's own delivery is keyed on the run, so a retried task
        cannot send the same reply twice.
        """
        from app.services.messaging import pipeline

        assert "client_reference=f\"run:{result.run_id}\"" in inspect.getsource(
            pipeline._deliver)
