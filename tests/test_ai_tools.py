"""
Tool authorization — the gates, one at a time.

Every assertion here is about something that happens *before* a tool body runs.
That is the design: `registry.execute()` resolves the tool, checks the grant,
checks the scope, validates the arguments and decides autonomy, and only then
does any hotel data get touched. So a refusal in these tests is provable by the
fact that no database exists in them at all — a tool that reached its body would
raise on `get_db()`, and several of these would fail loudly rather than quietly
passing.

The section that matters most is the last one. A guest can type anything,
including a reservation reference belonging to somebody else, and the model will
faithfully pass it to a tool. `context.require_reservation` is what makes that a
refusal, and it is a set membership test in Python — there is no phrasing that
gets past it.
"""
import asyncio

import pytest

from app.services.ai import autonomy, registry
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.errors import OutOfScope

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

ALL_TOOLS = tuple(registry.REGISTRY)


def run(coro):
    # No pytest-asyncio in this suite; `asyncio.run` per test is the idiom the
    # rest of tests/ already uses.
    return asyncio.run(coro)


def make_ctx(
    *,
    autonomy_level: int = 2,
    scopes: tuple[str, ...] = ("tasks", "messaging"),
    tools: tuple[str, ...] = ALL_TOOLS,
    grants: dict | None = None,
    actor: str = "guest",
    reservation_id: str | None = None,
    guest_id: str | None = None,
    conversation_id: str | None = "conv-1",
) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1",
        agent_type="guest",
        partner_id=PARTNER,
        property_id=PROPERTY,
        display_name="Mandala",
        autonomy_level=autonomy_level,
        scopes=frozenset(scopes),
    )
    return AgentContext(
        principal=principal,
        property_id=PROPERTY,
        partner_id=PARTNER,
        model="test",
        conversation_id=conversation_id,
        reservation_id=reservation_id,
        guest_id=guest_id,
        actor=actor,
        tool_grants=grants or {t: {"tool_name": t, "enabled": True} for t in tools},
    )


def call(tool_name: str, args: dict, ctx: AgentContext):
    return run(registry.execute(tool_name, args, ctx))


TASK_ARGS = {"title": "AC not cooling in 204", "category": "maintenance"}
QUOTE_ARGS = {
    "room_type_id": "rt", "rate_plan_id": "rp",
    "check_in": "2026-09-01", "check_out": "2026-09-03",
}


# ── every tool declares itself completely ────────────────────────────────────

def test_every_tool_declares_a_usage_type():
    # Nothing ships unmetered. A tool without a usage type is one somebody has
    # to retro-fit against a rate card that already exists, and the retro-fit is
    # where the billing gap comes from.
    for name, spec in registry.REGISTRY.items():
        assert spec.usage_type, f"{name} has no usage type"
        assert spec.usage_type.startswith("ai."), f"{name} uses {spec.usage_type}"


def test_every_mutating_tool_needs_at_least_level_two():
    # Level 0 is read-only and level 1 proposes. Anything that changes the hotel
    # must sit above both, or the ladder means nothing.
    for name, spec in registry.REGISTRY.items():
        if spec.mutating:
            assert spec.autonomy_floor >= 2, f"{name} mutates at floor {spec.autonomy_floor}"


def test_every_tool_schema_is_serialisable():
    for name, spec in registry.REGISTRY.items():
        schema = spec.json_schema()
        assert schema["type"] == "object", name
        assert "$defs" not in schema, name


def test_guest_scoped_reads_take_no_subject_argument():
    # The anti-injection rule made checkable: a tool returning the guest's own
    # data must not accept an id, because an id is a parameter a guest can talk
    # the model into filling with somebody else's.
    for name in ("get_guest", "list_guest_reservations", "list_guest_tasks"):
        fields = registry.REGISTRY[name].input_model.model_fields
        assert not fields, f"{name} accepts {list(fields)}"


# ── gate 1: does the tool exist ──────────────────────────────────────────────

def test_an_invented_tool_is_refused_not_raised():
    out = call("cancel_everything", {}, make_ctx())
    assert out.status == "refused"
    assert "cancel_everything" in out.refusal_reason
    # The model is told, so it can say something useful instead of retrying.
    assert out.model_message


