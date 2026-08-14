"""
What a channel is.

One protocol, and the two things every provider has to be able to do: send free
text, and send an approved template. Everything else a provider offers —
reactions, read receipts, interactive buttons — is an extension, not part of
what the messaging service depends on, because the service must work identically
for a channel that has none of them.

`InboundMessage` and `SendResult` are the boundary. Above them nothing knows
what Meta calls a message id; below them nothing knows what a conversation is.
Instagram becomes a class in this package that produces the same two shapes.

**No adapter reads a credential from a database row.** Adapters are constructed
with resolved credentials by `credentials.for_connection()`, so a connection
whose secret is missing fails at construction — loudly, at the boundary — rather
than three layers down with a 401 from Meta that looks like a network problem.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class MessagingError(Exception):
    """
    A send that will not happen, with a machine-readable reason.

    The `code` is what the agent sees. It has to be a code rather than prose
    because the agent's next move differs — `MESSAGE_WINDOW_CLOSED` means pick a
    template, `PROVIDER_UNAVAILABLE` means try again or hand off — and a model
    branching on the wording of an error message is a model that will branch
    wrongly the first time someone rephrases it.
    """

    def __init__(self, code: str, detail: str, *, retryable: bool = False):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable


# The codes. Named here so the agent, the API and the tests agree on them.
WINDOW_CLOSED = "MESSAGE_WINDOW_CLOSED"
TEMPLATE_NOT_APPROVED = "TEMPLATE_NOT_APPROVED"
TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
NO_CONNECTION = "CHANNEL_NOT_CONNECTED"
PROVIDER_FAILED = "PROVIDER_FAILED"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
NOT_CONFIGURED = "CHANNEL_NOT_CONFIGURED"
# Only ever raised for *proactive* outbound — a nudge, a follow-up, a campaign.
# A reply to a guest who is waiting, and any transactional message, draws on the
# wallet's overdraft instead. See services/billing.
INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"


@dataclass(frozen=True)
class InboundMessage:
    """One message from a guest, in channel-neutral terms."""

    channel: str
    # The provider's id for this message. The idempotency key for everything
    # downstream — see service.record_inbound.
    external_id: str
    # The provider's id for the sender: a phone number on WhatsApp, a scoped
    # user id on Instagram. Not a guest id, and never treated as one.
    sender_external_id: str
    # The provider's id for *our* end, which is what resolves to a property.
    account_id: str

    message_type: str = "text"
    body: str | None = None
    media_urls: tuple[str, ...] = ()
    sender_name: str | None = None
    sent_at: datetime | None = None
    # What the provider actually said, kept for debugging a bad delivery three
    # days later. Trimmed of anything not worth storing by the adapter.
    raw: dict = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        """
        Whether the agent can be expected to do anything with this.

        An unsupported type is still persisted and still answered — the guest
        gets told we cannot open it. Silently dropping an inbound event is the
        one behaviour a hotel can never explain to a guest who is standing at
        the desk asking why nobody replied.
        """
        return self.message_type in ("text", "location") or bool(self.body)


@dataclass(frozen=True)
class DeliveryUpdate:
    channel: str
    external_id: str
    status: str            # sent · delivered · read · failed
    error_code: str | None = None
    error_detail: str | None = None
    at: datetime | None = None
    raw: dict = field(default_factory=dict)

    # What the provider will bill this on.
    #
    # Meta prices per *conversation*, not per message, and the category —
    # service, utility, marketing, authentication — decides the rate. It arrives
    # on the status callback and nowhere else, so discarding it means the only
    # record of what we were charged for lives in Meta's invoice.
    #
    # These are now the `dimension` a rate card resolves on — the category picks
    # the usage type, the country picks the rate. See
    # `usage.record_conversation`, which is charged from `apply_delivery_status`
    # and keyed on `conversation_id_external` so the several callbacks inside
    # one window resolve to one charge.
    conversation_id_external: str | None = None
    conversation_category: str | None = None
    pricing_model: str | None = None
    billable: bool | None = None
    recipient_country: str | None = None


@dataclass(frozen=True)
class SendResult:
    external_id: str | None
    status: str = "sent"
    raw: dict = field(default_factory=dict)


class ChannelAdapter(Protocol):
    """What every channel must do. Deliberately two methods."""

    channel: str
    # True when this adapter talks to a fake. Carried on the adapter rather than
    # worked out by the caller, so a sandbox send can never be reported as a
    # real one.
    is_sandbox: bool

    async def send_text(self, *, to: str, body: str, client_reference: str) -> SendResult: ...

    async def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language: str,
        parameters: list[str],
        client_reference: str,
    ) -> SendResult: ...
