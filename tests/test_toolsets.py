"""
Showing the model a shorter menu, without changing what it may order.

The guest agent's 23 tool schemas are about 14 KB, roughly seventy per cent of
the base prompt, and every model step in a turn re-sends all of it. Measured on
1,000 real Vertex runs, a turn that calls nothing costs 2,292 tokens and a turn
that calls three tools costs 10,499 — the catalogue, four times over.

The classifier already decides what a message is about, one cheap call before
the run opens. `toolsets` lets that decision pick the menu as well as the model
tier.

Everything here is about the two ways that could go wrong:

  * **It must never widen.** If narrowing could add a tool, it would be a
    second permission system sitting beside `agent_tools` and disagreeing with
    it. Asserted over every task × every agent definition, not by inspection.
  * **It must never strand a turn.** A task shown a set that omits the tool it
    exists for cannot recover — the model does not know the tool is there. So
    every name in the table is checked against the registry (a typo would
    silently remove the tool it meant to include), and the tasks that carry
    money are checked by hand.
"""
import pytest

from app.services.ai import classify, model_router, registry, toolsets
from app.services.ai.agents import all_definitions
from app.services.ai.context import AgentContext, AgentPrincipal

registry.load_all()

ALL_TASKS = tuple(model_router.known_tasks())


def granted_for(definition) -> frozenset[str]:
    return frozenset(t for t in definition.tools if t in registry.REGISTRY)


def ctx_for(definition) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="a", agent_type=definition.agent_type, partner_id="p",
        property_id="x", display_name="A", autonomy_level=3,
        scopes=frozenset(definition.permissions),
    )
    return AgentContext(
        principal=principal, property_id="x", partner_id="p",
        tool_grants={t: {"tool_name": t, "enabled": True}
                     for t in granted_for(definition)},
    )


DEFINITIONS = all_definitions()
DEFINED = [d for d in DEFINITIONS.values() if d.available]


# ── the invariant ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("definition", DEFINED, ids=lambda d: d.agent_type)
def test_narrowing_never_widens(definition):
    granted = granted_for(definition)
    for task in ALL_TASKS:
        shown = toolsets.for_task(task, granted)
        assert shown <= granted, f"{definition.agent_type}/{task} added {shown - granted}"


def test_an_agent_with_no_grants_is_shown_nothing():
    for task in ALL_TASKS:
        assert toolsets.for_task(task, frozenset()) == frozenset()


def test_every_name_in_the_table_is_a_real_tool():
    """
    A typo does not add a phantom tool — it silently *removes* the one it meant
    to include, and the turn that needed it cannot recover because the model was
    never told it exists.
    """
    named = set(toolsets.CORE)
    for tools in toolsets.BY_TASK.values():
        named |= set(tools)
    unknown = named - set(registry.REGISTRY)
    assert not unknown, f"toolsets names tools that do not exist: {sorted(unknown)}"


def test_every_narrowing_task_is_a_task_the_router_knows():
    unknown = set(toolsets.BY_TASK) - set(ALL_TASKS)
    assert not unknown, f"toolsets narrows tasks the router has never heard of: {sorted(unknown)}"


def test_every_guest_facing_capability_is_either_mapped_or_deliberately_not():
    """
    A capability the classifier can return and this table has never heard of
    falls back to the full catalogue — correct, but silent. This is the test
    that makes adding one to `model_router` surface here.
    """
    for agent_type in ("guest", "sales"):
        for task in classify.candidates(agent_type):
            assert task in toolsets.BY_TASK or task in toolsets.EVERYTHING, (
                f"{agent_type} can be routed to {task!r}, which is neither "
                "narrowed nor deliberately left wide"
            )


# ── never strand a turn ──────────────────────────────────────────────────────

def test_the_way_out_is_always_on_the_menu():
    """Every task's honest ending is a person."""
    granted = granted_for(DEFINITIONS["guest"])
    for task in ALL_TASKS:
        assert "request_human_handoff" in toolsets.for_task(task, granted), task


def test_a_guest_can_always_ask_a_side_question():
    """
    Somebody halfway through booking still asks whether there is parking. The
    alternative to answering from `get_property_knowledge` is inventing.
    """
    granted = granted_for(DEFINITIONS["guest"])
    for task in ALL_TASKS:
        assert "get_property_knowledge" in toolsets.for_task(task, granted), task


@pytest.mark.parametrize("task,needed", [
    # The tool each task exists to reach. Written out rather than derived,
    # because deriving it from the same table would prove nothing.
    ("payment_status", "get_booking_status"),
    ("booking", "get_quote"),
    ("booking", "create_booking_payment_link"),
    ("simple_quote", "get_quote"),
    ("availability", "search_availability"),
    ("guest_request", "create_task"),
    ("reservation_lookup", "get_reservation"),
    ("cancellation_dispute", "get_property_policies"),
    ("multi_reservation", "list_guest_reservations"),
])
def test_the_task_can_reach_the_tool_it_is_named_for(task, needed):
    granted = granted_for(DEFINITIONS["guest"])
    if needed not in granted:
        pytest.skip(f"the guest agent does not hold {needed}")
    assert needed in toolsets.for_task(task, granted)


