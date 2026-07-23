"""
Admin authentication for the internal dashboard.

A shared passcode sent as `X-Admin-Passcode`. This is deliberately simple: the
dashboard is an internal tool with a handful of operators, and a per-user login
would mean building user management for three people.

What that buys, versus the previous arrangement (service-role key compiled into
the dashboard bundle):

  * The credential that can write to the database never leaves the server.
  * The dashboard holds a passcode that grants only the specific operations
    these endpoints expose — not arbitrary SQL against every table.
  * Rotating it is one env var, not a rebuild of every bundle ever shipped.

Limits worth being honest about: it's a shared secret, so actions are
attributable to "an admin" rather than to a person until we add real accounts.
`actor` is therefore taken from an optional `X-Admin-Actor` header — useful for
the audit trail, but self-reported and not a security boundary.

Send it over HTTPS only, and make it long and random.
"""
import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)


class AdminIdentity:
    """Who is acting, for the audit trail."""

    def __init__(self, actor: str, ip: str | None):
        self.actor = actor
        self.ip = ip

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AdminIdentity(actor={self.actor!r})"


async def require_admin(
    request: Request,
    x_admin_passcode: str | None = Header(default=None),
    x_admin_actor: str | None = Header(default=None),
) -> AdminIdentity:
    """
    Gate every admin endpoint.

    Fails closed: if no passcode is configured server-side the endpoints are
    unusable rather than open, so a missing env var can never silently expose
    the whole admin surface.
    """
    expected = (settings.admin_api_passcode or "").strip()

    if not expected:
        logger.error("[admin] ADMIN_API_PASSCODE is not set — refusing all admin requests")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Admin API is not configured.",
        )

    supplied = (x_admin_passcode or "").strip()
    # compare_digest to keep a wrong guess from leaking length/prefix by timing.
    if not supplied or not hmac.compare_digest(supplied, expected):
        # 404, not 403 — don't confirm the endpoint exists to someone guessing.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    actor = (x_admin_actor or "").strip()[:80] or "admin"
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else None
    )
    return AdminIdentity(actor=actor, ip=ip)
