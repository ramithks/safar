"""
Answering the same guest message twice must not do the work twice.

**The window.** `messaging/pipeline` stamps `agent_run_id` on a message the
moment `run_turn` returns, and that mark is what stops the next drain answering
it again. But the stamp is a database write, and the task around it is declared
`autoretry_for=(Exception,)` with `acks_late=True`. So there is a real ordering
in which a booking is made and the message is never marked:

    run_turn  →  create_booking_payment_link  →  Razorpay order raised
              →  _mark_answered  ✗ (a connection reset, an OOM, a redeploy)
              →  Celery redelivers  →  the message is still pending
              →  the whole turn runs again  →  a second Razorpay order

Nothing in the turn defends against this. `runtime` deduplicates repeated calls
*within* one turn — `called_writes` — and that set is a local variable which
dies with the turn. Across turns there was nothing at all.

**Why not fix it in the pipeline.** The obvious move is to make the stamp
unfailable, and it is not available: if `_mark_answered` silently swallowed its
error the same message would come back from `_pending` on the very next
iteration of the drain loop and be answered again *inside the same task* — the
same bug, one second sooner. The mark has to be allowed to fail, so the tools
underneath it have to be safe when it does.

**The fix is scope, not luck.** Every mutating tool call carries the id of the
guest message it was made in service of. Answering that message again finds the
call already recorded and returns what it returned the first time, without
running anything. Reads are untouched: re-running `get_quote` costs a moment and
returns the truth, and suppressing it would hand a replay a stale price.
"""
import asyncio
from datetime import date, timedelta

import pytest

from app.services.ai import registry, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.providers import EchoProvider
from tests.fake_supabase import FakeDb, install

registry.load_all()

PROPERTY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
PARTNER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
CONVERSATION = "d0d0d0d0-0001-4001-8001-000000000001"
MESSAGE = "m0m0m0m0-0001-4001-8001-000000000001"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "city": "Anjuna", "currency": "INR", "timezone": "Asia/Kolkata",
        "policies": {}, "status": "active", "settings": {},
    })
    fake.seed("conversations", {
        "id": CONVERSATION, "property_id": PROPERTY, "partner_id": PARTNER,
        "channel": "whatsapp", "handler": "ai", "status": "open",
        "guest_profile": {},
    })
    # Every module that took its own reference to `get_db`, found rather than
    # named — see `fake_supabase.install`.
    install(monkeypatch, fake)
    return fake


def ctx_for(db, *, answering: str | None = MESSAGE) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        conversation_id=CONVERSATION, channel="whatsapp", actor="guest",
        answering=answering,
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


# A mutating tool with no external dependency, so the test is about the guard
# rather than about a payment provider. `create_task` writes a row and returns
# its id, which is exactly the shape that must not happen twice.
TASK = [[("create_task", {"title": "Extra towels please", "category": "housekeeping"})],
        "Ho jayega."]


def tools_run(db) -> list[str]:
    return [a["tool"] for a in db.rows("agent_actions") if a["status"] == "executed"]


