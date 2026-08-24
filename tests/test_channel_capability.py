"""
A tool the channel cannot carry is a tool the model must not be shown.

**Found by probing the deployed backend, not by any test here.** A visitor
asked the website chat on a hotel's own booking page for five rooms. The model
called `get_group_rates`, got its answer, and then reached for `send_message`
to ask which dates — and the tool raised

    ValueError: no messaging policy for channel 'website'

`registry.execute` did exactly the right thing with that: the model was told the
lookup had not come back and to call `request_human_handoff` rather than guess.
So the highest-intent guest on the highest-intent surface a hotel has was told
somebody would get back to them, on a turn where nothing was actually wrong with
the hotel, the rooms or the price.

Only `whatsapp` and `instagram` have an outbound policy. `website`, `internal`,
`phone` and `email` do not — and `send_message` was on the menu for all six,
because the menu had never been filtered by anything except grants and task.

`registry`'s own docstring already stated the rule this violates:

    **The model never sees a tool it cannot call.**

That was true of permissions and false of channels. This file is that rule
applied to the dimension it had missed, in both directions — because the fix
that removes `send_message` everywhere would be worse than the bug.
"""
import pytest

from app.services.ai import registry
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.messaging import policy

registry.load_all()

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def ctx_on(channel: str) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        channel=channel, actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def shown_on(channel: str) -> set[str]:
    return {s.name for s in registry.available_for(ctx_on(channel))}


# ── the bug ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel", sorted(policy.WINDOWLESS))
def test_a_channel_that_cannot_send_is_not_offered_the_sending_tool(channel):
    """
    `website`, `internal`, `phone`, `email`. Every one of them would raise, and
    the model reaching for any of them ends the turn at a handoff.
    """
    assert "send_message" not in shown_on(channel), (
        f"send_message is on the menu for {channel!r}, where messaging.send "
        f"raises — the model calling it turns a working turn into a handoff"
    )


@pytest.mark.parametrize("channel", ["whatsapp", "instagram"])
def test_a_channel_that_can_send_still_gets_it(channel):
    """
    The half that would be a much worse regression. On the DM channels the
    agent replies *with the tool* on some paths, and removing it there would
    silence the product on its two busiest surfaces.
    """
    assert "send_message" in shown_on(channel)


def test_the_two_lists_agree_with_the_policy_module():
    """
    Derived, not restated. A channel that gains a policy tomorrow gains the
    tool without anybody editing this rule, and one that loses it loses the
    tool the same way.
    """
    can_send = set(policy.channels())
    for channel in can_send:
        assert "send_message" in shown_on(channel)
    for channel in policy.WINDOWLESS:
        assert channel not in can_send
        assert "send_message" not in shown_on(channel)


def test_every_channel_the_database_allows_is_decided_one_way_or_the_other():
    """
    The lesson `policy.WINDOWLESS` half-learned, finished.

    Any channel `conversations.channel` can hold must be either sendable or
    knowably not; a channel in neither list would raise inside
    `_usable_on` on a real guest turn, which is the failure this file exists
    for wearing a different hat.
    """
    stored = set(policy.channels()) | set(policy.WINDOWLESS)
    for channel in ("whatsapp", "instagram", "website", "internal", "phone", "email"):
        assert channel in stored, (
            f"{channel!r} can be stored on a conversation and is in neither "
            f"_POLICIES nor WINDOWLESS"
        )
        # And asking is safe rather than explosive.
        shown_on(channel)


# ── everything else is untouched ────────────────────────────────────────────

def test_only_the_sending_tool_is_affected():
    """
    Narrow on purpose. A website guest must still be able to have their rooms
    priced, their group checked and their booking read — the channel decides
    how the agent *speaks*, not what it may know.
    """
    website = shown_on("website")
    whatsapp = shown_on("whatsapp")

    assert whatsapp - website == {"send_message"}, (
        f"the channel filter removed more than the sending tool: "
        f"{sorted(whatsapp - website)}"
    )
    for essential in ("get_group_rates", "negotiate_rate", "get_quote",
                      "get_booking_status", "request_human_handoff",
                      "cancel_booking", "get_cancellation_quote"):
        assert essential in website


def test_a_website_guest_can_still_be_answered():
    """
    The reply does not need the tool. `web_chat.ask` returns `result.reply` in
    the HTTP response and writes it to the thread — which is the designed path
    and the one that was already working on every turn where the model did not
    reach for `send_message`.
    """
    assert "request_human_handoff" in shown_on("website")
    assert "get_property_knowledge" in shown_on("website")


def test_the_filter_cannot_add_anything():
    """
    Same invariant `toolsets` holds: narrowing may only ever narrow. A filter
    that could widen would be a second permission system disagreeing with the
    first.
    """
    for channel in ("whatsapp", "instagram", "website", "internal", "phone", "email"):
        assert shown_on(channel) <= set(registry.REGISTRY)


def test_an_unknown_channel_is_treated_as_unable_to_send():
    """
    Conservative in the direction that cannot break a turn. A channel nobody
    has thought about does not get a tool that would raise on it; the agent
    answers in prose, which every channel can carry.
    """
    assert "send_message" not in shown_on("carrier-pigeon")
