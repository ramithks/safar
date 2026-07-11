"""
Background flush queue for trip-state writes whose synchronous attempts failed.

The process occasionally hits multi-second windows where every outbound TLS
connection to Supabase dies with SSLV3_ALERT_BAD_RECORD_MAC (correlated with
curl_cffi scraper bursts; see docs in db/client.py). In-request retries can't
outlast the longer windows, so writes that still matter get queued here and
retried with backoff for up to a few minutes. The live Redis stream has already
delivered the patches to connected clients — this only makes the durable
snapshot converge.

Ordering: a single worker drains the queue FIFO, so multiple flushes for one
trip apply in submission order. While a trip has jobs in flight
(`has_pending`), writers must route follow-up writes for that trip through the
queue too — a synchronous write landing first would be overwritten by the
older queued patches.
"""
import asyncio
import logging

import httpx
import sentry_sdk

from app.db.client import run_db

from . import store
from .patch import PatchOp

logger = logging.getLogger(__name__)

_MAX_RETRY_SECONDS = 300
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None
# Jobs in flight per trip. Only touched from the event loop — no locking needed.
_pending_by_trip: dict[str, int] = {}


def has_pending(trip_id: str) -> bool:
    return _pending_by_trip.get(trip_id, 0) > 0


def enqueue(
    trip_id: str,
    actor: str,
    patches: list[PatchOp],
    phase: str | None = None,
    reasoning: str | None = None,
) -> None:
    """Queue patches for background persistence. Must be called on the loop."""
    global _queue, _worker
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker is None or _worker.done():
        _worker = asyncio.get_running_loop().create_task(_drain())
    _pending_by_trip[trip_id] = _pending_by_trip.get(trip_id, 0) + 1
    _queue.put_nowait((trip_id, actor, patches, phase, reasoning))
    logger.warning(
        "[trip-state] queued %d patches (%s) for background flush of %s",
        len(patches), phase, trip_id,
    )


async def _drain() -> None:
    while True:
        trip_id, actor, patches, phase, reasoning = await _queue.get()
        try:
            await _flush_one(trip_id, actor, patches, phase, reasoning)
        finally:
            remaining = _pending_by_trip.get(trip_id, 1) - 1
            if remaining > 0:
                _pending_by_trip[trip_id] = remaining
            else:
                _pending_by_trip.pop(trip_id, None)


async def _flush_one(
    trip_id: str,
    actor: str,
    patches: list[PatchOp],
    phase: str | None,
    reasoning: str | None,
) -> None:
    deadline = asyncio.get_running_loop().time() + _MAX_RETRY_SECONDS
    delay = 2.0
    while True:
        try:
            await run_db(
                lambda: store.append_events(
                    trip_id, actor, patches, phase=phase, reasoning=reasoning
                )
            )
            logger.info("[trip-state] background flush landed for %s (%s)", trip_id, phase)
            return
        except httpx.TransportError as exc:
            if asyncio.get_running_loop().time() >= deadline:
                logger.error(
                    "[trip-state] background flush gave up for %s (%s) after %ss: %s",
                    trip_id, phase, _MAX_RETRY_SECONDS, exc,
                )
                sentry_sdk.capture_exception(exc)
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        except Exception as exc:  # noqa: BLE001 — a poison job must not kill the worker
            logger.error(
                "[trip-state] background flush failed permanently for %s (%s): %s",
                trip_id, phase, exc, exc_info=True,
            )
            sentry_sdk.capture_exception(exc)
            return
