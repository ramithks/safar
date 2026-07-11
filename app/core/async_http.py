"""
Event-loop-scoped httpx.AsyncClient registry.

Celery executes each task under a fresh `asyncio.run()` loop that is closed when
the task returns. A module-level `httpx.AsyncClient` created once keeps its
keep-alive connections (and internal anyio locks) bound to that now-closed loop,
so the next task's first request raises "Event loop is closed" / "bound to a
different event loop".

`loop_client()` hands each running loop its own client and evicts clients whose
loops are gone, so the registry can't grow unbounded across tasks. Under uvicorn
(one long-lived loop per worker) this is effectively a lazy singleton.
"""
import asyncio
from typing import Callable

import httpx


def loop_client(
    registry: dict[int, httpx.AsyncClient],
    factory: Callable[[], httpx.AsyncClient],
) -> httpx.AsyncClient:
    """Return an AsyncClient bound to the running loop, building it on first use."""
    key = id(asyncio.get_running_loop())
    client = registry.get(key)
    if client is None:
        # Drop clients for loops that have ended (one short-lived loop per Celery
        # task) before adding the new one.
        for stale in [k for k in registry if k != key]:
            registry.pop(stale, None)
        client = factory()
        registry[key] = client
    return client
