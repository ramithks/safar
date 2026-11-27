"""
The templates every hotel gets on the day it connects.

A newly onboarded hotel has a WABA with **no templates on it at all**. Until one
is approved, the hotel can answer a guest who wrote in the last 24 hours and
nothing else: no booking confirmation, no check-in link, no pre-arrival note.
`templates.resolve` raises `TEMPLATE_NOT_FOUND` and the agent hands off to a
human who is asleep.

So onboarding provisions a standard set. The alternative — a screen asking a
hotelier to compose six WhatsApp templates in Meta's parameter syntax before
their AI employee works — is a screen nobody finishes.

── the set is data ──────────────────────────────────────────────────────────

One dict, below. Adding a seventh purpose is an entry here, not a code change,
and the `purpose` values are exactly the ones in `templates.PURPOSES` and in the
`message_templates_purpose_check` constraint. Three vocabularies that must
agree, and the constraint is the one that wins.

`parameters` is our ordered list of *names*; Meta only ever sees `{{1}}`,
`{{2}}`. That indirection is what lets `templates.bind` take a dict from the
agent and produce positional arguments in the template's own order — and it is
why a hotel that re-records a template under a new name breaks nothing.

── what is deliberately not automated ───────────────────────────────────────

Approval. Every row is written `pending` and only the
`message_template_status_update` webhook moves it. Writing `approved` here
would produce a template the hotel believes it can send and Meta refuses, which
surfaces as `132001` to a guest waiting for a booking confirmation.
"""
import logging
from datetime import datetime, timezone
from functools import partial

from app.db.client import get_db
from app.services.messaging.whatsapp import template_api
from app.services.messaging.whatsapp.onboarding import OnboardingError

logger = logging.getLogger(__name__)


# The standard set. English only at launch — a hotel operating in Hindi gets a
# second row per purpose, which is data, not a redeploy (see `templates.resolve`,
# which already falls back across languages).
#
# Categories are Meta's and are not a free choice: a utility template sent as
# marketing is a policy violation, and a marketing template that Meta decides is
# really utility is rejected. Anything transactional — confirmations, links a
# guest asked for, arrival details — is UTILITY. Only the re-engagement note is
# MARKETING, and it is the one a hotel can switch off.
STANDARD_TEMPLATES: tuple[dict, ...] = (
    {
        "purpose": "booking_confirmation",
        "name": "booking_confirmation_v1",
        "category": "utility",
        "parameters": ["guest_name", "property_name", "check_in", "confirmation_code"],
        "body": ("Hi {{1}}, your booking at {{2}} is confirmed.\n\n"
                 "Check-in: {{3}}\nConfirmation: {{4}}\n\n"
                 "Reply here if you need anything before you arrive."),
        "example": ["Ravi", "Anandi House", "12 September, 2pm", "AH-4821"],
    },
    {
        "purpose": "payment_confirmation",
        "name": "payment_confirmation_v1",
        "category": "utility",
        "parameters": ["guest_name", "amount", "property_name"],
        "body": ("Hi {{1}}, we've received your payment of {{2}} for your stay at "
                 "{{3}}. Thank you — your booking is secure."),
        "example": ["Ravi", "₹8,400", "Anandi House"],
    },
    {
        "purpose": "checkin_link",
        "name": "checkin_link_v1",
        "category": "utility",
        "parameters": ["guest_name", "property_name", "link"],
        "body": ("Hi {{1}}, you can check in to {{2}} online before you arrive — "
                 "it takes a minute and saves the queue at the desk.\n\n{{3}}"),
        "example": ["Ravi", "Anandi House", "https://app.staysuite.example.com/c/ah4821"],
    },
    {
        "purpose": "pre_arrival",
        "name": "pre_arrival_v1",
        "category": "utility",
        "parameters": ["guest_name", "property_name", "check_in", "address"],
        "body": ("Hi {{1}}, we're looking forward to having you at {{2}} tomorrow.\n\n"
                 "Check-in from {{3}}\n{{4}}\n\n"
                 "Reply here if you need directions or a late arrival."),
        "example": ["Ravi", "Anandi House", "2pm", "Lake Palace Road, Udaipur"],
    },
    {
        "purpose": "human_handoff",
        "name": "human_handoff_v1",
        "category": "utility",
        "parameters": ["guest_name", "property_name"],
        "body": ("Hi {{1}}, someone from {{2}} is picking this up now and will "
                 "reply shortly. Sorry to keep you waiting."),
        "example": ["Ravi", "Anandi House"],
    },
    {
        # The only MARKETING template, and the only one a hotel would ever want
        # switched off. Kept separate from the transactional five for exactly
        # that reason: a hotel that opts out of marketing must not lose its
        # booking confirmations.
        "purpose": "reengagement",
        "name": "reengagement_v1",
        "category": "marketing",
        "parameters": ["guest_name", "property_name"],
        "body": ("Hi {{1}}, it's been a while since you stayed with us at {{2}}. "
                 "If you're planning another trip, reply here and we'll find you "
                 "a room at our best direct rate."),
        "example": ["Ravi", "Anandi House"],
    },
)


