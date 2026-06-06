"""
JWT authentication dependency for FastAPI routes.

Usage:
    @router.get("/protected")
    async def endpoint(user = Depends(require_user)):
        return {"user_id": user.id}
"""
import logging
from fastapi import Header, HTTPException, status
from app.config import settings
from supabase import create_client, Client

logger = logging.getLogger(__name__)

_supabase_client: Client | None = None


def _get_auth_client() -> Client:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = create_client(settings.supabase_url, settings.supabase_anon_key)
    return _supabase_client


async def require_user(authorization: str = Header(...)):
    """
    Validates the Supabase JWT from the Authorization: Bearer <token> header.
    Returns the Supabase User object on success, raises 401 on failure.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authorization header")

    token = authorization[len("Bearer "):]

    if not settings.supabase_url or not settings.supabase_anon_key:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Auth not configured")

    try:
        client = _get_auth_client()
        result = client.auth.get_user(token)
        if not result or not result.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return result.user
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Token verification failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token verification failed")
