"""
Sprint 2 — initial generation must persist to the durable event log EXACTLY
ONCE, at the critical "complete" emit. Every intermediate phase streams to Redis
only and buffers its patches; the single commit folds the buffer + the
authoritative sections into one append_events call. This is what collapsed the
per-phase read-modify-write storm (~5 append cycles → 1) that drove the Supabase
SSL bad_record_mac storm.
"""
import asyncio

import httpx
import pytest

import app.trip_state.generator as generator
from app.trip_state.patch import PatchOp


class _Recorder:
    """Captures append_events / publish_event calls without touching a DB or Redis."""

    def __init__(self):
        self.appends = []      # (actor, patches, phase)
        self.published = []    # phase
        self.flushed = []      # (phase, n_patches)

    def append_events(self, trip_id, actor, patches, phase=None, reasoning=None):
        self.appends.append((actor, list(patches), phase))
        return {}

    def publish(self, trip_id, event):
        self.published.append(event["phase"])

    def enqueue(self, trip_id, actor, patches, phase=None, reasoning=None):
        self.flushed.append((phase, len(patches)))


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    # run_db just calls the sync callable inline.
    async def _run_db(fn, *args):
        return fn(*args)
    monkeypatch.setattr(generator, "run_db", _run_db)
    monkeypatch.setattr(generator.store, "append_events", r.append_events)
    monkeypatch.setattr(generator, "publish_event", r.publish)
    monkeypatch.setattr(generator.flush, "enqueue", r.enqueue)
    return r


def _p(path, value):
    return PatchOp(op="set", path=path, value=value)


def test_intermediate_emits_do_not_persist_only_the_commit_does(rec):
    async def run():
        emit = generator._Emitter("trip-1")
        await emit("understanding", "…", [], 5)                              # empty
        await emit("weather_ready", "…", [_p("/weather", {"x": 1})], 50, actor="weather")
        await emit("transport_ready", "…", [_p("/transport/going", {"a": 1})], 45)
        await emit("itinerary_ready", "…", [_p("/itinerary/days", [])], 70)
        # Authoritative final commit re-sets whole sections.
        await emit("complete", "Trip ready",
                   [_p("/transport", {"final": True}), _p("/meta", {"status": "ready"})],
                   100, critical=True)

    asyncio.run(run())

    # Every phase streamed live.
    assert rec.published == [
        "understanding", "weather_ready", "transport_ready", "itinerary_ready", "complete",
    ]
    # Exactly one durable write — the commit.
    assert len(rec.appends) == 1
    actor, patches, phase = rec.appends[0]
    assert phase == "complete"
    # The one write folds the buffered intermediate patches THEN the authoritative
    # sections, in emit order (last-write-wins for overlapping paths downstream).
    paths = [p.path for p in patches]
    assert paths == ["/weather", "/transport/going", "/itinerary/days", "/transport", "/meta"]
    # Buffered-only path (/weather) survives; it is never re-set by the commit.
    assert "/weather" in paths


def test_commit_storm_falls_back_to_background_flush(rec, monkeypatch):
    def _boom(*a, **k):
        raise httpx.ConnectError("bad_record_mac storm")
    monkeypatch.setattr(generator.store, "append_events", _boom)

    async def run():
        emit = generator._Emitter("trip-2")
        await emit("transport_ready", "…", [_p("/transport/going", {"a": 1})], 45)
        await emit("complete", "Trip ready", [_p("/meta", {"status": "ready"})], 100, critical=True)

    asyncio.run(run())

    # The whole buffer (transport + meta) is handed to the flush queue exactly once.
    assert rec.flushed == [("complete", 2)]
    # Live stream still delivered both phases.
    assert rec.published == ["transport_ready", "complete"]