# ── gate 2: does this agent have a grant ─────────────────────────────────────

def test_a_tool_with_no_grant_is_denied():
    out = call("create_task", TASK_ARGS, make_ctx(tools=("get_property",)))
    assert out.status == "refused"
    assert "no grant" in out.refusal_reason


def test_a_disabled_grant_is_denied():
    ctx = make_ctx(grants={"create_task": {"tool_name": "create_task", "enabled": False}})
    out = call("create_task", TASK_ARGS, ctx)
    assert out.status == "refused"
    assert "disabled" in out.refusal_reason


def test_a_tool_without_a_grant_is_never_offered_to_the_model():
    ctx = make_ctx(tools=("get_property", "get_property_knowledge"))
    offered = [t["function"]["name"] for t in registry.describe_for_provider(ctx)]
    assert offered == ["get_property", "get_property_knowledge"]
    assert "create_task" not in offered


# ── gate 3: does it hold the scope ───────────────────────────────────────────

def test_a_missing_permission_is_denied():
    out = call("create_task", TASK_ARGS, make_ctx(scopes=("messaging",)))
    assert out.status == "refused"
    assert "'tasks' permission" in out.refusal_reason


def test_pricing_needs_the_rates_scope_and_says_so():
    out = call("get_quote", QUOTE_ARGS, make_ctx(scopes=("tasks", "messaging")))
    assert out.status == "refused"
    assert "'rates' permission" in out.refusal_reason


def test_an_agent_principal_has_no_owner_shortcut():
    # A human owner is trusted with everything by virtue of owning the place.
    # An agent is trusted with exactly what somebody wrote down.
    principal = AgentPrincipal(
        agent_id="a", agent_type="guest", partner_id=PARTNER, property_id=PROPERTY,
        display_name="Mandala", autonomy_level=4, scopes=frozenset({"tasks"}),
    )
    assert principal.can("tasks")
    assert not principal.can("rates")
    assert not principal.can("billing")


# ── gate 4: arguments ────────────────────────────────────────────────────────

def test_bad_arguments_are_refused_with_a_reason_the_model_can_use():
    out = call("create_task", {"title": "x"}, make_ctx())
    assert out.status == "refused"
    assert "title" in out.refusal_reason or "category" in out.refusal_reason


def test_an_unknown_category_is_refused_by_the_tool():
    out = call("create_task", {"title": "Please fix the roof", "category": "roofing"}, make_ctx())
    assert out.status == "refused"
    assert "category" in out.refusal_reason


# ── gate 5: autonomy ─────────────────────────────────────────────────────────

def test_a_level_two_agent_may_raise_a_ticket_but_not_quote():
    ctx = make_ctx(autonomy_level=2, scopes=("tasks", "messaging", "rates"))
    assert autonomy.decide(registry.REGISTRY["create_task"], ctx) == autonomy.EXECUTE
    assert autonomy.decide(registry.REGISTRY["get_quote"], ctx) == autonomy.APPROVE


def test_an_action_above_autonomy_becomes_an_approval_not_a_silent_drop():
    out = call("get_quote", QUOTE_ARGS, make_ctx(autonomy_level=2, scopes=("rates",)))
    # Recorded as pending a human, and the guest is told a person is looking —
    # which is true, rather than "done" (a lie) or nothing (a hang).
    assert out.status == "awaiting_approval"
    assert "autonomy 3" in out.refusal_reason
    assert "hotel team" in out.guest_message


def test_a_level_zero_agent_refuses_rather_than_queues():
    # An Owner Copilot is read-only. A queue of pending actions from it is noise
    # nobody asked for.
    ctx = make_ctx(autonomy_level=0, scopes=("tasks",))
    assert autonomy.decide(registry.REGISTRY["create_task"], ctx) == autonomy.REFUSE
    assert autonomy.decide(registry.REGISTRY["get_property"], ctx) == autonomy.EXECUTE


