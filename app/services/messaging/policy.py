"""
What a channel permits — asked, not assumed.

`service.send()` used to know three WhatsApp facts: that a closed window can be
reopened with an approved template, that the window is 24 hours, and that
connections live in `whatsapp_connections`. None of those is true of Instagram,
and none of them is a property of *messaging* — they are properties of a
channel. So they move here, and the shared path asks.

    service.send(conversation)
        └─ policy.for_channel(conversation["channel"])
               ├─ window_hours(conversation)   how long may we reply freely?
               ├─ supports_templates            is there a way to reopen it?
               └─ connections_table             where does the account live?

The alternative — `if channel == "instagram"` inside the send path — is the
thing this file exists to prevent. It reads fine with two channels and becomes
unmaintainable at four, and it puts channel rules in the one function that must
stay identical for every channel.

**Two genuinely different rules, encoded rather than commented:**

*Templates.* WhatsApp reopens a closed window with a template Meta approved in
advance. Instagram has no such mechanism. A caller asking for a template on
Instagram gets a refusal naming the reason, not a silent fallback to free text —
which would be a message sent outside a window Meta will reject anyway.

*The human-agent window.* Meta gives a **person** answering an Instagram
conversation seven days, against twenty-four hours for an automated reply. That
is not a quirk to paper over: it is exactly the difference between a bot and a
human agent, and this codebase already tracks which one is driving in
`conversations.handler`. So the Instagram window is a function of the
conversation, not a constant. WhatsApp's is a constant, and stays one.
"""
from dataclasses import dataclass, field
from typing import Callable

from app.config import settings

WHATSAPP = "whatsapp"
INSTAGRAM = "instagram"


@dataclass(frozen=True)
class ChannelPolicy:
    channel: str

    # Where the account→property mapping lives, and the SECURITY DEFINER
    # function that reads it. Never a table read from the service: the resolver
    # is the only trusted route from an inbound payload to a hotel.
    connections_table: str
    resolver_rpc: str
    resolver_arg: str
    # The column on the connection row holding the provider's id for our end.
    account_column: str

    # Whether a closed window can be reopened at all.
    supports_templates: bool

    # What the hotel is charged for outbound. WhatsApp bills per *conversation*
    # (see usage.record_conversation); Instagram has no conversation billing
    # from Meta at all, so it is metered per message and the conversation-level
    # charge is opened by us on the first inbound rather than by a Meta
    # callback that never arrives.
    bills_per_conversation: bool
    conversation_usage_type: str | None = None

    # Given a conversation, how many hours of free-form reply are permitted.
    # A callable rather than an int because Instagram's answer depends on who
    # is answering.
    window_hours: Callable[[dict], int] = field(default=lambda _c: 24)

    def human_is_handling(self, conversation: dict) -> bool:
        return (conversation.get("handler") or "ai") in ("human", "human_requested")


def _whatsapp_window(_conversation: dict) -> int:
    # Meta's rule, and a constant: 24 hours from the guest's last message,
    # whoever is replying. A human on WhatsApp gets no more room than the agent.
    return settings.whatsapp_service_window_hours


def _instagram_window(conversation: dict) -> int:
    """
    24 hours for an automated reply; 7 days once a person is handling it.

    This is Meta's human-agent allowance, and claiming it while an agent is
    still driving the conversation would be a policy violation dressed up as a
    longer window. `handler` is the server's own record of who took over — set
    by `take_over()` and `request_human()`, never by the model — which is why
    it is safe to key on.
    """
    return (settings.instagram_human_agent_window_hours
            if (conversation.get("handler") or "ai") in ("human", "human_requested")
            else settings.instagram_service_window_hours)


_POLICIES = {
    WHATSAPP: ChannelPolicy(
        channel=WHATSAPP,
        connections_table="whatsapp_connections",
        resolver_rpc="resolve_whatsapp_property",
        resolver_arg="p_phone_number_id",
        account_column="phone_number_id",
        supports_templates=True,
        bills_per_conversation=True,
        window_hours=_whatsapp_window,
    ),
    INSTAGRAM: ChannelPolicy(
        channel=INSTAGRAM,
        connections_table="instagram_connections",
        resolver_rpc="resolve_instagram_property",
        resolver_arg="p_account_id",
        account_column="instagram_account_id",
        supports_templates=False,
        # Meta does not bill Instagram per conversation, so nothing external
        # ever tells us a window opened. We open ours on the first inbound
        # message, which is also the only honest moment: it is our pricing
        # decision, not a pass-through of someone else's.
        bills_per_conversation=False,
        conversation_usage_type="instagram.conversation",
        window_hours=_instagram_window,
    ),
}


def for_channel(channel: str | None) -> ChannelPolicy:
    """
    The policy for a channel.

    Unknown channels raise rather than defaulting to WhatsApp. A channel we have
    not thought about must not silently inherit another channel's window rule
    and template behaviour — that is how a message goes out under rules Meta
    never agreed to.
    """
    policy = _POLICIES.get(channel or WHATSAPP)
    if policy is None:
        raise ValueError(f"no messaging policy for channel {channel!r}")
    return policy


def channels() -> tuple[str, ...]:
    return tuple(_POLICIES)
