"""
Instagram Business Login — the flow that lets a hotel connect its own account.

Until this existed, an Instagram account was connected by inserting a row and
putting a token in the environment by hand. That works for one account and
cannot work for the hundredth, because the token is the hotel's and only the
hotel can grant it.

    PWA  ──► POST .../instagram/oauth/start   → signed state + Meta's URL
    owner ──► instagram.com consent screen
    Meta ──► GET  .../instagram/oauth/callback?code=…&state=…
                    │
                    ├─ code   → short-lived token   (api.instagram.com)
                    ├─ short  → long-lived, 60 days (graph.instagram.com)
                    ├─ token  → user_id + username  (graph.instagram.com/me)
                    └─ store encrypted, then 302 back to the PWA

**This is "Instagram API with Instagram Login", not "with Facebook Login".**
The hosts differ (`graph.instagram.com`, not `graph.facebook.com`), the app id
is the Instagram app id rather than the Facebook one, and the scopes are the
`instagram_business_*` family. Mixing the two halves produces an "Invalid
platform app" from the token exchange that names nothing.

Nothing here touches the database or a request object: it takes strings and
returns strings, so the route can be read as a sequence of steps and each step
can be tested without a webserver.
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

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_HOST = "https://graph.instagram.com"

_TIMEOUT = 20.0


class InstagramOAuthError(RuntimeError):
    """Anything that went wrong between the consent screen and a stored token."""

    def __init__(self, reason: str, message: str):
        # `reason` is a short slug the callback puts in the redirect so the PWA
        # can say something specific; `message` is for the log.
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class ConnectedAccount:
    access_token: str
    instagram_account_id: str
    username: str | None
    expires_in: int | None
    scopes: tuple[str, ...]


def client_secret() -> str | None:
    """
    The secret the token exchange and the `state` signature use.

    Falls back to `INSTAGRAM_APP_SECRET`, because for Instagram Login they are
    normally the same string — Meta shows one "Instagram app secret" that both
    signs webhooks and authenticates the token exchange. The separate variable
    exists for the case where they are not, and setting it is what makes that
    case configurable rather than a fork in the code.
    """
    return settings.instagram_oauth_client_secret or settings.instagram_app_secret


def is_configured() -> bool:
    return bool(settings.instagram_app_id and client_secret())


# ── state ───────────────────────────────────────────────────────────────────
#
# The `state` parameter is a CSRF token, and here it is also the only way the
# callback knows which hotel the consent belongs to. Meta hands it back
# verbatim to an unauthenticated endpoint, so it has to carry its own proof:
# HMAC over the payload with the app secret, plus an issued-at that expires.
#
# Signed rather than stored in Redis on purpose — a stateless state survives a
# deploy mid-consent, and there is nothing to clean up when an owner abandons
# the screen.


def _state_key() -> bytes:
    secret = client_secret() or ""
    if not secret:
        raise InstagramOAuthError("not_configured", "no Instagram app secret to sign state with")
    return secret.encode()


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
    sig = _b64e(hmac.new(_state_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_state(state: str | None) -> dict:
    if not state or "." not in state:
        raise InstagramOAuthError("bad_state", "state missing or malformed")

    body, _, sig = state.partition(".")
    expected = _b64e(hmac.new(_state_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise InstagramOAuthError("bad_state", "state signature did not verify")

    try:
        claims = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001
        raise InstagramOAuthError("bad_state", f"state payload unreadable: {exc}") from exc

    age = int(time.time()) - int(claims.get("t") or 0)
    if age > settings.oauth_state_ttl_seconds:
        raise InstagramOAuthError(
            "state_expired",
            f"state is {age}s old, limit {settings.oauth_state_ttl_seconds}s",
        )
    # A negative age larger than a little clock skew means the payload was
    # crafted, not merely stale.
    if age < -60:
        raise InstagramOAuthError("bad_state", "state is issued in the future")

    return claims


# ── the flow ────────────────────────────────────────────────────────────────


def authorize_url(state: str) -> str:
    if not is_configured():
        raise InstagramOAuthError(
            "not_configured",
            "INSTAGRAM_APP_ID / INSTAGRAM_OAUTH_CLIENT_SECRET are not set",
        )
    query = urlencode(
        {
            "client_id": settings.instagram_app_id,
            "redirect_uri": settings.instagram_redirect_uri,
            "response_type": "code",
            "scope": settings.instagram_oauth_scopes,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _meta_error(payload: dict, fallback: str) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        return err.get("message") or fallback
    return payload.get("error_message") or payload.get("error_description") or err or fallback


async def exchange_code(code: str) -> tuple[str, str, tuple[str, ...]]:
    """Authorization code → short-lived token, the Instagram user id, and scopes."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "client_id": settings.instagram_app_id,
                "client_secret": client_secret(),
                "grant_type": "authorization_code",
                # Meta re-checks this against the value used on the authorize
                # call *and* against the app config. All three must match.
                "redirect_uri": settings.instagram_redirect_uri,
                "code": code,
            },
        )

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        payload = {}

    if r.status_code >= 400 or not payload.get("access_token"):
        message = _meta_error(payload, r.text[:200])
        # "Invalid platform app" is not a failed connection, it is a broken
        # deployment: the app id is not an Instagram Login app (most often the
        # Facebook app id, which looks near-identical). Separated from the
        # generic slug because the owner can retry the first one forever and it
        # will never work, and because it is the one failure we caused.
        reason = ("app_misconfigured" if "platform app" in message.lower()
                  else "code_exchange_failed")
        raise InstagramOAuthError(
            reason, f"Instagram refused the code ({r.status_code}): {message}",
        )

    permissions = payload.get("permissions") or []
    if isinstance(permissions, str):
        permissions = [p for p in permissions.split(",") if p]

    return (
        payload["access_token"],
        str(payload.get("user_id") or ""),
        tuple(permissions),
    )


