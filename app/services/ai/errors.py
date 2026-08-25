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

    def __init__(self, reason: str, guest_message: str | None = None,
                 model_message: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.guest_message = guest_message or (
            "Let me put you through to the hotel team on that one."
        )
        # **Three messages, not two, and the third is the one that leaked.**
        #
        # `reason` is written for the audit trail and is usually also the right
        # thing to hand the model: "reservation X does not belong to this
        # conversation" is a fact the agent can act on and would never repeat
        # verbatim to a guest.
        #
        # It stopped being the right thing at exactly one place. When a tool
        # raises something nobody anticipated, `registry.execute` builds the
        # reason out of the Python exception — `get_quote failed: APIError:
        # JSON object requested, multiple (or no) rows returned` — and hands it
        # straight to the model, which paraphrases it at a guest. That is how a
        # real guest was told *"quote generate karne mein takleef ho rahi hai"*.
        #
        # So the two are allowed to differ. The row keeps the exception; the
        # model is told what kind of thing went wrong and what to do about it,
        # which is all it can act on anyway.
        self.model_message = model_message or reason


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

    def __init__(self, reason: str, guest_message: str | None = None,
                 model_message: str | None = None):
        super().__init__(
            reason,
            guest_message or "I've asked the hotel team to confirm that — one moment.",
            model_message,
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

    def __init__(self, reason: str, guest_message: str | None = None,
                 model_message: str | None = None):
        super().__init__(
            reason,
            guest_message or "I can't reach our system right now — connecting you to the team.",
            model_message,
        )


class MutationUnverified(AgentError):
    """
    The tool said it changed something and the re-read does not agree.

    **The failure this class exists to make impossible is the agent telling a
    guest that something happened because it asked for it to happen.** A tool
    returns a payload the model reads as success; if the write silently did not
    land — a race, a constraint, a service that answered 200 and did nothing —
    nothing downstream could tell, and the model would report a check-in that
    did not occur to the person standing at the desk.

    So a mutating tool that declares a `verify` re-reads the record it claims to
    have changed, and this is what a disagreement becomes. Status `failed`, not
    `refused`: nothing was denied, the system is in a state nobody intended, and
    the honest end of that is a person looking at it.

    The model is told the **observed** state rather than the state it wanted,
    because that is the only thing it can say to the guest that is true.
    """

    action_status = "failed"

    def __init__(self, reason: str, guest_message: str | None = None,
                 model_message: str | None = None):
        super().__init__(
            reason,
            guest_message or (
                "I couldn't confirm that went through — let me get someone from "
                "the team to check."
            ),
            model_message or (
                "This did NOT complete. Do not tell the guest it is done, do not "
                "apologise for a system error and do not try again — report what "
                "is actually true and call request_human_handoff."
            ),
        )


class ProviderError(AgentError):
    """
    The model itself is unreachable or refused.

    Not recoverable inside the turn: there is nothing left to reason with, so
    the conversation goes to the human inbox.
    """

    # The upstream exception type, when the provider knew it. Set by the
    # provider at the point it swallows the original, so the runtime can
    # record what actually failed without parsing its own message.
    error_class: str | None = None
    # What the transport went through before giving up: how many times it
    # retried, and the status it ended on. A 429 that exhausted every region is
    # a quota problem, not a model problem.
    provider_retries: int = 0
    provider_status: str = "error"

    recoverable = False
    action_status = "failed"


class AgentNotConfigured(AgentError):
    """No active agent, or no published configuration, for this property."""

    recoverable = False
