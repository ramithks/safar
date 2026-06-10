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
from types import SimpleNamespace

import jwt
from fastapi import Header, HTTPException, status
from supabase import create_client, Client

from app.config import settings

logger = logging.getLogger(__name__)

_supabase_client: Client | None = None
_jwks_client: jwt.PyJWKClient | None = None


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

    return SimpleNamespace(id=payload["sub"], email=payload.get("email"))


def _verify_remote(token: str):
    """Network fallback: ask Supabase Auth to validate the token."""
    client = _get_auth_client()
    result = client.auth.get_user(token)
    if not result or not result.user:
        return None
    return result.user


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

    try:
        # to_thread: JWKS fetch (first call) and the supabase fallback are
        # blocking I/O — keep them off the event loop.
        user = await asyncio.to_thread(_verify_local, token)
        if user is None:
            user = await asyncio.to_thread(_verify_remote, token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return user
    except HTTPException:
        raise
    except jwt.InvalidTokenError as e:
        logger.info(f"JWT rejected: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
    except Exception as e:
        logger.warning(f"Token verification failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token verification failed")
