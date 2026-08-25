"""
Hotel A's guest, reaching for Hotel B's rows.

**Mission 19, and it had never been tested.** Every guest-facing tool that takes
an id mentions `property_id` somewhere in its source — which is encouraging and
is not proof. The id might be used before the check, the filter might be on a
different query, or the check might be on a row already fetched by id alone.

So this attempts it. Two properties, each with its own rooms, rate plans,
quotes, reservations and tasks; a context scoped to the first; and every id the
second owns, passed in.

**An id is a uuid.** A model that has seen one in a prior conversation, a guest
who pastes one, a bug that carries one across a thread — all of them arrive here
as a well-formed identifier that belongs to somebody else. The rule is that
ownership is resolved server-side from `ctx.property_id`, which the model cannot
name, cannot guess and cannot be talked into changing.
"""
import asyncio
from datetime import date, timedelta

import pytest

from app.services.ai import registry
from app.services.ai.context import AgentContext, AgentPrincipal
from tests.fake_supabase import FakeDb, install

registry.load_all()

# Ours.
A = "aaaaaaaa-0000-4000-8000-00000000000a"
A_PARTNER = "aaaaaaaa-1111-4000-8000-00000000000a"
A_CONVERSATION = "aaaaaaaa-2222-4000-8000-00000000000a"
A_ROOM = "aaaaaaaa-3333-4000-8000-00000000000a"
A_PLAN = "aaaaaaaa-4444-4000-8000-00000000000a"

# Theirs. Every id below is well-formed, real, and none of it is ours.
B = "bbbbbbbb-0000-4000-8000-00000000000b"
B_PARTNER = "bbbbbbbb-1111-4000-8000-00000000000b"
B_CONVERSATION = "bbbbbbbb-2222-4000-8000-00000000000b"
B_ROOM = "bbbbbbbb-3333-4000-8000-00000000000b"
B_PLAN = "bbbbbbbb-4444-4000-8000-00000000000b"
B_QUOTE = "bbbbbbbb-5555-4000-8000-00000000000b"
B_RESERVATION = "bbbbbbbb-6666-4000-8000-00000000000b"
B_TASK = "bbbbbbbb-7777-4000-8000-00000000000b"
B_HOLD = "bbbbbbbb-8888-4000-8000-00000000000b"

CHECK_IN = date.today() + timedelta(days=30)
CHECK_OUT = CHECK_IN + timedelta(days=2)


def run(coro):
    return asyncio.run(coro)


def _property(pid, partner, name):
    return {"id": pid, "partner_id": partner, "name": name, "city": "Anjuna",
            "currency": "INR", "timezone": "Asia/Kolkata", "policies": {},
            "status": "active", "settings": {}, "tax_inclusive": False,
            "check_in_time": "14:00", "check_out_time": "11:00"}


def _room(rid, pid, partner, name, code):
    return {"id": rid, "property_id": pid, "partner_id": partner, "name": name,
            "code": code, "total_units": 6, "max_occupancy": 3,
            "standard_occupancy": 2, "is_active": True, "sort_order": 1}


def _plan(plid, pid, partner, room):
    return {"id": plid, "property_id": pid, "partner_id": partner,
            "room_type_id": room, "code": "RO", "name": "Room only",
            "meal_plan": "RO", "audience": "public", "refundable": True,
            "inclusions": None, "base_price_paise": 450000, "min_stay": 1,
            "derived_from_plan_id": None, "derive_discount_pct": None,
            "valid_from": None, "valid_to": None, "days_of_week": None,
            "allocation_units": None, "is_active": True,
            "cancellation_cutoff_hours": 48, "cancellation_policy": None}