async def exchange_long_lived(short_token: str) -> tuple[str, int | None]:
    """Short-lived (1 hour) → long-lived (60 days)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{GRAPH_HOST}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": client_secret(),
                "access_token": short_token,
            },
        )

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        payload = {}

    if r.status_code >= 400 or not payload.get("access_token"):
        raise InstagramOAuthError(
            "long_lived_exchange_failed",
            f"could not upgrade the token ({r.status_code}): "
            f"{_meta_error(payload, r.text[:200])}",
        )

    expires_in = payload.get("expires_in")
    return payload["access_token"], int(expires_in) if expires_in else None


async def refresh_long_lived(token: str) -> tuple[str, int | None]:
    """
    Extend a long-lived token for another 60 days.

    Meta only refreshes a token that is at least 24 hours old and not yet
    expired. Once it has expired there is no refresh — the hotel reconnects.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{GRAPH_HOST}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        payload = {}

    if r.status_code >= 400 or not payload.get("access_token"):
        raise InstagramOAuthError(
            "refresh_failed",
            f"could not refresh the token ({r.status_code}): "
            f"{_meta_error(payload, r.text[:200])}",
        )

    expires_in = payload.get("expires_in")
    return payload["access_token"], int(expires_in) if expires_in else None


async def fetch_profile(token: str) -> tuple[str, str | None]:
    """The account id to key the connection on, and the @handle to show the owner."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{GRAPH_HOST}/v23.0/me",
            params={"fields": "user_id,username", "access_token": token},
        )

    try:
        payload = r.json()
    except Exception:  # noqa: BLE001
        payload = {}

    if r.status_code >= 400:
        raise InstagramOAuthError(
            "profile_failed",
            f"token accepted but the account would not read back ({r.status_code}): "
            f"{_meta_error(payload, r.text[:200])}",
        )

    account_id = str(payload.get("user_id") or payload.get("id") or "")
    if not account_id:
        raise InstagramOAuthError("profile_failed", "no account id on the profile response")

    return account_id, payload.get("username")


async def connect(code: str) -> ConnectedAccount:
    """The whole exchange, as one step, because no caller wants a partial one."""
    short_token, user_id, scopes = await exchange_code(code)
    long_token, expires_in = await exchange_long_lived(short_token)
    account_id, username = await fetch_profile(long_token)

    # The id from the profile wins over the one on the code exchange: it is the
    # one the webhook will carry, and a connection keyed on the other id is a
    # connection no inbound message ever matches.
    return ConnectedAccount(
        access_token=long_token,
        instagram_account_id=account_id or user_id,
        username=username,
        expires_in=expires_in,
        scopes=scopes,
    )


# ── signed_request (deauthorize / data deletion) ────────────────────────────


def parse_signed_request(signed_request: str, app_secret: str | None) -> dict:
    """
    Meta's `signed_request`: base64url(signature).base64url(payload).

    Unsigned or wrongly-signed requests raise. These endpoints are public and
    the payload names a user whose data we are about to delete, so an unverified
    body here would be a delete-anyone button.
    """
    if not app_secret:
        raise InstagramOAuthError("not_configured", "no app secret to verify signed_request with")
    if not signed_request or "." not in signed_request:
        raise InstagramOAuthError("bad_signature", "signed_request missing or malformed")

    encoded_sig, _, encoded_payload = signed_request.partition(".")

    try:
        sig = _b64d(encoded_sig)
        payload = json.loads(_b64d(encoded_payload))
    except Exception as exc:  # noqa: BLE001
        raise InstagramOAuthError("bad_signature", f"signed_request unreadable: {exc}") from exc

    algorithm = str(payload.get("algorithm", "")).upper()
    if algorithm and algorithm.replace("-", "") != "HMACSHA256":
        raise InstagramOAuthError("bad_signature", f"unexpected algorithm {algorithm!r}")

    expected = hmac.new(app_secret.encode(), encoded_payload.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise InstagramOAuthError("bad_signature", "signed_request signature did not verify")

    return payload
