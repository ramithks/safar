"""
JWT authentication dependency for FastAPI routes.

Usage:
    @router.get("/protected")
    async def endpoint(user = Depends(require_user)):
        return {"user_id": user.id}
"""
from fastapi import Header, HTTPException, status
from app.config import settings
from supabase import create_client


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
        client = create_client(settings.supabase_url, settings.supabase_anon_key)
        result = client.auth.get_user(token)
        if not result or not result.user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return result.user
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token verification failed")