@pytest.fixture
def db(monkeypatch) -> FakeDb:
    fake = FakeDb()
    fake.seed("properties", _property(A, A_PARTNER, "Casa Mandala"),
              _property(B, B_PARTNER, "Somewhere Else"))
    fake.seed("conversations",
              {"id": A_CONVERSATION, "property_id": A, "partner_id": A_PARTNER,
               "channel": "whatsapp", "handler": "ai", "status": "open",
               "guest_profile": {}},
              {"id": B_CONVERSATION, "property_id": B, "partner_id": B_PARTNER,
               "channel": "whatsapp", "handler": "ai", "status": "open",
               "guest_profile": {}})
    fake.seed("room_types", _room(A_ROOM, A, A_PARTNER, "Deluxe Balcony", "DLX"),
              _room(B_ROOM, B, B_PARTNER, "Sea View Double", "SVD"))
    fake.seed("rate_plans", _plan(A_PLAN, A, A_PARTNER, A_ROOM),
              _plan(B_PLAN, B, B_PARTNER, B_ROOM))

    # Hotel B's business rows. Every one of these is a real, live record.
    fake.seed("quotes", {
        "id": B_QUOTE, "property_id": B, "partner_id": B_PARTNER,
        "room_type_id": B_ROOM, "rate_plan_id": B_PLAN,
        "conversation_id": B_CONVERSATION, "status": "active",
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
        "units": 1, "adults": 2, "children": 0, "total_paise": 900000,
        "hold_id": B_HOLD, "expires_at": "2099-01-01T00:00:00+00:00"})
    fake.seed("reservations", {
        "id": B_RESERVATION, "property_id": B, "partner_id": B_PARTNER,
        "ref": "TB-SECRET", "room_type_id": B_ROOM, "rate_plan_id": B_PLAN,
        "conversation_id": B_CONVERSATION, "status": "confirmed",
        "guest_name": "Somebody Else", "guest_phone": "+919000000000",
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT), "nights": 2,
        "units": 1, "adults": 2, "children": 0, "total_paise": 900000,
        "paid_paise": 900000, "payment_status": "paid"})
    fake.seed("tasks", {
        "id": B_TASK, "property_id": B, "partner_id": B_PARTNER,
        "code": "#1801", "title": "Fix the tap in 301", "status": "open",
        "category": "maintenance", "priority": "normal"})
    fake.seed("inventory_holds", {
        "id": B_HOLD, "property_id": B, "partner_id": B_PARTNER,
        "room_type_id": B_ROOM, "status": "active", "units": 1,
        "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT)})
    # Both hotels have rooms they can actually sell. Without this every quote
    # fails for want of inventory, and the cross-tenant tests above would pass
    # because *everything* fails — which is why `TestOurOwnIdsStillWork` exists
    # and why it has to be green for any of this to mean anything.
    def _availability(params):
        start = date.fromisoformat(params["p_from"])
        end = date.fromisoformat(params["p_to"])
        out, day = [], start
        while day < end:
            for room in (A_ROOM, B_ROOM):
                out.append({"room_type_id": room, "date": day.isoformat(),
                            "units_left": 6, "stop_sell": False})
            day += timedelta(days=1)
        return out

    fake.rpcs["property_availability"] = _availability
    fake.rpcs["property_rates"] = lambda params: []
    fake.rpcs["hold_inventory"] = lambda params: "cccccccc-0000-4000-8000-00000000000c"
    fake.rpcs["release_hold"] = lambda params: True
    install(monkeypatch, fake)
    return fake


def ours() -> AgentContext:
    """A context for Hotel A. Nothing below may reach out of it."""
    principal = AgentPrincipal(
        agent_id="agent-a", agent_type="guest", partner_id=A_PARTNER,
        property_id=A, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}))
    return AgentContext(
        principal=principal, property_id=A, partner_id=A_PARTNER,
        conversation_id=A_CONVERSATION, channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY})


def reached(outcome, *needles: str) -> bool:
    """Did any of the other hotel's data come back?"""
    blob = f"{outcome.output!r} {outcome.model_message!r} {outcome.refusal_reason!r}"
    return any(n in blob for n in needles)


# ── every guest tool that takes somebody else's id ──────────────────────────