class TestOneMessageOneEffect:
    def test_the_same_message_answered_twice_does_the_work_once(self, db):
        """
        The whole defect. Two turns, one guest sentence, one task.

        Both runs must still *reply* — the guest is owed an answer either way —
        and both must record an action row, because a replay that leaves no
        trace is indistinguishable from one that never happened.
        """
        first = run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                                     provider=EchoProvider(list(TASK))))
        second = run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                                      provider=EchoProvider(list(TASK))))

        assert first.status == "succeeded" and second.status == "succeeded"
        assert len(db.rows("tasks")) == 1, "the ticket was raised twice"
        assert tools_run(db).count("create_task") == 1

    def test_the_replay_is_told_what_the_first_run_produced(self, db):
        """
        Returning an error would be worse than running it twice: the model would
        apologise for a ticket that exists. It gets the original result.
        """
        run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        created = db.rows("tasks")[0]["id"]

        second = run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                                      provider=EchoProvider(list(TASK))))
        replayed = [a for a in second.actions if a.tool == "create_task"][0]
        assert replayed.ok
        assert replayed.result_id == created

    def test_the_replay_is_recorded_as_a_replay(self, db):
        """§19 — "what did this run do" must not read as "it made a ticket"."""
        run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))

        statuses = [a["status"] for a in db.rows("agent_actions") if a["tool"] == "create_task"]
        assert statuses == ["executed", "replayed"]

    def test_a_different_message_is_a_different_scope(self, db):
        """
        Two genuine requests are two tickets, even with identical arguments —
        a guest may well ask twice, and the second is not a retry of the first.
        """
        run(runtime.run_turn(ctx_for(db, answering="msg-1"), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        run(runtime.run_turn(ctx_for(db, answering="msg-2"), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        assert len(db.rows("tasks")) == 2

    def test_different_arguments_are_a_different_action(self, db):
        other = [[("create_task", {"title": "Extra pillows please",
                                   "category": "housekeeping"})], "Ho jayega."]
        run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                             provider=EchoProvider(list(other))))
        assert len(db.rows("tasks")) == 2

    def test_a_turn_with_no_scope_is_not_guarded(self, db):
        """
        The staff console and the approval queue call the same tools with no
        guest message behind them. A receptionist pressing a button twice means
        it twice, and inventing a scope for them would be the system overruling
        a person.
        """
        run(runtime.run_turn(ctx_for(db, answering=None), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        run(runtime.run_turn(ctx_for(db, answering=None), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        assert len(db.rows("tasks")) == 2


class TestReadsAreNotGuarded:
    def test_a_read_runs_again_on_a_replay(self, db):
        """
        Suppressing a read would be its own bug: the replay would answer from a
        price fetched before whatever failed, and the whole reason a turn is
        being replayed is that something went wrong in between.
        """
        script = [[("get_property", {})], "Casa Mandala, Anjuna."]
        run(runtime.run_turn(ctx_for(db), "Hotel kahan hai?",
                             provider=EchoProvider(list(script))))
        run(runtime.run_turn(ctx_for(db), "Hotel kahan hai?",
                             provider=EchoProvider(list(script))))
        assert tools_run(db).count("get_property") == 2


class TestTheGuardIsNotAWayToSkipAGate:
    def test_a_refused_call_is_not_remembered_as_done(self, db):
        """
        Only a call that *succeeded* is replayable. A refusal must be reachable
        again — the second attempt may come from a run at a different autonomy
        level, or after a grant changed, and answering it from a stale refusal
        would make a permission fix invisible until the next message.
        """
        ctx = ctx_for(db)
        ctx.tool_grants.pop("create_task")
        run(runtime.run_turn(ctx, "Towels bhej do", provider=EchoProvider(list(TASK))))
        assert not db.rows("tasks")

        run(runtime.run_turn(ctx_for(db), "Towels bhej do",
                             provider=EchoProvider(list(TASK))))
        assert len(db.rows("tasks")) == 1

    def test_a_tool_that_failed_is_reachable_again_and_not_replayed_as_done(self, db):
        """
        **The case that makes the `status = 'executed'` filter load-bearing.**

        Found by mutation testing, and it took two attempts to find the right
        case, which is worth recording. Deleting the filter broke no test at
        first because the only refusal the suite exercised — a revoked grant —
        happens *before* the arguments are validated, so its outcome carries no
        key for the lookup to find. Then a queued approval, which does carry a
        key, also failed to kill it: the autonomy gate returns
        `awaiting_approval` above the lookup, so a second attempt never reaches
        it. That ordering is its own guarantee and has its own test below.

        What the filter actually guards is a tool that ran and *failed* — a
        Razorpay timeout on `create_booking_payment_link`, a 500 from the PMS.
        That outcome is produced below the gate, so it carries a key and the
        lookup does find it. Without the filter the retry is told the work is
        done, with the failed row's empty output, and the guest is left with a
        payment link that was never created.

        A failure must stay reachable. That is the whole difference between a
        guard against duplication and a guard against ever trying again.
        """
        from app.services.ai.errors import ToolUnavailable

        broken = {"title": "Extra blankets please", "category": "housekeeping"}
        spec = registry.REGISTRY["create_task"]
        original = spec.fn

        async def _explodes(_args, _ctx):
            raise ToolUnavailable("the ticket system is down")

        object.__setattr__(spec, "fn", _explodes)
        try:
            first = run(runtime.run_turn(
                ctx_for(db), "blankets bhejo",
                provider=EchoProvider([[("create_task", broken)], "Dekhta hoon."])))
        finally:
            object.__setattr__(spec, "fn", original)

        failed = [a for a in first.actions if a.tool == "create_task"][0]
        assert failed.status == "failed"
        assert failed.idempotency_key, "a failure below the gate must carry the key"

        # The ticket system is back. The same message, answered again, must
        # actually raise the ticket.
        second = run(runtime.run_turn(
            ctx_for(db), "blankets bhejo",
            provider=EchoProvider([[("create_task", broken)], "Ho jayega."])))
        again = [a for a in second.actions if a.tool == "create_task"][0]
        assert again.status == "executed", (
            f"a failed call was replayed as {again.status} — the guest never "
            f"gets the thing they asked for"
        )
        assert len(db.rows("tasks")) == 1

    def test_an_action_waiting_for_a_human_never_reaches_the_lookup_at_all(self, db):
        """
        The gate order, asserted as its own guarantee.

        `create_booking` always requires approval, so the autonomy verdict
        returns above the idempotency lookup and a second attempt queues behind
        the same human rather than being told it is done. Replaying an
        `awaiting_approval` row as a success would hand the model a booking that
        does not exist — the single worst sentence this product can send,
        arrived at through the machinery meant to prevent duplicates.
        """
        booking = ("create_booking",
                   {"quote_id": "40000000-0000-4000-8000-000000000001",
                    "guest_name": "Nisha Kapoor", "guest_phone": "+919812345678"})
        script = [[booking], "Main team se confirm karwa raha hoon."]

        # Through the runtime, not `registry.execute` directly: the action row
        # the lookup reads is written by `runtime._write_action`, so a direct
        # call leaves nothing for a replay to find and the test would pass
        # whatever the filter said.
        first = run(runtime.run_turn(ctx_for(db), "book kar do",
                                     provider=EchoProvider(list(script))))
        queued = [a for a in first.actions if a.tool == "create_booking"][0]
        assert queued.status == "awaiting_approval", queued.refusal_reason
        assert queued.idempotency_key, (
            "a queued action must carry the key, or the filter below is untested"
        )

        second = run(runtime.run_turn(ctx_for(db), "book kar do",
                                      provider=EchoProvider(list(script))))
        again = [a for a in second.actions if a.tool == "create_booking"][0]
        assert again.status == "awaiting_approval", (
            f"a booking waiting for a human was replayed as {again.status}"
        )
        assert not again.output, "a booking that has not happened has no result"
