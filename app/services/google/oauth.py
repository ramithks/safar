"""
Google OAuth2 for Business Profile — consent, exchange, and staying alive.

Deliberately the same shape as `messaging/instagram/oauth.py`: a signed
stateless `state`, an authorize URL builder, a code exchange, and a typed error
carrying a `reason` slug the callback turns into a sentence. A second OAuth
implementation that looked different would be a second set of mistakes.

Two things genuinely differ from Meta, and both are load-bearing.

**Offline access.** Google's access tokens last an hour. `access_type=offline`
plus `prompt=consent` is what makes Google return a *refresh* token, and the
refresh token is the credential — the access token is a derived, disposable
thing. `prompt=consent` is not belt-and-braces: Google returns a refresh token
only on the first consent for a client/user pair, so an owner reconnecting after
we lost their token would otherwise get an access token, no refresh token, and a
connection that dies again in an hour.

**The refresh happens here, not at the call site.** `access_token()` is the only
way anything in this package reaches Google, and it refreshes on the way through
when the stored token is within the skew of expiring. Callers cannot forget.
"""
import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.services.messaging import store

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

CHANNEL = "google"

_TIMEOUT = 20.0


class GoogleOAuthError(RuntimeError):
    """
    A failure with a slug the UI can map to a sentence a hotelier understands.

    The slug never reaches a screen. `api/v1/google/messages.py` owns the
    translation, so the vocabulary Google uses ("invalid_grant") and the
    vocabulary an owner reads ("Your Google connection has expired") never have
    to be the same word.
    """

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scopes: tuple[str, ...]


def is_configured() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


def _client_secret() -> str:
    secret = settings.google_oauth_client_secret
    if not secret:
        raise GoogleOAuthError("not_configured", "GOOGLE_OAUTH_CLIENT_SECRET is not set")
    return secret