def test_an_autonomy_override_can_only_raise_the_bar():
    spec = registry.REGISTRY["create_task"]      # floor 2
    assert autonomy.effective_floor(spec, {"autonomy_override": 4}) == 4
    # A property asking for a lower floor is asking for the tool to be less
    # careful than it was written to be. Ignored.
    assert autonomy.effective_floor(spec, {"autonomy_override": 0}) == 2


def test_requires_approval_wins_even_at_sufficient_autonomy():
    ctx = make_ctx(
        autonomy_level=4,
        scopes=("tasks",),
        grants={"create_task": {"tool_name": "create_task", "enabled": True,
                                "requires_approval": True}},
    )
    assert call("create_task", TASK_ARGS, ctx).status == "awaiting_approval"


def test_booking_always_requires_approval():
    # Not a configuration a property can switch off. The first hotel to let an
    # AI take bookings unattended will not be the first hotel to try it.
    ctx = make_ctx(autonomy_level=4, scopes=("frontdesk",))
    assert autonomy.decide(registry.REGISTRY["create_booking"], ctx) == autonomy.APPROVE


# ── the injection boundary ───────────────────────────────────────────────────

def test_a_guest_cannot_read_a_reservation_outside_their_conversation():
    ctx = make_ctx(reservation_id="res-mine")
    with pytest.raises(OutOfScope):
        run(ctx.require_reservation("res-someone-else"))


def test_the_conversations_own_reservation_is_allowed():
    ctx = make_ctx(reservation_id="res-mine")
    assert run(ctx.require_reservation("res-mine")) == "res-mine"


def test_get_reservation_refuses_a_stranger_id_before_touching_the_database():
    # If the scope check did not run first this would reach get_db() and raise a
    # connection error rather than a refusal, so asserting on `status` is also
    # asserting on the ordering.
    ctx = make_ctx(reservation_id="res-mine")
    out = call("get_reservation", {"reservation_id": "res-theirs"}, ctx)
    assert out.status == "refused"
    assert "does not belong to this conversation" in out.refusal_reason
    # And the guest is told something that does not confirm the id exists.
    assert "res-theirs" not in (out.guest_message or "")


def test_a_conversation_with_no_booking_can_read_nothing():
    ctx = make_ctx(reservation_id=None, guest_id=None)
    assert run(ctx.allowed_reservations()) == set()
    with pytest.raises(OutOfScope):
        run(ctx.require_reservation("res-anything"))


def test_a_staff_agent_is_scoped_by_property_not_by_conversation():
    # A front desk agent asking about today's arrivals is scoped by the
    # property, which membership already established. Applying the conversation
    # rule here would break the staff agent for no gain.
    ctx = make_ctx(actor="member", reservation_id=None)
    assert run(ctx.require_reservation("res-anything")) == "res-anything"


# ── the five agent definitions ───────────────────────────────────────────────

def test_every_agent_definition_names_only_registered_tools():
    from app.services.ai.agents import all_definitions

    for agent_type, definition in all_definitions().items():
        unknown = [t for t in definition.tools if t not in registry.REGISTRY]
        assert not unknown, f"{agent_type} names tools that do not exist: {unknown}"


def test_only_the_agents_with_a_runtime_behind_them_are_available():
    """
    Availability is a claim that something has been built to supervise the
    agent, not that its definition exists. The Owner Copilot has a shape and no
    slice; `provisioning.provision` refuses it.

    This list grows one entry per phase, deliberately by hand — an agent that
    became available because someone flipped a default is an agent nobody
    decided to ship.
    """
    from app.services.ai.agents import all_definitions

    available = sorted(t for t, d in all_definitions().items() if d.available)
    assert available == ["frontdesk", "guest", "marketing", "sales"]


