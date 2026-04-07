"""
Supabase client singletons.

- `get_db()`       → service role client (bypasses RLS, for server-side DB ops)
- `get_user_db(token)` → anon client scoped to a user JWT (respects RLS)
"""
from functools import lru_cache
from supabase import create_client, Client
from app.config import settings


@lru_cache(maxsize=1)
def get_db() -> Client:
    """Service role Supabase client — reused across requests."""
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(settings.supabase_url, settings.supabase_service_key)


def get_user_db(token: str) -> Client:
    """
    Anon client with user JWT injected.
    Used for JWT verification and user-scoped queries.
    """
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    # Inject the user's token so auth.get_user() validates it
    client.auth.set_session(token, "")
    return client
