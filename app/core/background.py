"""
Safe fire-and-forget background tasks.

`asyncio.create_task()` only keeps a *weak* reference to the task via the event
loop. If the caller doesn't hold a strong reference, the task can be garbage
collected mid-flight — silently cancelled before it finishes. For DB writes
(trip persistence, journey snapshots) that means data loss with no error.

`spawn_background()` holds a strong reference until the task completes and routes
any exception to the logs + Sentry instead of the asyncio "Task exception was
never retrieved" warning that nobody sees.
"""
import asyncio
import logging

import sentry_sdk

logger = logging.getLogger(__name__)

# Strong references to in-flight tasks so the GC can't collect them early.
_tasks: set[asyncio.Task] = set()


def spawn_background(coro, *, name: str) -> asyncio.Task:
    """Schedule `coro` on the running loop, keep it alive, and surface failures.

    Use for durable side effects (DB writes) that must not be silently dropped.
    Must be called from within a running event loop.
    """
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_on_done)
    return task


def _on_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        logger.warning("[background] task %s was cancelled", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "[background] task %s failed: %s", task.get_name(), exc, exc_info=exc
        )
        sentry_sdk.capture_exception(exc)
