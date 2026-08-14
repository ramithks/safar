"""
Templates — what may be said once the window has closed.

A caller asks for a **purpose**, never for a Meta template name:

    templates.resolve(property_id, purpose="human_handoff", language="en")

That indirection is why a template name never appears in the agent, in a prompt,
or in a service. Meta rejects templates, properties re-record them under new
names, and a hotel that operates in two languages has two rows for one purpose.
All of that is data, and none of it should be a redeploy.

Only `status = 'approved'` is sendable, and a template marked `is_test` is
refused on a live connection. Meta approval is Meta's to grant; nothing here
fakes it, and the development templates carry a column saying so.
"""
import logging

from app.db.client import get_db, run_db
from app.services.messaging.channels import (
    TEMPLATE_NOT_APPROVED,
    TEMPLATE_NOT_FOUND,
    MessagingError,
)

logger = logging.getLogger(__name__)

PURPOSES = (
    "booking_confirmation", "payment_confirmation", "checkin_link",
    "pre_arrival", "follow_up", "cancellation", "human_handoff",
    "reengagement", "other",
)


def _load(property_id: str, purpose: str, language: str, channel: str) -> list[dict]:
    db = get_db()
    return (
        db.table("message_templates")
        .select("*")
        .eq("property_id", property_id)
        .eq("channel", channel)
        .eq("purpose", purpose)
        .execute()
        .data
        or []
    )


async def resolve(
    property_id: str,
    *,
    purpose: str,
    language: str = "en",
    channel: str = "whatsapp",
    allow_test: bool = False,
) -> dict:
    """
    The approved template for this purpose, in the closest available language.

    Raises rather than returning None. A caller that has decided to send a
    template has already decided it cannot send free text, so "there is no
    template" is a dead end that needs an explicit decision — hand off — rather
    than a None somebody will forget to check.
    """
    rows = await run_db(_load, property_id, purpose, language, channel)
    if not rows:
        raise MessagingError(
            TEMPLATE_NOT_FOUND,
            f"this property has no {purpose!r} template for {channel}",
        )

    approved = [r for r in rows if r["status"] == "approved"]
    if not approved:
        statuses = ", ".join(sorted({r["status"] for r in rows}))
        raise MessagingError(
            TEMPLATE_NOT_APPROVED,
            f"the {purpose!r} template exists but is {statuses}, not approved",
        )

    if not allow_test:
        live = [r for r in approved if not r.get("is_test")]
        if not live:
            raise MessagingError(
                TEMPLATE_NOT_APPROVED,
                f"the only {purpose!r} template is a test template, "
                "which cannot be sent through a live connection",
            )
        approved = live

    # Exact language, then the base language ('en' for 'en_GB'), then whatever
    # the property has. A guest getting the right words in the wrong dialect is
    # a better outcome than silence.
    for candidate in (
        [r for r in approved if r["language"] == language],
        [r for r in approved if r["language"].split("_")[0] == language.split("_")[0]],
        approved,
    ):
        if candidate:
            return candidate[0]

    raise MessagingError(TEMPLATE_NOT_FOUND, f"no {purpose!r} template survived filtering")


def bind(template: dict, values: dict) -> list[str]:
    """
    Turn a dict into Meta's positional parameters, in the template's own order.

    A missing parameter is an empty string rather than an error. Meta rejects a
    template whose parameter count does not match, so dropping one turns a
    partially-filled message into no message at all — and the guest waiting for
    a booking confirmation would rather have one with a blank in it than
    nothing.
    """
    missing = [p for p in (template.get("parameters") or []) if p not in values]
    if missing:
        logger.warning(
            "template %s missing parameters: %s", template.get("provider_name"), missing
        )
    return [str(values.get(name, "")) for name in (template.get("parameters") or [])]
