"""
JWT authentication dependency for FastAPI routes.

Verification order:
  1. Local — HS256 with SUPABASE_JWT_SECRET, or RS256/ES256 via the project's
     JWKS endpoint (keys cached). No per-request network call.
  2. Fallback — supabase auth.get_user() network round-trip, only when local
     verification isn't configured for the token's algorithm.

Usage:
    @router.get("/protected")
    async def endpoint(user = Depends(require_user)):
        return {"user_id": user.id}
"""
import asyncio
import logging
import time
from types import SimpleNamespace

import jwt
import sentry_sdk
from fastapi import Header, HTTPException, status
from supabase import create_client, Client

from app.config import settings

logger = logging.getLogger(__name__)

_supabase_client: Client | None = None
_jwks_client: jwt.PyJWKClient | None = None

# Verified-token cache: token → (user, expires_at). A burst of parallel requests
# (the workspace fires several GETs at once) must not each pay a network
# verification — and must not each be able to fail one.
_token_cache: dict[str, tuple[SimpleNamespace, float]] = {}
_TOKEN_CACHE_TTL = 300.0
_TOKEN_CACHE_MAX = 1024

_REMOTE_RETRIES = 3
_hinted_missing_secret = False


def _get_auth_client() -> Client:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(settings.supabase_url, settings.supabase_anon_key)
    return _supabase_client


def _get_jwks_client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{settings.supabase_url}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
        )
    return _jwks_client


def _verify_local(token: str):
    """
    Verify the JWT signature locally. Returns a user-like object on success,
    None when local verification isn't possible for this token (caller should
    fall back to the network check). Raises jwt.InvalidTokenError on a bad token.
    """
    header = jwt.get_unverified_header(token)
    alg = header.get("alg")

    if alg == "HS256":
        if not settings.supabase_jwt_secret:
            global _hinted_missing_secret
            if not _hinted_missing_secret:
                _hinted_missing_secret = True
                logger.warning(
                    "HS256 token but SUPABASE_JWT_SECRET is not set — every request "
                    "falls back to a network verification round-trip. Set "
                    "SUPABASE_JWT_SECRET to verify locally."
                )
            return None
        payload = jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    elif alg in ("RS256", "ES256"):
        key = _get_jwks_client().get_signing_key_from_jwt(token).key
        payload = jwt.decode(token, key, algorithms=[alg], audience="authenticated")
    else:
        return None

    return SimpleNamespace(
        id=payload["sub"],
        email=payload.get("email"),
        # Carried so the rate limiter can tell a guest apart from a real
        # account. The remote path returns Supabase's own User, which already
        # has this attribute; keep the two shapes in step.
        is_anonymous=bool(payload.get("is_anonymous", False)),
    )


def _is_transient(exc: Exception) -> bool:
    """A network hiccup (EAGAIN, timeouts, supabase AuthRetryableError), not a
    verdict on the token. Must never be reported to the client as a 401 — the
    frontend treats 401 as 'logged out'."""
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return True
    return "retryable" in type(exc).__name__.lower()


def _verify_remote(token: str):
    """Network fallback: ask Supabase Auth to validate the token. Retries
    transient failures with a short backoff; re-raises the last transient error
    so the caller can answer 503 instead of 401."""
    client = _get_auth_client()
    last_exc: Exception | None = None
    for attempt in range(_REMOTE_RETRIES):
        try:
            result = client.auth.get_user(token)
            if not result or not result.user:
                return None
            return result.user
        except Exception as exc:  # noqa: BLE001 — classify below
            if not _is_transient(exc):
                raise
            last_exc = exc
            if attempt < _REMOTE_RETRIES - 1:
                time.sleep(0.2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _cache_get(token: str):
    hit = _token_cache.get(token)
    if not hit:
        return None
    user, expires_at = hit
    if time.monotonic() >= expires_at:
        _token_cache.pop(token, None)
        return None
    return user


def verified_sub(token: str) -> str | None:
    """
    The user id for a token this process has ALREADY verified, else None.

    Read-only and allocation-free: it never verifies, never touches the network,
    and a token that was merely presented — not checked — is not in the cache
    and so answers None.

    That distinction is the point. The rate limiter used to read `sub` straight
    out of an unverified JWT, which meant anyone could mint a token with a
    random `sub` per request and get a fresh bucket every time — the limit that
    is supposed to make ref enumeration impractical could be stepped around
    with two lines of script. Dependencies resolve before the limiter runs, so
    on an authenticated endpoint the token is in this cache by then and the
    per-user bucket still works; everywhere else the limiter falls back to IP.
    """
    user = _cache_get(token)
    return str(user.id) if user is not None else None


def verified_identity(token: str) -> tuple[str, bool] | None:
    """
    (user_id, is_anonymous) for a token this process has ALREADY verified.

    Same read-only contract as verified_sub. The anonymity flag exists because
    a per-user rate-limit bucket is only meaningful when obtaining a user costs
    something — see the note in app/core/limiter.py.
    """
    user = _cache_get(token)
    if user is None:
        return None
    return str(user.id), bool(getattr(user, "is_anonymous", False))


def _cache_put(token: str, user) -> None:
    if len(_token_cache) >= _TOKEN_CACHE_MAX:
        _token_cache.clear()
    ttl = _TOKEN_CACHE_TTL
    try:  # never cache past the token's own expiry
        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
        if exp:
            ttl = min(ttl, max(exp - time.time(), 0))
    except Exception:  # noqa: BLE001
        pass
    if ttl > 0:
        _token_cache[token] = (
            SimpleNamespace(id=user.id, email=getattr(user, "email", None)),
            time.monotonic() + ttl,
        )


async def optional_user(authorization: str = Header(default=None)):
    """
    The verified user when a usable token is present, otherwise None.

    For endpoints that serve signed-out callers but want to attribute the row
    when they ARE signed in — marketplace checkout, where a traveller need not
    have an account. A bad or expired token is treated as absent rather than
    rejected: the caller loses attribution, not the ability to buy.

    This must never be used for authorization. It answers "who is this, if
    anyone", not "may they do this".
    """
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return await require_user(authorization)
    except HTTPException:
        return None


async def require_user(authorization: str = Header(...)):
    """
    Validates the Supabase JWT from the Authorization: Bearer <token> header.
    Returns an object with .id (and .email) on success, raises 401 on failure.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")

    token = authorization[len("Bearer "):]

    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Auth not configured")

    cached = _cache_get(token)
    if cached is not None:
        sentry_sdk.set_user({"id": str(cached.id), "email": cached.email})
        return cached

    try:
        # to_thread: JWKS fetch (first call) and the supabase fallback are
        # blocking I/O — keep them off the event loop.
        user = await asyncio.to_thread(_verify_local, token)
        if user is None:
            user = await asyncio.to_thread(_verify_remote, token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        _cache_put(token, user)
        # Tag Sentry events with the Supabase user so issues are traceable back
        # to a specific user/row in Supabase.
        sentry_sdk.set_user({"id": str(user.id), "email": getattr(user, "email", None)})
        return user
    except HTTPException:
        raise
    except jwt.InvalidTokenError as e:
        logger.info(f"JWT rejected: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    except Exception as e:
        if _is_transient(e):
            # The token was never judged invalid — we just couldn't reach the
            # verifier. 503 tells the client to retry, not to log the user out.
            logger.warning(f"Token verification unreachable: {type(e).__name__}: {e}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth service temporarily unreachable — retry",
            )
        logger.warning(f"Token verification failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token verification failed")