def test_an_availability_question_can_become_a_price_question():
    """
    A guest who hears a room is free asks the price in the same breath. Being
    shown the pricing tool here saves the whole turn that would otherwise be
    spent discovering it exists.
    """
    granted = granted_for(DEFINITIONS["guest"])
    assert "get_quote" in toolsets.for_task("availability", granted)


# ── the deliberate non-narrowings ────────────────────────────────────────────

def test_the_fallback_task_narrows_nothing():
    """
    `guest_conversation` is the greeting *and* what an unreadable message
    becomes. Under-equipping the turns we understand least is exactly backwards.
    """
    granted = granted_for(DEFINITIONS["guest"])
    assert toolsets.for_task("guest_conversation", granted) == granted


def test_an_unknown_task_narrows_nothing():
    granted = granted_for(DEFINITIONS["guest"])
    assert toolsets.for_task("a_task_from_the_future", granted) == granted
    assert toolsets.for_task(None, granted) == granted


def test_a_task_whose_whole_toolset_is_ungranted_falls_back_to_everything():
    """
    A misconfigured agent, not a cheap turn. Three tools that have nothing to do
    with the question is worse than the full catalogue.
    """
    granted = frozenset({"request_human_handoff", "get_property", "get_room_types"})
    assert toolsets.for_task("payment_status", granted) == granted


# ── and it actually reduces the prompt ───────────────────────────────────────

def test_the_catalogue_really_does_get_smaller():
    import json

    ctx = ctx_for(DEFINITIONS["guest"])
    full = len(json.dumps(registry.describe_for_provider(ctx)))
    faq = len(json.dumps(registry.describe_for_provider(ctx, task="guest_faq")))
    # Measured at −81%; asserted at half, so an added tool does not fail the
    # suite for being a tool.
    assert faq < full / 2, f"{faq} vs {full}"


def test_describing_without_a_task_is_what_it_always_was():
    ctx = ctx_for(DEFINITIONS["guest"])
    everything = {t["function"]["name"] for t in registry.describe_for_provider(ctx)}
    assert everything == granted_for(DEFINITIONS["guest"])


# ── the wiring, not just the table ───────────────────────────────────────────

def test_the_runtime_actually_narrows_the_menu(monkeypatch):
    """
    The table can be perfect and the runtime can still describe everything. This
    is the test that fails if `run_turn` stops passing the classifier's answer
    through to `describe_for_provider`.
    """
    import asyncio

    from app.services.ai import classify, runtime
    from app.services.ai.providers import EchoProvider
    from tests.fake_supabase import FakeDb

    fake = FakeDb()
    fake.seed("properties", {"id": "x", "partner_id": "p", "name": "Casa Mandala",
                             "city": "Anjuna"})
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in ("app.services.ai.runtime", "app.services.ai.context",
                   "app.services.ai.prompts", "app.services.pms.clock",
                   "app.services.messaging.service"):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)

    ctx = ctx_for(DEFINITIONS["guest"])
    provider = EchoProvider(["Breakfast is 08:00 to 10:30."])
    asyncio.run(runtime.run_turn(
        ctx, "what time is breakfast?", provider=provider,
        classifier=classify.StaticClassifier("guest_faq"),
    ))

    shown = set(provider.seen_tools[0])
    assert "get_property_knowledge" in shown
    assert "request_human_handoff" in shown
    # The tools a breakfast question has no business being offered.
    assert "get_quote" not in shown
    assert "create_booking_payment_link" not in shown
    assert len(shown) < len(granted_for(DEFINITIONS["guest"]))


def test_an_unclassified_turn_still_sees_everything(monkeypatch):
    import asyncio

    from app.services.ai import classify, runtime
    from app.services.ai.providers import EchoProvider
    from tests.fake_supabase import FakeDb

    fake = FakeDb()
    fake.seed("properties", {"id": "x", "partner_id": "p", "name": "Casa Mandala",
                             "city": "Anjuna"})
    monkeypatch.setattr("app.db.client.get_db", lambda: fake)
    for module in ("app.services.ai.runtime", "app.services.ai.context",
                   "app.services.ai.prompts", "app.services.pms.clock",
                   "app.services.messaging.service"):
        monkeypatch.setattr(f"{module}.get_db", lambda: fake, raising=False)

    ctx = ctx_for(DEFINITIONS["guest"])
    provider = EchoProvider(["Hello!"])
    asyncio.run(runtime.run_turn(
        ctx, "hi", provider=provider,
        classifier=classify.StaticClassifier("guest_conversation"),
    ))
    assert set(provider.seen_tools[0]) == granted_for(DEFINITIONS["guest"])
