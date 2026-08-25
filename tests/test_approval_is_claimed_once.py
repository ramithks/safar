"""
Two managers press Approve at the same moment.

§3 asks it directly — *"can two approvals be created?"*, *"can inventory be
deducted twice?"* — and the answer, before this, was yes under a race.

`execute_approved` guarded with a read: load the action, check
`status in ('awaiting_approval', 'approved')`, then run the tool. Between the
read and the write there is a whole tool execution — a booking, a folio charge,
a negotiated rate — and nothing holds the row. Two requests that both read
`awaiting_approval` both proceed, and the second settles over the first.

**It is not covered by the idempotency key either.** That guard is scoped to
`ctx.answering`, the guest message a turn is answering, and the approval path has
no guest message: it is a person pressing a button minutes or hours later. So the
one place in the system where a mutating tool is *guaranteed* to run — every
`create_booking`, every `post_folio_charge` — had the weakest protection against
running twice.

A shared inbox makes this ordinary rather than exotic. Two people see the same
pending approval on their phones and both press it.

**The fix is a claim, not a key.** The status transition becomes a conditional
update — `set status='approved' where id=? and status='awaiting_approval'` — and
the caller proceeds only if a row came back. Postgres serialises the two updates;
the loser gets nothing and is told the action is already decided. It is smaller
than an idempotency column, it needs no migration, and it makes the state machine
enforce its own transitions rather than describing them.
"""
import asyncio

import pytest

from app.services.ai import approvals, registry
from tests.fake_supabase import FakeDb, install

registry.load_all()

PROPERTY = "aaaaaaaa-7000-4000-8000-000000000001"
PARTNER = "bbbbbbbb-7000-4000-8000-000000000001"
RUN = "cccccccc-7000-4000-8000-000000000001"
ACTION = "dddddddd-7000-4000-8000-000000000001"
AGENT = "eeeeeeee-7000-4000-8000-000000000001"


def run(coro):
    return asyncio.run(coro)


class Member:
    profile_id = "member-1"
    id = "member-1"


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", {
        "id": PROPERTY, "partner_id": PARTNER, "name": "Casa Mandala",
        "currency": "INR", "timezone": "Asia/Kolkata", "policies": {},
        "status": "active", "settings": {},
    })
    fake.seed("agents", {
        "id": AGENT, "property_id": PROPERTY, "partner_id": PARTNER,
        "agent_type": "guest", "status": "active", "name": "Mandala",
        "autonomy_level": 3, "active_config_id": "config-1", "limits": {},
    })
    fake.seed("agent_configs", {
        "id": "config-1", "agent_id": AGENT, "version": 1, "model": "",
        "provider": "vertex_ai", "params": {}, "system_prompt": None,
    })
    fake.seed("agent_permissions", {
        "id": "perm-1", "agent_id": AGENT, "scope": "tasks", "granted": True,
    })
    fake.seed("agent_tools", {
        "id": "grant-1", "agent_id": AGENT, "tool_name": "create_task",
        "enabled": True, "requires_approval": True,
    })
    fake.seed("agent_runs", {
        "id": RUN, "property_id": PROPERTY, "partner_id": PARTNER,
        "agent_id": AGENT, "conversation_id": None, "model_tier": "conversation",
        "member_id": None,
    })
    fake.seed("agent_actions", {
        "id": ACTION, "run_id": RUN, "property_id": PROPERTY, "partner_id": PARTNER,
        "agent_id": AGENT, "seq": 1, "tool": "create_task",
        "input": {"title": "Extra towels please", "category": "housekeeping"},
        "status": "awaiting_approval", "decision_source": "model",
        "created_at": "2099-01-01T00:00:00+00:00",
    })
    install(monkeypatch, fake)
    return fake


def test_claiming_an_action_twice_only_claims_it_once(db):
    """
    The transition itself, in isolation. Everything below rests on this.
    """
    first = approvals.claim(ACTION, PROPERTY, approver="member-1")
    second = approvals.claim(ACTION, PROPERTY, approver="member-2")
    assert first is not None, "the first press must win"
    assert second is None, "the second press must find nothing left to claim"


def test_the_claim_records_who_won(db):
    approvals.claim(ACTION, PROPERTY, approver="member-1")
    approvals.claim(ACTION, PROPERTY, approver="member-2")
    row = db.rows("agent_actions")[0]
    assert row["approved_by"] == "member-1"
    assert row["status"] == "approved"


def test_an_action_already_executed_cannot_be_claimed(db):
    """
    The other direction, and the one that matters after a crash: a process that
    died between executing the tool and settling the row leaves `approved`, not
    `awaiting_approval`, so a retry cannot run the tool a second time.
    """
    db.rows("agent_actions")[0]["status"] = "executed"
    assert approvals.claim(ACTION, PROPERTY, approver="member-1") is None


def test_an_action_that_failed_cannot_be_claimed_again(db):
    """
    Failure is terminal by design — the recovery is a fresh proposal against the
    hotel as it is now, not another press of the same button.
    """
    db.rows("agent_actions")[0]["status"] = "failed"
    assert approvals.claim(ACTION, PROPERTY, approver="member-1") is None


def test_the_claim_is_scoped_to_the_property(db):
    """
    An action id is a uuid, and a uuid from another hotel must not be claimable
    — the same rule every other read in this system follows.
    """
    assert approvals.claim(ACTION, "ffffffff-0000-4000-8000-000000000009",
                           approver="member-1") is None
    assert db.rows("agent_actions")[0]["status"] == "awaiting_approval"


def test_two_approvals_produce_one_business_effect(db):
    """
    The whole point, end to end. Two managers, one ticket.
    """
    outcomes = [
        run(approvals.execute_approved(action_id=ACTION, property_id=PROPERTY,
                                       member=Member()))
        for _ in range(2)
    ]
    assert outcomes[0].ok is True
    assert outcomes[1].ok is False
    assert outcomes[1].code == "ALREADY_DECIDED"
    assert len(db.rows("tasks")) == 1, "the ticket was raised twice"


def test_the_loser_is_told_something_a_person_can_act_on(db):
    run(approvals.execute_approved(action_id=ACTION, property_id=PROPERTY,
                                   member=Member()))
    second = run(approvals.execute_approved(action_id=ACTION, property_id=PROPERTY,
                                            member=Member()))
    assert "already" in (second.detail or "").lower()
    assert second.status in ("executed", "approved")
