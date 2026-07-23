"""
In-process event bus.

Admin endpoints do the work and then announce what happened. They do not know
who cares. That keeps `approve_listing()` about approving a listing, and lets
email, analytics, activity feeds and future Slack/push land as subscribers
without the endpoint growing another branch.

    await emit("listing.approved", {...})

Design decisions worth knowing:

* **Subscriber failures are isolated.** One handler raising must not stop the
  others, and must never fail the admin action that already committed. Every
  handler is awaited under `gather(return_exceptions=True)` and errors are
  logged, not raised.
* **Fire-and-forget from the caller's perspective.** `emit()` awaits its
  subscribers so ordering is deterministic in tests, but the endpoint treats a
  failed emit as a non-event.
* **In-process on purpose.** A queue (Celery/Redis) is the obvious next step
  once a subscriber needs retries or runs longer than a request. The call site
  doesn't change when that happens — only `emit()` does.

The audit log is deliberately NOT a subscriber: it is the record of what
happened and is written explicitly by the endpoint before the event fires.
Losing an email is survivable; silently losing an audit row is not.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Payload = dict[str, Any]
Handler = Callable[[str, Payload], Awaitable[None] | None]

_subscribers: dict[str, list[Handler]] = defaultdict(list)

# Wildcard subscribers see everything — useful for an activity feed or a
# firehose logger without listing every event name.
WILDCARD = "*"


def on(*event_names: str) -> Callable[[Handler], Handler]:
    """Register a handler for one or more events.

        @on("listing.approved", "listing.rejected")
        async def notify(name, payload): ...
    """

    def decorator(fn: Handler) -> Handler:
        for name in event_names:
            _subscribers[name].append(fn)
            logger.debug("[events] %s -> %s", name, getattr(fn, "__name__", fn))
        return fn

    return decorator


def subscribers_for(event_name: str) -> list[Handler]:
    return [*_subscribers.get(event_name, []), *_subscribers.get(WILDCARD, [])]


async def emit(event_name: str, payload: Payload | None = None) -> None:
    """
    Announce that something happened. Never raises.

    Handlers may be sync or async; sync ones run inline (they're expected to be
    trivial — anything slow should be async or queued).
    """
    handlers = subscribers_for(event_name)
    if not handlers:
        logger.debug("[events] %s — no subscribers", event_name)
        return

    data = payload or {}

    async def _run(handler: Handler) -> None:
        result = handler(event_name, data)
        if inspect.isawaitable(result):
            await result

    results = await asyncio.gather(*(_run(h) for h in handlers), return_exceptions=True)

    for handler, result in zip(handlers, results):
        if isinstance(result, Exception):
            # The action already committed. A broken subscriber is a bug to fix,
            # not a reason to tell the admin their approval failed.
            logger.exception(
                "[events] subscriber %s failed for %s: %s",
                getattr(handler, "__name__", handler), event_name, result,
            )


def reset_for_tests() -> None:
    """Clear every subscriber. Test hook only."""
    _subscribers.clear()
