"""Flight search caches genuinely dead routes (no air service) so a single trip
doesn't re-scrape them across outbound + return + mirror passes — but never
caches transient outages (breaker open / timeout / provider error)."""
import asyncio
import time

import pytest

import app.services.flights as F
from app.services.flights import FlightService


@pytest.fixture(autouse=True)
def _clear_cache():
    F._no_service.clear()
    yield
    F._no_service.clear()


def test_predicate_and_expiry():
    F._mark_no_service("BAO", "KUU")
    assert F._is_no_service("BAO", "KUU")
    assert not F._is_no_service("DEL", "BOM")
    # Expired entry is treated as live again.
    F._no_service["X>Y"] = time.monotonic() - 1
    assert not F._is_no_service("X", "Y")


def test_dead_route_short_circuits_second_search(monkeypatch):
    calls = {"n": 0}

    async def _empty(*a, **k):
        calls["n"] += 1
        return []

    # All three providers respond (empty), breakers closed → route cached.
    monkeypatch.setattr(F, "booking_search_flights", _empty)
    monkeypatch.setattr(F, "emt_search_flights", _empty)
    monkeypatch.setattr(F, "gotogate_search_flights", _empty)
    for b in (F.breaker_booking, F.breaker_emt, F.breaker_gotogate):
        monkeypatch.setattr(b, "_state", "closed")

    svc = FlightService()
    r1 = asyncio.run(svc.search("X", "Y", "2026-07-19", from_iata="BAO", to_iata="KUU"))
    assert r1["flights"] == []
    first_calls = calls["n"]
    assert first_calls == 3  # all providers hit once

    # Second search (e.g. the return leg / mirror pass) is served from cache.
    r2 = asyncio.run(svc.search("X", "Y", "2026-07-22", from_iata="BAO", to_iata="KUU"))
    assert r2.get("no_service") is True
    assert calls["n"] == first_calls  # no additional provider calls


def test_open_breaker_does_not_cache(monkeypatch):
    async def _empty(*a, **k):
        return []

    monkeypatch.setattr(F, "booking_search_flights", _empty)
    monkeypatch.setattr(F, "emt_search_flights", _empty)
    monkeypatch.setattr(F, "gotogate_search_flights", _empty)
    # One breaker open → empties are a transient outage, must NOT be cached.
    monkeypatch.setattr(F.breaker_booking, "_state", "open")
    monkeypatch.setattr(F.breaker_booking, "_last_failure", time.monotonic())
    monkeypatch.setattr(F.breaker_emt, "_state", "closed")
    monkeypatch.setattr(F.breaker_gotogate, "_state", "closed")

    svc = FlightService()
    asyncio.run(svc.search("X", "Y", "2026-07-19", from_iata="BAO", to_iata="KUU"))
    assert not F._is_no_service("BAO", "KUU")