def test_an_agent_a_stranger_can_reach_is_guest_scoped():
    """
    The rule is *who is on the other end*, not what the agent does.

    This test asserted `actor_for("sales") == "member"` and was wrong in the
    dangerous direction — it encoded the same assumption that made
    `GUEST_FACING` a set containing only "guest". The Sales agent answers
    strangers on WhatsApp and Instagram; scoping its runs by property rather
    than by conversation would have let somebody who has never stayed at the
    hotel ask `get_task_status` about any ticket in the building.

    Caught by `scripts/sales_e2e.py`, which asserts on the routing *tuple* —
    agent type and actor together — rather than on the agent type alone.
    """
    from app.services.ai.agents import actor_for

    # Reachable by anyone with the hotel's number.
    assert actor_for("guest") == "guest"
    assert actor_for("sales") == "guest"

    # Reachable only by somebody `require_member` authenticated. Marketing is
    # here and not above: Phase D put it on the guest-facing list on a guess
    # about what a marketing agent talks to, and Phase E built it — it answers
    # staff on the `internal` console and plans campaigns. What reaches a guest
    # is a campaign, which is not a conversation, so nobody outside the building
    # can reach this agent at all.
    assert actor_for("frontdesk") == "member"
    assert actor_for("marketing") == "member"
    assert actor_for("owner") == "member"

    # No agent type at all falls back to the tighter of the two: a run nobody
    # classified should be visibly over-restricted, not quietly leaking.
    assert actor_for(None) == "guest"


def test_the_front_desk_agent_cannot_reach_a_guest_or_move_money_outside_a_folio():
    from app.services.ai.agents import get

    fd = get("frontdesk")
    # It holds `folio`, deliberately and with the reasoning written down in the
    # definition — but not the scopes behind refunds, payouts, credit grants,
    # wallet administration or rate changes.
    assert "folio" in fd.permissions
    for scope in ("billing", "payouts", "members", "agents", "reports", "rates"):
        assert scope not in fd.permissions, scope

    # No way to reach a guest: this agent answers on `internal`, which has no
    # provider at all.
    assert fd.channels == ("internal",)
    for tool in ("send_message", "send_template"):
        assert tool not in fd.tools, tool

    # And nothing that sells.
    for tool in ("get_quote", "create_booking", "release_hold"):
        assert tool not in fd.tools, tool


def test_the_front_desk_agents_material_actions_sit_above_its_shipping_autonomy():
    """
    The three that change a guest's stay or their bill must come back as
    approval requests at the level the agent ships at. If a floor ever drops to
    2, a hotel that changed nothing starts checking guests in unattended.
    """
    from app.services.ai.agents import get

    fd = get("frontdesk")
    assert fd.autonomy_level == 2
    for tool in ("check_in", "check_out", "post_folio_charge"):
        assert registry.REGISTRY[tool].autonomy_floor > fd.autonomy_level, tool

    # And the money one cannot be freed by raising the dial.
    assert registry.REGISTRY["post_folio_charge"].always_requires_approval
    # It takes no `kind`, so there is no argument to talk into a refund.
    assert "kind" not in registry.REGISTRY["post_folio_charge"].input_model.model_fields


def test_the_owner_copilot_cannot_change_anything():
    # Level 0 plus 'reports' alone. Both would have to be edited for it to
    # mutate, and every mutating tool sits at floor 2 or above.
    from app.services.ai.agents import get

    owner = get("owner")
    assert owner.autonomy_level == 0
    assert owner.permissions == ("reports",)
    for name in owner.tools:
        assert not registry.REGISTRY[name].mutating, name


def test_the_guest_agent_cannot_move_money_on_an_existing_stay():
    # It holds `rates` and `frontdesk`, because quoting and booking are what it
    # is for and autonomy is what holds it back. It does not hold the scopes
    # that touch a folio, a bill or a payout — arguing an AI into a refund is
    # the attack, and a scope it does not have cannot be argued for.
    from app.services.ai.agents import get

    guest = get("guest")
    assert "rates" in guest.permissions
    for forbidden in ("folio", "billing", "payouts", "members", "agents"):
        assert forbidden not in guest.permissions


def test_every_selling_tool_the_guest_agent_holds_is_gated_by_autonomy():
    # The dial a hotel turns is autonomy, and it has to be the only one. A tool
    # granted without its scope would look enabled and refuse; a scope granted
    # without the autonomy would look dangerous and be inert.
    from app.services.ai.agents import get

    guest = get("guest")
    for name in ("get_quote", "create_booking"):
        spec = registry.REGISTRY[name]
        assert spec.permission in guest.permissions, name
        assert spec.autonomy_floor > guest.autonomy_level, name
