"""
Booking receipts — traveller confirmation and partner notification.

These moved here with the webhook. Sending them from the website would have
left a hole: the webhook is the path that settles a booking when the traveller
closes the tab mid-redirect, and it now runs entirely in the backend, so a
website-side sender would simply never fire for those.

Called on the transition to `paid` ONLY — `finalise_booking` returns
status="paid" exactly once per booking, so a webhook arriving after the browser
callback cannot send a second copy.
"""
import asyncio
import logging

import httpx

from app.config import settings
from app.db.client import get_db, run_db
from app.services.admin_subscribers import _esc, _send, _wrap

logger = logging.getLogger(__name__)

SITE = "https://travbuddy.in"


def _rupees(paise: int | None) -> str:
    return f"₹{(paise or 0) // 100:,}"


async def send_booking_emails(booking: dict) -> None:
    """
    Fire-and-forget. A bounced receipt must never turn a settled payment into
    an error for the traveller, so every failure is logged and swallowed.
    """
    try:
        partner_email = None
        brand = "TravBuddy"

        if booking.get("partner_id"):
            res = await run_db(
                lambda: get_db()
                .table("partners")
                .select("email, brand_name")
                .eq("id", booking["partner_id"])
                .maybe_single()
                .execute()
            )
            row = (res.data if res else None) or {}
            partner_email = row.get("email")
            brand = row.get("brand_name") or brand

        ref = booking.get("ref") or ""
        title = booking.get("item_title") or "your booking"
        travellers = booking.get("travellers") or 1
        travel_date = booking.get("travel_date") or "to be confirmed"

        tasks = []

        if booking.get("lead_email"):
            traveller_html = _wrap(
                f"You're booked — {_esc(ref)}",
                f"<p>Hi {_esc(booking.get('lead_name') or 'there')}, your booking for "
                f"<strong>{_esc(title)}</strong> is confirmed.</p>"
                f"<p><strong>Reference:</strong> {_esc(ref)}<br>"
                f"<strong>Travellers:</strong> {travellers}<br>"
                f"<strong>Date:</strong> {_esc(travel_date)}<br>"
                f"<strong>Paid:</strong> {_rupees(booking.get('amount_paise'))}</p>"
                f"<p>{_esc(brand)} will be in touch with the details.</p>",
                ("View your booking", f"{SITE}/booking/{ref}"),
            )
            tasks.append(
                _send(booking["lead_email"], f"Booking confirmed — {ref}", traveller_html)
            )

        if partner_email:
            partner_html = _wrap(
                f"New booking — {_esc(ref)}",
                f"<p>{_esc(title)} just sold {travellers} seat(s).</p>"
                f"<p><strong>Traveller:</strong> {_esc(booking.get('lead_name') or '')}<br>"
                f"<strong>Phone:</strong> {_esc(booking.get('lead_phone') or '')}<br>"
                f"<strong>Date:</strong> {_esc(travel_date)}<br>"
                f"<strong>Your payout:</strong> {_rupees(booking.get('payout_paise'))}</p>",
                ("Open your dashboard", f"{SITE}/partner/bookings"),
            )
            tasks.append(_send(partner_email, f"New booking — {ref}", partner_html))

        if tasks:
            # One bounce must not stop the other email.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    logger.error("[booking_emails] send failed: %s", result)

    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        logger.exception("[booking_emails] receipts failed for %s", booking.get("ref"))
    except Exception:  # noqa: BLE001
        logger.exception("[booking_emails] unexpected failure for %s", booking.get("ref"))
