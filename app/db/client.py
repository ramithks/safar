"""
Supabase clients.

- `get_db()`       → service role client (bypasses RLS, for server-side DB ops),
                     ONE PER THREAD — see get_db docstring.
- `get_user_db(token)` → anon client scoped to a user JWT (respects RLS)

Both clients auto-retry transient "Server disconnected" errors. Supabase's edge
closes idle keep-alive connections; the next request that reuses a stale pooled
connection fails with `httpx.RemoteProtocolError` *before* the request reaches
PostgREST. Retrying with a fresh connection fixes it. We only retry idempotent
methods so a half-sent write is never replayed.
"""
import asyncio
import logging
import random
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)

# Dedicated executor for ALL sync Supabase calls made from async code, isolated
# from the default executor (busy with video downloads / curl_cffi scrapers whose
# fd churn corrupted shared TLS). Each thread gets its OWN thread-local client
# (see get_db), so N threads = N independent connection pools — no shared socket
# to corrupt. Sized so a couple of concurrent /plan requests (3-4 DB writes each)
# don't serialize behind one another. Override with DB_EXECUTOR_WORKERS.
_DB_EXECUTOR = ThreadPoolExecutor(
    max_workers=settings.db_executor_workers, thread_name_prefix="tb-db"
)


async def run_db(fn, *args):
    """Run a sync DB callable on the dedicated DB executor. `get_db()` must be
    called INSIDE `fn` (clients are per-thread and must not cross threads)."""
    return await asyncio.get_running_loop().run_in_executor(_DB_EXECUTOR, fn, *args)


# Transient failures that mean "the connection died, retry on a fresh one" —
# broader than _RETRYABLE below because it also covers the raw ssl.SSLError
# (SSLV3_ALERT_BAD_RECORD_MAC) the curl_cffi scraper fd churn induces, which
# httpx doesn't always normalise into a TransportError.
_WRITE_RETRYABLE = (httpx.TransportError, ssl.SSLError, ConnectionError)


def _resilient(fn, label: str, max_seconds: float):
    """Run `fn` (a sync DB op), retrying on a FRESH client through the
    multi-second TLS-corruption windows the scrapers induce, until it lands or
    `max_seconds` elapses. Runs entirely on one executor thread, so reset_db()
    rebuilds *this* thread's client before each retry — same recovery the
    trip_state append loop uses. IDEMPOTENT ops only (upsert on a stable key):
    a retry may re-send a request whose response was merely lost."""
    deadline = time.monotonic() + max_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except _WRITE_RETRYABLE as exc:
            if time.monotonic() >= deadline:
                logger.error("[db] %s gave up after %.0fs: %s", label, max_seconds, exc)
                raise
            logger.warning(
                "[db] %s transient %s — fresh client, retry %d",
                label, type(exc).__name__, attempt,
            )
            reset_db()
            time.sleep(min(0.3 * (2 ** (attempt - 1)), 5.0) + random.uniform(0, 0.3))


async def run_db_resilient(fn, *, label: str, max_seconds: float = 180.0):
    """`run_db` for IDEMPOTENT writes that must survive an SSL-corruption storm.
    Off the request path (call from a background task) — it can retry for minutes."""
    return await run_db(lambda: _resilient(fn, label, max_seconds))

# Transport-level errors that mean the connection died, not that the server
# rejected the request — safe to retry on a fresh connection.
_RETRYABLE = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.PoolTimeout,
    # Raw bad_record_mac from the scraper fd-churn isn't always normalised into
    # an httpx.TransportError — catch it here too (mirrors _WRITE_RETRYABLE).
    ssl.SSLError,
)
_IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "DELETE"}


def _install_retry(session: httpx.Client, attempts: int = 6) -> None:
    """Wrap an httpx.Client.send so stale-connection drops auto-retry.

    Retries are spaced with jittered exponential backoff (~0.3s → ~4s, ~10s
    total across the default 6 attempts). The backoff is the whole point: the
    curl_cffi scraper fd-churn corrupts Supabase's TLS sockets in bursts that
    last several seconds, and retries fired back-to-back all die inside the same
    window (the old no-sleep 3× loop is why user-facing GETs 500'd mid-storm).
    Spacing the retries lets an idempotent read land after the burst passes —
    the same recovery the trip_state append loop already relied on for writes.
    """
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
                if i < attempts - 1:
                    time.sleep(min(0.3 * (2 ** i), 4.0) + random.uniform(0, 0.3))
        assert last_exc is not None
        raise last_exc

    session.send = _send_with_retry  # type: ignore[method-assign]
    session._tb_retry = True  # type: ignore[attr-defined]


def _force_http1(session: httpx.Client) -> None:
    """
    postgrest builds its sync client with http2=True. HTTP/2 multiplexes every
    request over one connection, and httpcore's sync HTTP/2 stack STORES a read
    error and re-raises it for all later requests on that connection — one
    EAGAIN poisons every write after it (observed as cascading `[Errno 35]`
    failures). HTTP/1.1 gives one request per pooled connection and clean
    recovery, at no practical cost for PostgREST traffic.
    """
    try:
        session._transport = httpx.HTTPTransport(
            http2=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[db] could not force http/1.1: %s", exc)


def _wrap_client(client: Client) -> Client:
    """Install the retry wrapper + HTTP/1.1 transport on REST/Storage sessions."""
    for name in ("postgrest", "storage"):
        try:
            sub = getattr(client, name, None)
            session = getattr(sub, "session", None)
            if isinstance(session, httpx.Client):
                _force_http1(session)
                _install_retry(session)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[db] could not install retry on %s: %s", name, exc)
    return client


# One service-role client PER THREAD. A single process-wide client shared
# across executor threads corrupted the TLS stream under concurrent writes
# (server answers SSLV3_ALERT_BAD_RECORD_MAC → cascading ReadErrors that
# poisoned every in-flight caller, observed 2026-07-11 during trip generation:
# the generator's emit and the supervisor's on_phase write simultaneously).
# Executor threads are pooled and bounded, so the client count is too.
_thread_local = threading.local()


def get_db() -> Client:
    """Service role Supabase client — one per thread, reused across requests."""
    client = getattr(_thread_local, "client", None)
    if client is None:
        if not settings.supabase_url or not settings.supabase_service_key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
        client = _wrap_client(
            create_client(settings.supabase_url, settings.supabase_service_key)
        )
        _thread_local.client = client
    return client


def reset_db() -> None:
    """
    Drop the CURRENT THREAD's service-role client so its next get_db() builds a
    fresh one with a clean connection pool — for callers recovering from
    transport-level failures that may have poisoned the pool.
    """
    _thread_local.client = None


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
