"""
Pooled curl_cffi AsyncSessions — the root-cause fix for the recurring
SSLV3_ALERT_BAD_RECORD_MAC / ReadError storms on Supabase writes.

Every scraper used to do `async with CurlAsyncSession(...) as s:` *per request*,
creating and tearing down a libcurl session (and its socket fds) on every call.
During a scraper burst that fd turnover races against the Supabase TLS sockets
living in the DB-executor threads of the *same process*: a recycled fd number
gets closed under a DB socket, its TLS stream desyncs, and the MAC check fails.

Reusing a handful of long-lived sessions collapses that churn. Because the proxy
is a single constant URL (Webshare gateway), the only axes are proxy-or-direct ×
impersonate, so the pool is tiny (≤ ~4 sessions per loop).

Loop-scoped: Celery runs each task under a fresh, short-lived `asyncio.run()`
loop. A session bound to a closed loop is unusable, so we key by the running
loop and drop sessions for loops that have gone away — the pool can't leak across
tasks. Under uvicorn there is one long-lived loop, so this is a fixed pool.

Sessions are shared across concurrent coroutines, which curl_cffi's AsyncSession
supports (it multiplexes over one curl-multi handle).
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from curl_cffi.requests import AsyncSession as CurlAsyncSession

from app.config import settings

logger = logging.getLogger(__name__)

# key = (id(loop), proxy_or_empty, impersonate_or_empty)
_pool: dict[tuple, CurlAsyncSession] = {}


# ── Experiment #2 mock (settings.mock_scrapers) ───────────────────────────────
# A no-network stand-in for a curl_cffi AsyncSession. Every verb returns the same
# deterministic empty 200 response and NO libcurl socket is ever opened, so a
# generation run under mock_scrapers exercises the full planner + persistence +
# Supabase write path with zero scraper fd churn. Scrapers parsing an empty body
# simply yield "0 results" (or raise and get caught by their own try/except) —
# fine for the experiment, whose only variable is "curl activity present vs not".
class _MockResponse:
    def __init__(self, url: str = "") -> None:
        self.status_code = 200
        self.url = url
        self.headers: dict = {}
        self.cookies: dict = {}
        self.content = b"{}"
        self.text = "{}"
        self.ok = True

    def json(self):
        return {}

    def raise_for_status(self):
        return None


class _MockSession:
    async def request(self, method: str = "GET", url: str = "", **kwargs) -> _MockResponse:
        return _MockResponse(url)

    async def get(self, url: str = "", **kwargs) -> _MockResponse:
        return _MockResponse(url)

    async def post(self, url: str = "", **kwargs) -> _MockResponse:
        return _MockResponse(url)

    async def put(self, url: str = "", **kwargs) -> _MockResponse:
        return _MockResponse(url)

    async def delete(self, url: str = "", **kwargs) -> _MockResponse:
        return _MockResponse(url)

    async def head(self, url: str = "", **kwargs) -> _MockResponse:
        return _MockResponse(url)

    async def close(self) -> None:
        return None


_mock_session = _MockSession()


def _get(proxy: str | None, impersonate: str | None) -> CurlAsyncSession:
    loop_id = id(asyncio.get_running_loop())
    key = (loop_id, proxy or "", impersonate or "")
    session = _pool.get(key)
    if session is None:
        # Evict sessions bound to other (usually closed) loops so a stream of
        # Celery tasks can't accumulate dead sessions.
        for stale in [k for k in _pool if k[0] != loop_id]:
            _pool.pop(stale, None)
        kwargs: dict = {}
        if impersonate:
            kwargs["impersonate"] = impersonate
        if proxy:
            kwargs["proxy"] = proxy
        session = CurlAsyncSession(**kwargs)
        _pool[key] = session
    return session


@asynccontextmanager
async def get_curl_session(*, proxy: str | None = None, impersonate: str | None = None):
    """Drop-in for `async with CurlAsyncSession(...) as s:` that yields a pooled,
    reused session instead of building/closing one per request. Does NOT close
    the session on exit — that's the whole point (keep-alive, no fd churn)."""
    if settings.mock_scrapers:
        yield _mock_session
        return
    yield _get(proxy, impersonate)
