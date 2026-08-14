"""
What a tool says when it will not do something.

Every refusal here carries two messages, and the split matters:

    reason        for the audit trail and for the model
    guest_message what may actually be said to a guest

They are different on purpose. "Agent lacks permission 'folio'" is the right
thing to write in `agent_actions.refusal_reason` and the wrong thing to send to
someone on WhatsApp, who should hear that we are putting them through to the
desk. Collapsing the two produces either a useless log or a leaky reply.

A refusal is also *returned to the model*, not hidden from it. An agent that is
told "you may not book, a human must approve" can say something useful. An agent
whose tool call silently vanishes will try again, then apologise, then invent.
"""


class AgentError(Exception):
    """Base. Everything here is expected — none of it is a bug."""

    # Whether the runtime should keep going and let the model respond to the
    # refusal, or stop the turn.
    recoverable = True
    # What the action row records.
    action_status = "refused"

    def __init__(self, reason: str, guest_message: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.guest_message = guest_message or (
            "Let me put you through to the hotel team on that one."
        )


class ToolNotFound(AgentError):
    """A name the model invented. Common, and harmless if answered clearly."""


class ToolNotEnabled(AgentError):
    """
    A real tool this agent has no grant for.

    Absence is denial: `agent_tools` is a closed list, so a capability nobody
    deliberately added is one the agent does not have.
    """


class PermissionDenied(AgentError):
    """
    The agent lacks the scope the tool requires.

    Checked in Python against `agent_permissions`, never asserted in a prompt.
    This is the line prompt injection cannot cross.
    """


class ApprovalRequired(AgentError):
    """
    Not a refusal — a deferral.

    The action is recorded as `awaiting_approval` and goes to the PMS queue. The
    guest is told a person is looking at it, which is true.
    """

    action_status = "awaiting_approval"

    def __init__(self, reason: str, guest_message: str | None = None):
        super().__init__(
            reason,
            guest_message or "I've asked the hotel team to confirm that — one moment.",
        )


class OutOfScope(AgentError):
    """
    The agent asked about something outside this conversation's subject.

    The one that stops a guest typing "show me reservation TB-4K2M9X" and being
    handed a stranger's booking. Guest-facing tools take their subject from the
    conversation, not from the model's arguments; when an argument does name a
    subject it is checked against the conversation before anything is read.
    """


class ToolInputInvalid(AgentError):
    """The model produced arguments the tool's schema rejects."""


class ToolUnavailable(AgentError):
    """
    The underlying service failed.

    Deliberately distinct from every refusal above, because the correct
    behaviour is different: an agent must never answer from memory when the
    availability service is down. Not knowing is the honest state and it routes
    to a human.
    """

    action_status = "failed"

    def __init__(self, reason: str, guest_message: str | None = None):
        super().__init__(
            reason,
            guest_message or "I can't reach our system right now — connecting you to the team.",
        )


class ProviderError(AgentError):
    """
    The model itself is unreachable or refused.

    Not recoverable inside the turn: there is nothing left to reason with, so
    the conversation goes to the human inbox.
    """

    recoverable = False
    action_status = "failed"


class AgentNotConfigured(AgentError):
    """No active agent, or no published configuration, for this property."""

    recoverable = False