class TestAnotherHotelsIdsAreRefused:
    def test_a_quote(self, db):
        out = run(registry.execute("create_booking_payment_link",
                                   {"quote_id": B_QUOTE}, ours()))
        assert not out.ok, "Hotel B's quote was charged for on Hotel A's thread"
        assert not reached(out, "900000", "₹9,000")

    def test_a_reservation_through_the_payment_link(self, db):
        out = run(registry.execute("create_payment_link",
                                   {"reservation_id": B_RESERVATION}, ours()))
        assert not out.ok
        assert not reached(out, "TB-SECRET", "Somebody Else")

    def test_a_reservation_through_the_lookup(self, db):
        out = run(registry.execute("get_reservation",
                                   {"reservation_id": B_RESERVATION}, ours()))
        assert not out.ok
        assert not reached(out, "TB-SECRET", "Somebody Else", "+919000000000")

    def test_a_reservation_through_the_check_in_link(self, db):
        out = run(registry.execute("get_check_in_link",
                                   {"reservation_id": B_RESERVATION}, ours()))
        assert not out.ok
        assert not reached(out, "TB-SECRET")

    def test_a_room_type(self, db):
        """
        **Not a refusal, and the design is better for it.** The tool succeeds
        and answers "there is no such room here" — which is true, is what the
        model needs to say, and cannot be told apart from a room that never
        existed. A raised error would leak the fact that the id is real
        somewhere.

        Asserted precisely rather than as "failed or did not leak": an `or`
        passes when the behaviour changes to something worse.
        """
        out = run(registry.execute("get_room_details",
                                   {"room_type_id": B_ROOM}, ours()))
        assert out.ok
        assert out.output["found"] is False
        assert not reached(out, "Sea View Double")

    def test_a_rate_plan_through_the_quote(self, db):
        out = run(registry.execute("get_quote", {
            "room_type_id": B_ROOM, "rate_plan_id": B_PLAN,
            "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
            "units": 1, "adults": 2}, ours()))
        assert not out.ok, "Hotel A quoted a guest on Hotel B's inventory"

    def test_a_rate_plan_through_meals(self, db):
        """
        Answers about *this* hotel's meals, ignoring the foreign plan entirely.
        The id is a filter the property scope has already narrowed past.
        """
        out = run(registry.execute("get_meal_info",
                                   {"rate_plan_id": B_PLAN}, ours()))
        assert out.ok
        assert not reached(out, B_PLAN, "Sea View Double")

    def test_a_rate_plan_through_negotiation(self, db):
        out = run(registry.execute("negotiate_rate", {
            "rate_plan_id": B_PLAN, "requested_nightly_rupees": 1000}, ours()))
        assert not out.ok, "Hotel A negotiated a discount on Hotel B's rate plan"

    def test_a_task(self, db):
        out = run(registry.execute("get_task", {"task_id": B_TASK}, ours()))
        assert not out.ok, "another hotel's maintenance ticket was readable"
        assert "not found in this property" in (out.refusal_reason or "")
        assert not reached(out, "Fix the tap in 301", "#1801")

    def test_a_hold(self, db):
        out = run(registry.execute("release_hold", {"hold_id": B_HOLD}, ours()))
        assert not out.ok, "Hotel A released Hotel B's held inventory"
        assert db.rows("inventory_holds")[0]["status"] == "active"


class TestAForeignIdIsIgnoredRatherThanHonoured:
    """
    Several reads are about the *hotel*, not about the id they are handed —
    group bands, cancellation tiers, payment terms. A foreign id must not steer
    them; the property scope has already decided whose answer this is.
    """

    @pytest.mark.parametrize("tool,args", [
        ("get_group_rates", {"units": 3, "room_type_id": B_ROOM}),
        ("get_cancellation_terms", {"rate_plan_id": B_PLAN}),
        ("get_payment_terms", {"rate_plan_id": B_PLAN}),
    ])
    def test_it_answers_for_our_hotel(self, db, tool, args):
        out = run(registry.execute(tool, args, ours()))
        assert out.ok, out.refusal_reason
        assert not reached(out, "Sea View Double", B_ROOM, B_PLAN)


class TestTheConversationIsTheSubject:
    """
    The strongest form of the rule, and the one that needs no id at all.
    `get_booking_status` takes no arguments: its subject is the thread, which
    the model cannot name.
    """

    def test_a_thread_with_no_booking_sees_no_booking(self, db):
        out = run(registry.execute("get_booking_status", {}, ours()))
        assert not reached(out, "TB-SECRET", "Somebody Else")

    def test_and_the_same_call_on_their_thread_sees_theirs(self, db):
        """
        The control. If this returned nothing either, the tests above would
        pass on a build where the tool was simply broken.
        """
        theirs = ours()
        theirs.property_id = B
        theirs.conversation_id = B_CONVERSATION
        theirs.principal = AgentPrincipal(
            agent_id="agent-b", agent_type="guest", partner_id=B_PARTNER,
            property_id=B, display_name="Else", autonomy_level=3,
            scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}))
        out = run(registry.execute("get_booking_status", {}, theirs))
        assert out.ok and reached(out, "TB-SECRET"), (
            "the control failed — these tests prove nothing")


class TestOurOwnIdsStillWork:
    """
    Isolation that also blocks the hotel's own data is not isolation, it is an
    outage. Every refusal above has to be about ownership.
    """

    def test_our_own_room_reads(self, db):
        out = run(registry.execute("get_room_details",
                                   {"room_type_id": A_ROOM}, ours()))
        assert out.ok, out.refusal_reason

    def test_our_own_rate_plan_quotes(self, db):
        out = run(registry.execute("get_quote", {
            "room_type_id": A_ROOM, "rate_plan_id": A_PLAN,
            "check_in": str(CHECK_IN), "check_out": str(CHECK_OUT),
            "units": 1, "adults": 2}, ours()))
        assert out.ok, out.refusal_reason
