"""
In-process scraper observability registry.

Two metric families, recorded at two natural chokepoints:

1. **Request-level** (recorded inside ``scraper_proxy.with_proxy_fallback`` — the
   single funnel every scraper HTTP call passes through):
   latency, outcome (success / empty / failure / timeout / blocked / captcha),
   and whether the rotating proxy was used. One sample per HTTP attempt, so a
   provider that makes an autocomplete + a search call records two requests.

2. **Search-level** (recorded by the aggregators in ``services/hotels.py`` and
   ``services/flights.py``): per-provider raw result count, and the merge-wide
   duplicate ratio after dedup. One sample per user search.

Everything is held in bounded, process-local memory (no external dependency):
counters + a rolling deque of latency samples per provider for percentiles.
``snapshot()`` derives the rates the user actually wants to see — failure %,
proxy %, captcha %, timeout %, avg result count, duplicate % — so optimisation
stops being guesswork. The shape is deliberately backend-agnostic: swapping this
for a Prometheus client later means re-implementing ``record_*`` only.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

# Bounded latency history per provider — enough for stable p50/p95, cheap to keep.
_LATENCY_WINDOW = 512

# Outcome taxonomy for a single scraper HTTP attempt.
OUTCOME_SUCCESS = "success"   # returned usable data
OUTCOME_EMPTY = "empty"       # 200 but nothing parseable (not an error)
OUTCOME_FAILURE = "failure"   # network / HTTP / parse error
OUTCOME_TIMEOUT = "timeout"   # read/connect timeout
OUTCOME_BLOCKED = "blocked"   # anti-bot block (ScraperBlocked, WAF non-200)
OUTCOME_CAPTCHA = "captcha"   # block that explicitly demanded a captcha

_OUTCOMES = (
    OUTCOME_SUCCESS, OUTCOME_EMPTY, OUTCOME_FAILURE,
    OUTCOME_TIMEOUT, OUTCOME_BLOCKED, OUTCOME_CAPTCHA,
)
# Anything that isn't a clean success counts against the failure rate.
_FAILURE_OUTCOMES = (OUTCOME_FAILURE, OUTCOME_TIMEOUT, OUTCOME_BLOCKED, OUTCOME_CAPTCHA)


@dataclass
class _ProviderStat:
    # request-level
    requests: int = 0
    proxy_requests: int = 0
    outcomes: dict[str, int] = field(default_factory=lambda: {o: 0 for o in _OUTCOMES})
    latency_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=_LATENCY_WINDOW))
    # search-level
    searches: int = 0
    raw_results: int = 0
    deduped_results: int = 0
    duplicates: int = 0


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _percentile(samples: list[float], q: float) -> float:
    if not samples:
        return 0.0
    s = sorted(samples)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[idx], 1)


class ScraperMetrics:
    """Thread-safe, process-local registry. One shared singleton (``METRICS``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, _ProviderStat] = {}
        self._started_at = time.time()

    def _stat(self, provider: str) -> _ProviderStat:
        st = self._providers.get(provider)
        if st is None:
            st = _ProviderStat()
            self._providers[provider] = st
        return st

    # ── recording ────────────────────────────────────────────────────────────
    def record_request(
        self, provider: str, *, latency_ms: float, outcome: str, proxy_used: bool
    ) -> None:
        """Record one scraper HTTP attempt. ``outcome`` ∈ the OUTCOME_* set."""
        if outcome not in _OUTCOMES:
            outcome = OUTCOME_FAILURE
        with self._lock:
            st = self._stat(provider)
            st.requests += 1
            st.outcomes[outcome] += 1
            st.latency_ms.append(max(0.0, latency_ms))
            if proxy_used:
                st.proxy_requests += 1

    def record_search(self, provider: str, *, raw_count: int, deduped_count: int) -> None:
        """Record one provider's contribution to a user search (post-normalisation)."""
        with self._lock:
            st = self._stat(provider)
            st.searches += 1
            st.raw_results += max(0, raw_count)
            st.deduped_results += max(0, deduped_count)
            st.duplicates += max(0, raw_count - deduped_count)

    # ── reporting ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            providers = {name: self._provider_view(st) for name, st in self._providers.items()}
        return {
            "uptime_sec": round(time.time() - self._started_at, 1),
            "providers": providers,
            "totals": self._totals(providers),
        }

    def reset(self) -> None:
        with self._lock:
            self._providers.clear()
            self._started_at = time.time()

    def _provider_view(self, st: _ProviderStat) -> dict:
        oc = st.outcomes
        failures = sum(oc[o] for o in _FAILURE_OUTCOMES)
        lat = list(st.latency_ms)
        avg_lat = round(sum(lat) / len(lat), 1) if lat else 0.0
        return {
            "requests": st.requests,
            "latency_ms": {
                "avg": avg_lat,
                "p50": _percentile(lat, 0.50),
                "p95": _percentile(lat, 0.95),
            },
            "failure_pct": _pct(failures, st.requests),
            "proxy_pct": _pct(st.proxy_requests, st.requests),
            "timeout_pct": _pct(oc[OUTCOME_TIMEOUT], st.requests),
            "blocked_pct": _pct(oc[OUTCOME_BLOCKED] + oc[OUTCOME_CAPTCHA], st.requests),
            "captcha_pct": _pct(oc[OUTCOME_CAPTCHA], st.requests),
            "empty_pct": _pct(oc[OUTCOME_EMPTY], st.requests),
            "outcomes": dict(oc),
            "searches": st.searches,
            "avg_result_count": round(st.deduped_results / st.searches, 1) if st.searches else 0.0,
            "duplicate_pct": _pct(st.duplicates, st.raw_results),
        }

    def _totals(self, providers: dict[str, dict]) -> dict:
        reqs = sum(p["requests"] for p in providers.values())
        searches = sum(p["searches"] for p in providers.values())
        return {
            "providers": len(providers),
            "requests": reqs,
            "searches": searches,
        }


# Process-wide singleton.
METRICS = ScraperMetrics()


def classify_exception(exc: BaseException) -> str:
    """
    Map a raised exception to an OUTCOME_*. Keeps the classification logic in one
    place so both the proxy funnel and any future caller agree on what a
    timeout/block/captcha looks like.
    """
    # Local import avoids a cycle (scraper_proxy imports this module).
    from app.services.scraper_proxy import ScraperBlocked

    text = f"{type(exc).__name__}: {exc}".lower()
    if any(k in text for k in ("captcha", "are you human", "px-captcha", "recaptcha", "hcaptcha")):
        return OUTCOME_CAPTCHA
    if isinstance(exc, ScraperBlocked) or any(
        k in text for k in ("403", "429", "blocked", "forbidden", "waf", "access denied", "too many requests")
    ):
        return OUTCOME_BLOCKED
    if isinstance(exc, TimeoutError) or any(
        k in text for k in ("timeout", "timed out", "read timed out", "connecttimeout")
    ):
        return OUTCOME_TIMEOUT
    return OUTCOME_FAILURE
