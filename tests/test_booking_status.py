"""
"Payment hogyi" — and whether the system can answer it.

A guest paid a real hotel at 19:58:52. Twenty-six seconds later he asked whether
it had gone through, and the agent told him his booking would be confirmed once
the payment processed. It already had; the reservation had existed for half a
minute under reference TB-5CECKV. The next message escalated the conversation to
a human who was not there, and the sale ended in silence.

Nothing was broken in the money path. The order settled, the webhook fired, the
reservation was written. What was missing was any thread connecting the three to
the conversation the guest was sitting in — so every test here is about that
connection, and about the two things that hang off it:

  * `get_booking_status` reads this conversation's own money state, composed
    from the three tables that each own part of it.
  * the guardrail lets the agent say "confirmed" when, and only when, the tool
    came back holding a confirmed booking.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.services.ai import guardrails, registry
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.errors import ToolUnavailable
from app.services.ai.schemas import ToolOutcome
from tests.fake_supabase import FakeDb

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
OTHER_CONVERSATION = "c0c0c0c0-0002-4002-8002-000000000002"


def run(coro):
    return asyncio.run(coro)


def later(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    monkeypatch.setattr("app.services.ai.tools.booking.get_db", lambda: fake)
    return fake


def ctx(conversation_id: str | None = CONVERSATION) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"rates", "tasks", "messaging"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        model="test", conversation_id=conversation_id, actor="guest",
        tool_grants={"get_booking_status": {"tool_name": "get_booking_status",
                                            "enabled": True}},
    )


def status(conversation_id: str | None = CONVERSATION) -> dict:
    outcome = run(registry.execute("get_booking_status", {}, ctx(conversation_id)))
    assert outcome.ok, outcome.refusal_reason or outcome.error
    return outcome.output


# ── what the conversation knows ──────────────────────────────────────────────

def test_nothing_at_all(db):
    assert status()["state"] == "NONE"


def test_a_live_quote_is_not_a_booking(db):
    db.seed("quotes", {
        "id": "q1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "status": "active", "check_in": "2026-09-10", "check_out": "2026-09-14",
        "units": 2, "adults": 4, "children": 0, "total_paise": 3584000,
        "expires_at": later(10), "created_at": "2026-09-01T00:00:00+00:00",
    })
    out = status()
    assert out["state"] == "QUOTE_ACTIVE"
    assert out["quoteId"] == "q1"
    assert "nothing is booked" in out["instruction"].lower()


def test_an_expired_quote_is_nothing(db):
    db.seed("quotes", {
        "id": "q1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "status": "active", "total_paise": 3584000, "expires_at": later(-1),
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    assert status()["state"] == "NONE"


def test_a_link_was_sent_and_nothing_came_back(db):
    db.seed("payment_orders", {
        "id": "o1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "purpose": "booking", "status": "created", "amount_paise": 716800,
        "razorpay_order_id": "order_TSXk6xnvkoawOc",
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    out = status()
    assert out["state"] == "PAYMENT_PENDING"
    # The whole point. The guest saying they paid must not become the answer.
    assert "never say it is confirmed because the guest says they paid" \
        in out["instruction"].lower()


def test_the_money_arrived_but_the_booking_is_not_written_yet(db):
    """
    Fulfilment is mid-flight, or it failed. Either way the guest is told their
    payment landed and *not* that they have a room, because no room has left
    inventory.
    """
    db.seed("payment_orders", {
        "id": "o1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "purpose": "booking", "status": "paid", "amount_paise": 716800,
        "razorpay_order_id": "order_x", "created_at": "2026-09-01T00:00:00+00:00",
    })
    out = status()
    assert out["state"] == "PAYMENT_RECEIVED"
    assert "do not say the booking is confirmed yet" in out["instruction"].lower()


def test_a_failed_payment_says_so(db):
    db.seed("payment_orders", {
        "id": "o1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "purpose": "booking", "status": "failed", "amount_paise": 716800,
        "razorpay_order_id": "order_x", "failure_reason": "payment_failed",
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    out = status()
    assert out["state"] == "PAYMENT_FAILED"
    assert out["failureReason"] == "payment_failed"


def test_the_transcript_that_started_this(db):
    """The real numbers, from the real conversation, answered correctly."""
    db.seed("payment_orders", {
        "id": "o1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "purpose": "booking", "status": "paid", "amount_paise": 716800,
        "razorpay_order_id": "order_TSXk6xnvkoawOc",
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    db.seed("reservations", {
        "id": "r1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "ref": "TB-5CECKV", "status": "confirmed", "payment_status": "token_paid",
        "check_in": "2026-09-10", "check_out": "2026-09-14", "nights": 4,
        "units": 2, "adults": 4, "children": 0, "guest_name": "Sahil",
        "total_paise": 3584000, "paid_paise": 716800,
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    out = status()
    assert out["state"] == "CONFIRMED"
    assert out["ref"] == "TB-5CECKV"
    assert out["paidPaise"] == 716800
    assert out["balancePaise"] == 3584000 - 716800
    assert out["bookingUrl"].endswith("/checkin/TB-5CECKV")


def test_a_reservation_outranks_its_own_order(db):
    """Newest fact wins: an order that settled has a reservation to report."""
    db.seed("payment_orders", {
        "id": "o1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "purpose": "booking", "status": "paid", "amount_paise": 100,
        "razorpay_order_id": "order_x", "created_at": "2026-09-01T00:00:00+00:00",
    })
    db.seed("reservations", {
        "id": "r1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "ref": "TB-1", "status": "confirmed", "payment_status": "paid",
        "total_paise": 100, "paid_paise": 100,
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    assert status()["state"] == "CONFIRMED"


def test_a_cancelled_stay_is_never_described_as_active(db):
    db.seed("reservations", {
        "id": "r1", "property_id": PROPERTY, "conversation_id": CONVERSATION,
        "ref": "TB-1", "status": "cancelled", "payment_status": "refunded",
        "total_paise": 100, "paid_paise": 0,
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    out = status()
    assert out["state"] == "CANCELLED"
    assert "request_human_handoff" in out["instruction"]


# ── whose booking ────────────────────────────────────────────────────────────

def test_another_conversations_booking_is_invisible(db):
    """
    The tool takes no arguments, so there is no phrasing that reaches somebody
    else's stay. This proves the query is scoped as well as the signature.
    """
    db.seed("reservations", {
        "id": "r1", "property_id": PROPERTY, "conversation_id": OTHER_CONVERSATION,
        "ref": "TB-SOMEONE-ELSE", "status": "confirmed", "payment_status": "paid",
        "total_paise": 100, "paid_paise": 100,
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    assert status()["state"] == "NONE"


def test_another_propertys_booking_is_invisible(db):
    db.seed("reservations", {
        "id": "r1", "property_id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "conversation_id": CONVERSATION, "ref": "TB-OTHER-HOTEL",
        "status": "confirmed", "payment_status": "paid",
        "total_paise": 100, "paid_paise": 100,
        "created_at": "2026-09-01T00:00:00+00:00",
    })
    assert status()["state"] == "NONE"


def test_a_run_with_no_conversation_refuses(db):
    outcome = run(registry.execute("get_booking_status", {}, ctx(conversation_id=None)))
    assert not outcome.ok
    assert isinstance(outcome, ToolOutcome)


# ── what the agent is then allowed to say ────────────────────────────────────

def outcome(state: str) -> ToolOutcome:
    return ToolOutcome(
        tool="get_booking_status", status="executed",
        input={}, output={"state": state, "ref": "TB-5CECKV"},
    )


CONFIRMATION = "Payment mil gaya, aapki booking confirm ho gayi hai — TB-5CECKV"


def test_a_confirmed_booking_licenses_the_sentence():
    verdict = guardrails.check_reply(
        CONFIRMATION, tool_outputs=[outcome("CONFIRMED")], trusted_hosts=set(),
    )
    assert verdict.ok, verdict.reason


def test_a_pending_payment_does_not_license_it():
    """
    The reason `get_booking_status` cannot simply join CONFIRMING_TOOLS. It runs
    on every payment question, and on most of them the honest answer is that
    nothing has arrived.
    """
    verdict = guardrails.check_reply(
        CONFIRMATION, tool_outputs=[outcome("PAYMENT_PENDING")], trusted_hosts=set(),
    )
    assert not verdict.ok
    assert "secured" in verdict.reason


def test_a_failed_tool_call_does_not_license_it():
    failed = ToolOutcome(tool="get_booking_status", status="failed", input={},
                         output=None, error="boom")
    verdict = guardrails.check_reply(
        CONFIRMATION, tool_outputs=[failed], trusted_hosts=set(),
    )
    assert not verdict.ok


def test_nothing_at_all_still_refuses():
    verdict = guardrails.check_reply(CONFIRMATION, tool_outputs=[], trusted_hosts=set())
    assert not verdict.ok
