"""
A deterministic WhatsApp, for development and for the E2E suite.

It is a second implementation of `ChannelAdapter`, not a patched-over first one.
Everything above it — the window rule, template approval, idempotency, the
agent, the audit trail — runs unchanged, which is the only reason a test against
this proves anything at all.

**What it does not prove.** That Meta accepts our payload shape, that a template
is approved, that a number can receive, that the signature we compute matches
the one Meta sends. Those need Meta. This file exists so the other ninety
percent can be tested today, and the report says `FAKE META TESTED` rather than
letting the two blur.

It can also be told to fail — `fail_next`, `timeout_next` — because "the
provider timed out and we retried" is a path that has to be exercised and cannot
be exercised against something that always works.
"""
import logging
import uuid
from dataclasses import dataclass, field

from app.services.messaging.channels import (
    PROVIDER_FAILED,
    PROVIDER_UNAVAILABLE,
    MessagingError,
    SendResult,
)

logger = logging.getLogger(__name__)


@dataclass
class Sent:
    to: str
    kind: str                 # text · template · image
    body: str | None = None
    image_url: str | None = None
    template_name: str | None = None
    language: str | None = None
    parameters: list[str] = field(default_factory=list)
    client_reference: str = ""
    external_id: str = ""


class FakeWhatsAppAdapter:
    channel = "whatsapp"
    is_sandbox = True

    # Class-level, so a webhook handler that constructs its own adapter deep
    # inside a request still records into the box a test is holding. A fake
    # whose output can only be seen by the code that built it is a fake nothing
    # can assert on.
    outbox: list[Sent] = []
    fail_next: MessagingError | None = None
    timeout_next: bool = False

    def __init__(self, *, phone_number_id: str = "sandbox", **_kw):
        self.phone_number_id = phone_number_id

    @classmethod
    def reset(cls) -> None:
        cls.outbox = []
        cls.fail_next = None
        cls.timeout_next = False

    @classmethod
    def last(cls) -> Sent | None:
        return cls.outbox[-1] if cls.outbox else None

    def _check_failures(self) -> None:
        if type(self).timeout_next:
            type(self).timeout_next = False
            raise MessagingError(
                PROVIDER_UNAVAILABLE, "fake WhatsApp timed out", retryable=True
            )
        if type(self).fail_next is not None:
            exc = type(self).fail_next
            type(self).fail_next = None
            raise exc

    def _record(self, sent: Sent) -> SendResult:
        # Meta's ids look like `wamid.<base64>`. Shaped the same so nothing
        # downstream can be accidentally written to depend on the format.
        sent.external_id = f"wamid.FAKE{uuid.uuid4().hex[:20].upper()}"
        type(self).outbox.append(sent)
        logger.debug("fake whatsapp → %s: %s", sent.to, sent.body or sent.template_name)
        return SendResult(external_id=sent.external_id, status="sent",
                          raw={"sandbox": True, "clientReference": sent.client_reference})

    async def send_text(self, *, to: str, body: str, client_reference: str) -> SendResult:
        self._check_failures()
        return self._record(Sent(to=to, kind="text", body=body,
                                 client_reference=client_reference))

    async def send_image(self, *, to: str, image_url: str, caption: str | None,
                         client_reference: str) -> SendResult:
        """WhatsApp carries the caption on the image, so the fake records both."""
        sent = Sent(to=to, kind="image", body=caption, image_url=image_url,
                    client_reference=client_reference)
        sent.external_id = f"wamid.FAKE{uuid.uuid4().hex[:20]}"
        type(self).outbox.append(sent)
        logger.debug("fake whatsapp image → %s: %s", to, image_url)
        return SendResult(external_id=sent.external_id, status="sent",
                          raw={"sandbox": True, "clientReference": client_reference})


    async def send_template(
        self,
        *,
        to: str,
        template_name: str,
        language: str,
        parameters: list[str],
        client_reference: str,
    ) -> SendResult:
        self._check_failures()
        return self._record(Sent(
            to=to, kind="template", template_name=template_name, language=language,
            parameters=list(parameters), client_reference=client_reference,
        ))


def fail_with(code: str = PROVIDER_FAILED, detail: str = "fake failure",
              *, retryable: bool = False) -> None:
    FakeWhatsAppAdapter.fail_next = MessagingError(code, detail, retryable=retryable)
