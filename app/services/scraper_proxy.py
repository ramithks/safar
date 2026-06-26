"""
Shared proxy fallback for the travel-aggregator scrapers
(booking, easemytrip, confirmtkt, redbus, flixbus).

These scrapers call anti-bot-protected JSON APIs from the server's datacenter
IP. Each request is first attempted directly; if it fails — HTTP 4xx/5xx block,
connection reset, timeout, or a soft block (see ScraperBlocked) — the request is
retried ONCE through the rotating proxy. The proxy URL is shared with the
Instagram downloader (INSTAGRAM_PROXY_URL), so a single Webshare endpoint covers
everything. Direct-first keeps proxy bandwidth near zero until the datacenter IP
is actually blocked.
"""
import logging
import time
from typing import Awaitable, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def get_proxy_url() -> Optional[str]:
    """Proxy URL used for scraper fallback — shared with the IG downloader."""
    from app.config import settings
    return settings.instagram_proxy_url


class ScraperBlocked(Exception):
    """
    Raised inside a request closure when a non-exception HTTP response still
    signals a block — e.g. Booking.com returns a non-200 body without raising.
    Triggers the proxy retry just like a network/HTTP error would.
    """


async def with_proxy_fallback(
    label: str,
    do_request: Callable[[Optional[str]], Awaitable[T]],
) -> T:
    """
    Run ``do_request(None)``; on any failure, retry once with the proxy.

    ``do_request`` receives the proxy URL (or None), performs the HTTP call, and
    returns its parsed result — raising ScraperBlocked or any network/HTTP error
    on a condition that warrants a proxy retry. If no proxy is configured the
    direct error propagates unchanged (callers handle it as before).
    """
    # Lazy import keeps this module importable without the metrics registry and
    # avoids an import cycle (metrics.classify_exception imports ScraperBlocked).
    from app.core.metrics import METRICS, classify_exception, OUTCOME_SUCCESS

    proxy = get_proxy_url()
    started = time.monotonic()
    try:
        result = await do_request(None)
    except Exception as direct_exc:
        # Record the failed direct attempt, then decide whether to retry.
        METRICS.record_request(
            label,
            latency_ms=(time.monotonic() - started) * 1000,
            outcome=classify_exception(direct_exc),
            proxy_used=False,
        )
        if not proxy:
            raise
        logger.info("[%s] direct request failed (%s) — retrying via proxy", label, direct_exc)
        retry_started = time.monotonic()
        try:
            result = await do_request(proxy)
        except Exception as proxy_exc:
            METRICS.record_request(
                label,
                latency_ms=(time.monotonic() - retry_started) * 1000,
                outcome=classify_exception(proxy_exc),
                proxy_used=True,
            )
            raise
        METRICS.record_request(
            label,
            latency_ms=(time.monotonic() - retry_started) * 1000,
            outcome=OUTCOME_SUCCESS,
            proxy_used=True,
        )
        return result

    METRICS.record_request(
        label,
        latency_ms=(time.monotonic() - started) * 1000,
        outcome=OUTCOME_SUCCESS,
        proxy_used=False,
    )
    return result