def components_for(spec: dict) -> list[dict]:
    """
    Our spec → Meta's components array.

    The example is not optional. Meta rejects a template whose body has
    parameters and no sample values, with a message that says "template format
    is incorrect" and names nothing.
    """
    return [{
        "type": "BODY",
        "text": spec["body"],
        "example": {"body_text": [spec["example"]]},
    }]


def _preview(spec: dict) -> str:
    """The body with the examples substituted, for the PWA to show."""
    text = spec["body"]
    for index, value in enumerate(spec["example"], start=1):
        text = text.replace("{{%d}}" % index, str(value))
    return text


def _record(
    *, property_id: str, partner_id: str, waba_id: str, spec: dict,
    language: str, created: dict,
) -> dict:
    """Write what we asked Meta for. Status comes from Meta, never from us."""
    db = get_db()
    row = {
        "property_id": property_id,
        "partner_id": partner_id,
        "channel": "whatsapp",
        "purpose": spec["purpose"],
        "provider_name": spec["name"],
        "language": language,
        "category": spec["category"],
        "status": template_api.STATUS.get(
            str(created.get("status") or "PENDING").upper(), "pending"
        ),
        "parameters": spec["parameters"],
        "body_preview": _preview(spec),
        "components": components_for(spec),
        "waba_id": waba_id,
        "provider_template_id": str(created.get("id")) if created.get("id") else None,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        # A provisioned template is the real thing on the hotel's own WABA. The
        # flag exists to keep development templates off live connections, and
        # setting it here would make every onboarded hotel unable to send.
        "is_test": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = (
        db.table("message_templates")
        .upsert(row, on_conflict="property_id,channel,provider_name,language")
        .execute()
    )
    return (result.data or [{}])[0]


async def provision(
    *,
    property_id: str,
    partner_id: str,
    waba_id: str,
    token: str,
    language: str = "en",
) -> dict:
    """
    Create the standard set on this hotel's WABA and record it.

    **Never raises for one template.** A hotel whose re-engagement template is
    rejected still has five working ones, and failing the whole onboarding
    because of the optional marketing template would be the tail wagging the
    dog. What each template did is returned so the caller can log it and the
    PWA can show a hotelier which ones are pending.

    Runs after the connection is usable, not before: a template created against
    a WABA we then fail to subscribe to is a template on an account we cannot
    hear from.
    """
    from app.db.client import run_db

    created: list[str] = []
    failed: list[dict] = []

    for spec in STANDARD_TEMPLATES:
        try:
            meta = await template_api.create(
                waba_id=waba_id,
                token=token,
                name=spec["name"],
                language=language,
                category=spec["category"],
                components=components_for(spec),
            )
        except OnboardingError as exc:
            logger.warning("template %s not created for property %s: %s",
                           spec["name"], property_id, exc)
            failed.append({"purpose": spec["purpose"], "reason": exc.reason,
                           "detail": str(exc)[:300]})
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("template %s blew up for property %s", spec["name"], property_id)
            failed.append({"purpose": spec["purpose"], "reason": "unexpected",
                           "detail": str(exc)[:300]})
            continue

        try:
            # `run_db(fn, *args)` takes no keyword arguments — see db/client.py.
            # Passing them raises a TypeError that arrives here as "created at
            # Meta, not stored", which is a true sentence about a bug that has
            # nothing to do with Meta.
            await run_db(partial(
                _record, property_id=property_id, partner_id=partner_id,
                waba_id=waba_id, spec=spec, language=language, created=meta,
            ))
        except Exception:  # noqa: BLE001
            # The template exists at Meta and we could not write the row. Worth
            # an exception in the log rather than a warning: the next
            # provisioning run will hit "already exists" and recover, but until
            # then the hotel has a template it cannot address by purpose.
            logger.exception("template %s created at Meta but not recorded for %s",
                             spec["name"], property_id)
            failed.append({"purpose": spec["purpose"], "reason": "not_recorded",
                           "detail": "created at Meta, not stored"})
            continue

        created.append(spec["purpose"])

    logger.info("provisioned %d/%d templates for property %s on WABA %s",
                len(created), len(STANDARD_TEMPLATES), property_id, waba_id)
    return {"created": created, "failed": failed,
            "total": len(STANDARD_TEMPLATES)}
