"""
A deterministic Instagram, for development and for `instagram_e2e.py`.

The same argument as `whatsapp/fake.py`: a second implementation of
`ChannelAdapter`, selected by `is_sandbox` on the connection row, recording what
it was asked to send so a test can assert on it. Everything above it — the
window rule, the human-agent allowance, idempotency, the agent, the audit trail,
the ledger — runs unchanged, which is the only reason a test against this proves
anything.

**What it does not prove.** That Meta accepts our payload shape, that the app
holds `instagram_manage_messages` on the account, that an IGSID can receive,
that our signature matches theirs. Those need Meta, and the report says
`FAKE INSTAGRAM TESTED` rather than letting the two claims blur.

One deliberate difference from the WhatsApp fake: `send_template` raises here
too. A fake that quietly accepted a template would let a bug — a caller
assuming WhatsApp's reopen-with-a-template behaviour works everywhere — pass
every test and fail the first time it met Meta.
"""
import logging
import uuid
from dataclasses import dataclass, field

from app.services.messaging.channels import (
    PROVIDER_FAILED,
    PROVIDER_UNAVAILABLE,
    TEMPLATE_NOT_FOUND,
    MessagingError,
    SendResult,
)

logger = logging.getLogger(__name__)


@dataclass
class SentIg:
    to: str
    kind: str = "text"           # text · image
    body: str | None = None
    image_url: str | None = None
    client_reference: str = ""
    external_id: str = ""
    tags: list[str] = field(default_factory=list)


class FakeInstagramAdapter:
    channel = "instagram"
    is_sandbox = True

    # Class-level for the same reason the WhatsApp fake's is: a webhook handler
    # builds its own adapter deep inside a request, and a fake whose output only
    # the constructing code can see is a fake nothing can assert on.
    outbox: list[SentIg] = []
    fail_next: MessagingError | None = None
    timeout_next: bool = False

    def __init__(self, *, instagram_account_id: str = "sandbox", **_kw):
        self.instagram_account_id = instagram_account_id

    @classmethod
    def reset(cls) -> None:
        cls.outbox = []
        cls.fail_next = None
        cls.timeout_next = False

    @classmethod
    def last(cls) -> SentIg | None:
        return cls.outbox[-1] if cls.outbox else None

    def _check_failures(self) -> None:
        if type(self).timeout_next:
            type(self).timeout_next = False
            raise MessagingError(
                PROVIDER_UNAVAILABLE, "fake Instagram timed out", retryable=True
            )
        if type(self).fail_next is not None:
            exc = type(self).fail_next
            type(self).fail_next = None
            raise exc

    async def send_text(self, *, to: str, body: str, client_reference: str) -> SendResult:
        self._check_failures()
        sent = SentIg(to=to, body=body, client_reference=client_reference)
        # Instagram ids look like `aWdfZG...`; shaped differently from a wamid on
        # purpose, so nothing downstream can be written to depend on the format
        # of one and quietly work for both.
        sent.external_id = f"ig_mid.FAKE{uuid.uuid4().hex[:18]}"
        type(self).outbox.append(sent)
        logger.debug("fake instagram → %s: %s", to, body)
        return SendResult(external_id=sent.external_id, status="sent",
                          raw={"sandbox": True, "clientReference": client_reference})

    async def send_image(self, *, to: str, image_url: str, caption: str | None,
                         client_reference: str) -> SendResult:
        """
        The real one carries no caption on an attachment, so neither does this
        — a fake that accepted one would let a test pass on a path production
        does not have.
        """
        self._check_failures()
        sent = SentIg(to=to, kind="image", image_url=image_url,
                      client_reference=client_reference)
        sent.external_id = f"ig_mid.FAKE{uuid.uuid4().hex[:18]}"
        type(self).outbox.append(sent)
        logger.debug("fake instagram image → %s: %s", to, image_url)
        return SendResult(external_id=sent.external_id, status="sent",
                          raw={"sandbox": True, "clientReference": client_reference})

    async def send_template(self, *, to: str, template_name: str, language: str,
                            parameters: list[str], client_reference: str) -> SendResult:
        raise MessagingError(
            TEMPLATE_NOT_FOUND,
            "Instagram has no approved-template mechanism, in the fake as in the real one.",
        )


def fail_with(code: str = PROVIDER_FAILED, detail: str = "fake failure",
              *, retryable: bool = False) -> None:
    FakeInstagramAdapter.fail_next = MessagingError(code, detail, retryable=retryable)
