"""
"Hi" is the commonest turn in the system, and it paid the full catalogue.

**Measured, not assumed.** Three days of production webhook runs:

    task                  runs   share   tokens in   tools called
    guest_conversation    1406   60.2%       2,328           0.43

`guest_conversation` is in `toolsets.EVERYTHING`, so it narrows nothing and
carries all 39 of the guest agent's schemas — about 7,300 tokens — on every
model step of the most common turn shape there is.

**The reason it narrows nothing is sound, and is not being changed.** It is the
fallback for a message the classifier could not read, and under-equipping the
turns we understand least is exactly backwards. But it is *also* the greeting
task, and those two populations are not alike. `classify.is_opening` is a closed
list of exact strings — "hi, do you have rooms tomorrow?" is not one, and never
was — so a greeting is the one message in this product known, for free and
before any model runs, to be asking for nothing.

**CORE, not nothing.** A greeting still has to be answerable: the hotel's own
name and knowledge, a way to reply where the channel needs a tool for it, and a
way to reach a person. What it does not need is nineteen ways to price a stay
nobody has mentioned. The guest's next message classifies normally.
"""
import json

import pytest

from app.services.ai import agents, classify, registry, toolsets
from app.services.ai.context import AgentContext, AgentPrincipal

registry.load_all()

GRANTED = frozenset(agents.get("guest").tools) | {"send_message"}


def ctx() -> AgentContext:
    principal = AgentPrincipal(
        agent_id="a", agent_type="guest", partner_id="p", property_id="h",
        display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id="h", partner_id="p", conversation_id="c",
        channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in GRANTED},
    )


def tokens(tools) -> int:
    return len(json.dumps(tools)) // 4


class TestAGreetingCarriesTheCoreAndNothingElse:
    def test_it_narrows_where_the_task_alone_does_not(self):
        full = toolsets.for_task("guest_conversation", GRANTED)
        greeting = toolsets.for_task("guest_conversation", GRANTED, opening=True)
        assert full == GRANTED, "guest_conversation still narrows nothing on its own"
        assert greeting < full

    def test_what_is_left_is_exactly_the_core(self):
        assert toolsets.for_task("guest_conversation", GRANTED,
                                 opening=True) == (GRANTED & toolsets.CORE)

    @pytest.mark.parametrize("tool", [
        "send_message",            # some channels reply with a tool, not prose
        "request_human_handoff",   # every turn's honest ending is a person
        "get_property",            # the hotel's own name
        "get_property_knowledge",  # "hi, parking hai?" is still answerable
    ])
    def test_a_greeting_can_still_do_the_things_any_turn_must(self, tool):
        assert tool in toolsets.for_task("guest_conversation", GRANTED, opening=True)

    def test_it_is_still_a_subset_of_what_was_granted(self):
        """
        The invariant that makes this an optimisation and not a second
        permission system. It holds for the opening path as much as any other.
        """
        assert toolsets.for_task("guest_conversation", GRANTED,
                                 opening=True) <= GRANTED

    def test_an_agent_granted_none_of_the_core_is_not_left_with_nothing(self):
        """
        A misconfigured agent must fall through to the ordinary path rather
        than be sent into a turn holding an empty list.
        """
        odd = frozenset({"get_quote", "search_availability"})
        assert toolsets.for_task("guest_conversation", odd, opening=True) == odd

    def test_the_saving_is_the_size_it_was_claimed_to_be(self):
        """
        Numbers in a commit message rot. This one is asserted.
        """
        before = tokens(registry.describe_for_provider(ctx(), task="guest_conversation"))
        after = tokens(registry.describe_for_provider(ctx(), task="guest_conversation",
                                                      opening=True))
        assert after < before * 0.25, f"{before} → {after} is not the win claimed"


class TestOnlyARealGreeting:
    @pytest.mark.parametrize("message", ["hi", "Hello!", "namaste", "good morning"])
    def test_a_bare_greeting_is_an_opening(self, message):
        assert classify.is_opening(message)

    @pytest.mark.parametrize("message", [
        "hi, do you have rooms tomorrow?",
        "hello can I get a discount",
        "hey, 5 rooms chahiye",
        "namaste, human se baat karni hai",
        "no",
        "when?",
    ])
    def test_and_anything_carrying_a_question_is_not(self, message):
        """
        The safety property the whole narrowing rests on. A message with any
        business content in it must reach the full catalogue — `is_opening`
        matches exact strings for precisely this reason.
        """
        assert not classify.is_opening(message)
        assert toolsets.for_task("guest_conversation", GRANTED,
                                 opening=classify.is_opening(message)) == GRANTED


class TestTheFlagComesFromTheClassifier:
    """
    The runtime is told, rather than asking again.

    The condition is `is_opening(message) AND the thread has no history`, and
    only the classifier sees both. A predicate written in two places is a
    predicate that drifts.
    """

    def test_the_opening_shortcut_says_so_and_calls_nothing(self):
        import asyncio

        class Exploding:
            name = "never"

            async def step(self, **_kwargs):  # pragma: no cover - must not run
                raise AssertionError("a greeting must not reach the model")

        decided = asyncio.run(classify.ModelClassifier(Exploding()).classify(
            agent_type="guest", message="hi", history=None,
        ))
        assert decided.opening is True
        assert decided.source == "default"
        assert decided.task == "guest_conversation"

    def test_a_greeting_on_a_thread_with_history_is_not_an_opening(self):
        """
        Once a thread has history a bare "hi" may well be an answer to
        something, and that is the classifier's job. It must not take the
        shortcut — proved by letting the model call fail and reading the
        fallback, rather than by reaching Vertex.
        """
        import asyncio

        class Failing:
            name = "broken"

            async def step(self, **_kwargs):
                raise RuntimeError("no model here")

        decided = asyncio.run(classify.ModelClassifier(Failing()).classify(
            agent_type="guest", message="hi",
            history=[{"role": "assistant", "content": "Kitne log hain?"}],
        ))
        assert decided.opening is False

    def test_a_scripted_run_never_claims_an_opening(self):
        """
        `StaticClassifier` is what the deterministic suites use. It answers
        without looking at the message at all, so it must not assert something
        about one — a scripted turn that narrowed its own catalogue would be
        testing a path production does not take.
        """
        import asyncio

        decided = asyncio.run(classify.StaticClassifier().classify(
            agent_type="guest", message="hi", history=None,
        ))
        assert decided.opening is False