# ── state ───────────────────────────────────────────────────────────────────
#
# Same construction as Instagram's, and the same reasoning: the callback is
# unauthenticated, so the state has to carry its own proof. Signed rather than
# stored, so it survives a deploy mid-consent and leaves nothing to clean up
# when an owner closes the tab.


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_state(*, property_id: str, partner_id: str, profile_id: str) -> str:
    payload = json.dumps(
        {"p": property_id, "pa": partner_id, "u": profile_id, "t": int(time.time())},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = _b64e(payload)
    sig = _b64e(hmac.new(_client_secret().encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_state(state: str | None) -> dict:
    if not state or "." not in state:
        raise GoogleOAuthError("bad_state", "state missing or malformed")

    body, _, sig = state.partition(".")
    expected = _b64e(hmac.new(_client_secret().encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise GoogleOAuthError("bad_state", "state signature did not verify")

    try:
        claims = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001
        raise GoogleOAuthError("bad_state", f"state payload unreadable: {exc}") from exc

    age = int(time.time()) - int(claims.get("t") or 0)
    if age > settings.oauth_state_ttl_seconds:
        raise GoogleOAuthError(
            "state_expired", f"state is {age}s old, limit {settings.oauth_state_ttl_seconds}s"
        )
    if age < -60:
        raise GoogleOAuthError("bad_state", "state is issued in the future")

    return claims


# ── the flow ────────────────────────────────────────────────────────────────


def authorize_url(state: str) -> str:
    if not is_configured():
        raise GoogleOAuthError(
            "not_configured",
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set",
        )
    query = urlencode(
        {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": settings.google_oauth_scopes,
            "state": state,
            # See the module docstring. Both are required for a refresh token.
            "access_type": "offline",
            "prompt": "consent",
            # Cuts the "choose an account" step for an owner with one Google
            # account, and is harmless for one with several.
            "include_granted_scopes": "true",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _token_error(payload: dict, status_code: int) -> GoogleOAuthError:
    err = str(payload.get("error") or "")
    detail = str(payload.get("error_description") or payload)

    # `invalid_grant` is the one worth naming. It means the refresh token is
    # dead — revoked in the owner's Google account, or expired after six months
    # unused — and the only fix is a new consent. Every other token error is
    # ours to fix and must not tell the owner to reconnect.
    if err == "invalid_grant":
        return GoogleOAuthError("reconnect_required", detail)
    if status_code >= 500:
        return GoogleOAuthError("google_unavailable", detail)
    return GoogleOAuthError(err or "token_failed", detail)


async def _post_token(form: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(TOKEN_URL, data=form)
    except httpx.HTTPError as exc:
        raise GoogleOAuthError("google_unavailable", str(exc)) from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleOAuthError("token_failed", f"non-JSON from Google: {response.text[:200]}") from exc

    if response.status_code >= 400:
        raise _token_error(payload, response.status_code)
    return payload


async def exchange_code(code: str) -> TokenSet:
    """Consent code → the credential we keep."""
    payload = await _post_token(
        {
            "code": code,
            "client_id": settings.google_oauth_client_id or "",
            "client_secret": _client_secret(),
            "redirect_uri": settings.google_oauth_redirect_uri,
            "grant_type": "authorization_code",
        }
    )

    access = payload.get("access_token")
    if not access:
        raise GoogleOAuthError("token_failed", "Google returned no access token")

    refresh = payload.get("refresh_token")
    if not refresh:
        # Not fatal to *this* request — the access token works for an hour — but
        # the connection would die overnight with no way back except a reconnect
        # nobody knows to do. Refuse it now, while the owner is still here.
        raise GoogleOAuthError(
            "no_refresh_token",
            "Google returned no refresh token; the consent did not request offline access",
        )

    return TokenSet(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(payload["expires_in"]) if payload.get("expires_in") else None,
        scopes=tuple(str(payload.get("scope") or "").split()),
    )


async def refresh_access_token(refresh_token: str) -> TokenSet:
    payload = await _post_token(
        {
            "refresh_token": refresh_token,
            "client_id": settings.google_oauth_client_id or "",
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        }
    )
    access = payload.get("access_token")
    if not access:
        raise GoogleOAuthError("token_failed", "Google returned no access token on refresh")

    return TokenSet(
        access_token=access,
        # Google omits this on refresh. `store.save_token` leaves the stored one
        # in place when it is None — see the note there, it is the whole reason
        # that parameter is optional.
        refresh_token=payload.get("refresh_token"),
        expires_in=int(payload["expires_in"]) if payload.get("expires_in") else None,
        scopes=tuple(str(payload.get("scope") or "").split()),
    )


async def revoke(token: str) -> bool:
    """Best effort. A token Google has already forgotten is not an error."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(REVOKE_URL, data={"token": token})
        return response.status_code < 400
    except httpx.HTTPError:
        logger.warning("could not reach Google to revoke a token", exc_info=True)
        return False


# ── the one way to get a usable token ───────────────────────────────────────


async def access_token(*, credentials_ref: str) -> str:
    """
    A live access token for this hotel, refreshing if the stored one is stale.

    Everything in `client.py` goes through here. That is why no API call in this
    package has to think about expiry: there is one door, and the refresh is
    behind it.
    """
    from app.db.client import run_db

    token = await run_db(store.load_token, credentials_ref, CHANNEL)
    if token:
        return token

    # `load_token` returns None for both "expired" and "never existed". Only the
    # first is recoverable, and the refresh token is what tells them apart.
    refresh = await run_db(store.load_refresh_token, credentials_ref, CHANNEL)
    if not refresh:
        raise GoogleOAuthError(
            "not_connected", f"no live Google credential for {credentials_ref}"
        )

    fresh = await refresh_access_token(refresh)
    await run_db(
        store.update_access_token,
        channel=CHANNEL,
        credentials_ref=credentials_ref,
        access_token=fresh.access_token,
        expires_in=fresh.expires_in,
        refresh_token=fresh.refresh_token,
    )
    logger.info("refreshed Google access token for %s", credentials_ref)
    return fresh.access_token
