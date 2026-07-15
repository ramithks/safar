"""
Global trip-generation queue — the top-level load-shedder.

A burst of users must not launch 100 concurrent generations: each one fans out
to Vertex (tight quota), the scrapers (proxy bandwidth + anti-bot), and the TLS
stack all at once, and nothing here sheds the excess — so they all degrade
together. This caps concurrent generations PER WORKER PROCESS at
`settings.max_concurrent_generations`; the surplus WAITS on the semaphore
(FIFO-ish) rather than failing. The trip row already exists (status "planning"),
and generation emits a "queued" event so the frontend can show a spinner with a
position instead of a stall.

Loop-scoped, like curl_pool / circuit_breaker: Celery runs tasks under fresh
`asyncio.run()` loops, so a semaphore bound to a dead loop would wedge. Under
uvicorn there is one long-lived loop, so it's effectively a fixed gate.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from app.config import settings

logger = logging.getLogger(__name__)

_sem: tuple[int, asyncio.Semaphore] | None = None
_capacity = max(1, settings.max_concurrent_generations)
_in_flight = 0
_waiting = 0
_peak_in_flight = 0
_total_admitted = 0
_total_queued = 0   # generations that had to wait for a slot


def _semaphore() -> asyncio.Semaphore:
    global _sem
    loop_id = id(asyncio.get_running_loop())
    if _sem is None or _sem[0] != loop_id:
        _sem = (loop_id, asyncio.Semaphore(_capacity))
    return _sem[1]


def has_free_slot() -> bool:
    """True if a generation can start immediately (no queue wait)."""
    return _in_flight < _capacity


@asynccontextmanager
async def generation_slot(trip_id: str):
    """Acquire a generation slot, queueing if the process is at capacity. Yields
    a bool: True if the caller had to wait (so it can emit a 'queued' event)."""
    global _in_flight, _waiting, _peak_in_flight, _total_admitted, _total_queued
    sem = _semaphore()
    queued = _in_flight >= _capacity
    if queued:
        _waiting += 1
        _total_queued += 1
        logger.info("[gen-queue] %s waiting for a slot (%d in-flight, %d waiting)",
                    trip_id, _in_flight, _waiting)
    try:
        await sem.acquire()
    finally:
        if queued:
            _waiting -= 1
    _in_flight += 1
    _total_admitted += 1
    _peak_in_flight = max(_peak_in_flight, _in_flight)
    try:
        yield queued
    finally:
        _in_flight -= 1
        sem.release()


def stats() -> dict:
    return {
        "capacity": _capacity,
        "in_flight": _in_flight,
        "waiting": _waiting,
        "peak_in_flight": _peak_in_flight,
        "total_admitted": _total_admitted,
        "total_queued": _total_queued,
    }
