"""
Creating and reading templates on a hotel's own WABA.

**Before this module, no code had ever created a template.** Rows in
`message_templates` were written by scripts with `status = 'approved'` already
set, because there was one WABA and a person approved its templates in the Meta
console. That does not survive a second hotel: templates belong to a WABA, a
newly onboarded hotel's WABA has none, and every purpose would fail at
`templates.resolve` with "this property has no booking_confirmation template".

It is also half of what Meta's App Review asks to see. Advanced access to
`whatsapp_business_management` is granted on the evidence of an app creating a
template through the API — not of a template existing.

── the two vocabularies ─────────────────────────────────────────────────────

Meta says `UTILITY`; we say `utility`. Meta nests body text inside a components
array; we keep an ordered list of parameter *names* so `templates.bind` can turn
a dict into positional arguments. This module is the only place both are known,
which is what keeps Meta's shape out of `templates.py` and our shape out of the
wire.
"""
import logging

import httpx

from app.config import settings
from app.services.messaging.whatsapp.onboarding import OnboardingError, _raise_for

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# Meta's statuses, lowercased into ours. `PAUSED` and `PENDING_DELETION` are
# real and mean the template cannot be sent right now, which is what `disabled`
# means to `templates.resolve` — so they map there rather than inventing two
# more states nothing branches on.
STATUS = {
    "APPROVED": "approved",
    "PENDING": "pending",
    "IN_APPEAL": "pending",
    "REJECTED": "rejected",
    "PENDING_DELETION": "disabled",
    "DELETED": "disabled",
    "DISABLED": "disabled",
    "PAUSED": "disabled",
    "LIMIT_EXCEEDED": "disabled",
}


def _base() -> str:
    return settings.whatsapp_api_base.rstrip("/")


async def create(
    *,
    waba_id: str,
    token: str,
    name: str,
    language: str,
    category: str,
    components: list[dict],
) -> dict:
    """
    Submit one template for Meta's review.

    Returns Meta's `{id, status, category}`. The status is almost always
    `PENDING` — approval is Meta's to grant and arrives later as a
    `message_template_status_update` webhook, which is why nothing here waits
    for it and nothing writes `approved`.

    A name that already exists on the WABA is a `2388023`. Treated as success
    with the existing template looked up, because re-running onboarding for a
    hotel that half-completed it must not stop at the first template it already
    made.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            return _raise_for(
                await client.post(
                    f"{_base()}/{waba_id}/message_templates",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "name": name,
                        "language": language,
                        # Meta's enum is upper case and rejects ours silently
                        # enough to be worth stating: a bad category comes back
                        # as a generic 100.
                        "category": category.upper(),
                        "components": components,
                    },
                ),
                f"creating the {name!r} template",
            )
        except OnboardingError as exc:
            if "already exists" not in str(exc).lower():
                raise
            logger.info("template %s already exists on WABA %s", name, waba_id)

        existing = await find(waba_id=waba_id, token=token, name=name)
        if existing:
            return existing
        raise OnboardingError(
            "template_conflict",
            f"Meta says {name!r} already exists on WABA {waba_id} but will not "
            "return it. It may belong to a deleted template still inside Meta's "
            "30-day name lock.",
        )


async def listing(*, waba_id: str, token: str, limit: int = 100) -> list[dict]:
    """Every template on the WABA, as Meta currently sees them."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        payload = _raise_for(
            await client.get(
                f"{_base()}/{waba_id}/message_templates",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "fields": "id,name,status,category,language,components,rejected_reason",
                    "limit": limit,
                },
            ),
            "listing templates",
        )
    return payload.get("data") or []


async def find(*, waba_id: str, token: str, name: str) -> dict | None:
    for template in await listing(waba_id=waba_id, token=token):
        if template.get("name") == name:
            return template
    return None


async def delete(*, waba_id: str, token: str, name: str) -> bool:
    """
    Remove a template from the WABA.

    ⚠ Meta locks the *name* for 30 days after a deletion. A template deleted and
    recreated under the same name inside that window is rejected, which is why
    nothing in the onboarding path deletes anything — this exists for support,
    not for cleanup.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        payload = _raise_for(
            await client.delete(
                f"{_base()}/{waba_id}/message_templates",
                headers={"Authorization": f"Bearer {token}"},
                params={"name": name},
            ),
            f"deleting the {name!r} template",
        )
    return bool(payload.get("success", True))
