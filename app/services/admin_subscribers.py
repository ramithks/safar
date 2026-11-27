"""
Event subscribers for admin actions.

Importing this module registers everything that reacts to an admin decision.
The endpoints never call these — they emit, and whoever cares listens. Adding
push notifications, an activity feed or a Slack webhook later means adding an
`@on(...)` here and touching no endpoint.

Currently: email the partner when their listing is approved or rejected.

Note on where email lives: admin-triggered mail is sent from here because the
dashboard now talks only to this service. Traveller- and partner-triggered mail
(leads, bookings, submissions) is still sent by the website, which is where
those actions happen. Two senders, split by who triggers the action rather than
by accident.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.db.client import get_db, run_db
from app.services.events import on

logger = logging.getLogger(__name__)

RESEND_API = "https://api.resend.com/emails"


def _esc(value: str) -> str:
    """Everything here is partner-supplied and goes into HTML we send out."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _wrap(heading: str, body: str, cta: tuple[str, str] | None = None) -> str:
    button = ""
    if cta:
        label, url = cta
        button = (
            f'<p style="margin:24px 0 0"><a href="{_esc(url)}" '
            'style="display:inline-block;background:#2563eb;color:#fff;text-decoration:none;'
            f'font-weight:700;padding:12px 20px;border-radius:10px">{_esc(label)}</a></p>'
        )
    return (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#f8fafc;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a\">"
        '<table role="presentation" style="max-width:520px;margin:0 auto;background:#fff;'
        'border-radius:16px;border:1px solid #e2e8f0"><tr><td style="padding:28px">'
        '<p style="margin:0 0 4px;font-size:12px;font-weight:700;letter-spacing:.08em;'
        'text-transform:uppercase;color:#2563eb">Safar</p>'
        f'<h1 style="margin:0 0 16px;font-size:20px;line-height:1.3">{_esc(heading)}</h1>'
        f'<div style="font-size:15px;line-height:1.6;color:#334155">{body}</div>{button}'
        "</td></tr></table></body></html>"
    )


async def _send(to: str, subject: str, html: str) -> None:
    if not settings.resend_api_key:
        logger.info("[subscribers] email skipped (no RESEND_API_KEY): %s", subject)
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(
            RESEND_API,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={"from": settings.resend_from, "to": [to], "subject": subject, "html": html},
        )
        if res.status_code >= 300:
            logger.error("[subscribers] resend %s: %s", res.status_code, res.text[:300])


async def _partner_email(partner_id: str | None) -> tuple[str | None, str]:
    """(email, brand_name) for a partner, or (None, fallback)."""
    if not partner_id:
        return None, "there"
    try:
        res = await run_db(
            lambda: get_db()
            .table("partners")
            .select("email, brand_name")
            .eq("id", partner_id)
            .maybe_single()
            .execute()
        )
        row = (res.data if res else None) or {}
        return row.get("email"), row.get("brand_name") or "there"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[subscribers] partner lookup failed: %s", exc)
        return None, "there"


@on("listing.approved")
async def email_on_approved(_event: str, payload: dict) -> None:
    email, brand = await _partner_email(payload.get("partner_id"))
    if not email:
        return

    title = payload.get("entity_label") or "Your listing"
    url = payload.get("public_url") or settings.site_url

    await _send(
        email,
        f'"{title}" is live on Safar',
        _wrap(
            "You're live",
            f'<p style="margin:0 0 12px"><strong>{_esc(title)}</strong> is now on the marketplace '
            "and can be recommended inside AI-planned itineraries.</p>"
            f'<p style="margin:0;color:#64748b;font-size:14px">Enquiries and bookings appear in '
            "your dashboard, and we'll email you the moment one lands.</p>",
            ("See it live", url),
        ),
    )
    logger.info("[subscribers] approval email -> %s (%s)", email, brand)


@on("listing.rejected")
async def email_on_rejected(_event: str, payload: dict) -> None:
    email, _brand = await _partner_email(payload.get("partner_id"))
    if not email:
        return

    title = payload.get("entity_label") or "Your listing"
    reason = payload.get("reason") or "Needs more detail before it can go live."

    await _send(
        email,
        f'"{title}" needs a couple of changes',
        _wrap(
            "Almost there",
            f'<p style="margin:0 0 12px">We looked at <strong>{_esc(title)}</strong> and it needs '
            "a bit more before it can go live:</p>"
            f'<p style="margin:0 0 16px;padding:12px;background:#fef2f2;border-radius:10px;'
            f'color:#991b1b">{_esc(reason)}</p>'
            '<p style="margin:0;color:#64748b;font-size:14px">Fix that and resubmit — it comes '
            "straight back to us, no need to start over.</p>",
            ("Edit and resubmit", f"{settings.site_url}/partner/dashboard/listings"),
        ),
    )


@on("*")
async def log_every_admin_event(event: str, payload: dict) -> None:
    """Firehose for now; the seed of an activity feed later."""
    logger.info(
        "[event] %s actor=%s entity=%s",
        event, payload.get("actor"), payload.get("entity_label") or payload.get("entity_id"),
    )
