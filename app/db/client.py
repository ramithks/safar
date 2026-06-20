"""
Supabase client singletons.

- `get_db()`       → service role client (bypasses RLS, for server-side DB ops)
- `get_user_db(token)` → anon client scoped to a user JWT (respects RLS)

Both clients auto-retry transient "Server disconnected" errors. Supabase's edge
closes idle keep-alive connections; the next request that reuses a stale pooled
connection fails with `httpx.RemoteProtocolError` *before* the request reaches
PostgREST. Retrying with a fresh connection fixes it. We only retry idempotent
methods so a half-sent write is never replayed.
"""
import logging
from functools import lru_cache

import httpx
from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)

# Transport-level errors that mean the connection died, not that the server
# rejected the request — safe to retry on a fresh connection.
_RETRYABLE = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
)
_IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "DELETE"}


def _install_retry(session: httpx.Client, attempts: int = 3) -> None:
    """Wrap an httpx.Client.send so stale-connection drops auto-retry."""
    if not isinstance(session, httpx.Client) or getattr(session, "_tb_retry", False):
        return
    _original_send = session.send

    def _send_with_retry(request: httpx.Request, **kwargs):
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return _original_send(request, **kwargs)
            except _RETRYABLE as exc:
                last_exc = exc
                # A non-idempotent request may already have been processed —
                # don't risk a duplicate write.
                if request.method not in _IDEMPOTENT:
                    raise
                logger.warning(
                    "[db] transient %s on %s %s — retry %d/%d",
                    type(exc).__name__, request.method, request.url, i + 1, attempts,
                )
        assert last_exc is not None
        raise last_exc

    session.send = _send_with_retry  # type: ignore[method-assign]
    session._tb_retry = True  # type: ignore[attr-defined]


def _wrap_client(client: Client) -> Client:
    """Install the retry wrapper on the client's REST + Storage sessions."""
    for name in ("postgrest", "storage"):
        try:
            sub = getattr(client, name, None)
            session = getattr(sub, "session", None)
            if session is not None:
                _install_retry(session)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[db] could not install retry on %s: %s", name, exc)
    return client


@lru_cache(maxsize=1)
def get_db() -> Client:
    """Service role Supabase client — reused across requests."""
    if not settings.supabase_url or not settings.supabase_service_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return _wrap_client(create_client(settings.supabase_url, settings.supabase_service_key))


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
    return _wrap_client(client)
