"""
Per-provider circuit breakers.

States: closed → open (after N failures) → half-open (after timeout) → closed

Module-level singletons are shared across the process lifetime so failure
counts persist across requests (the whole point of a circuit breaker).
"""
import time
from typing import Any, Callable


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, recovery_timeout: float = 60.0):
        self.name = name
        self._failures = 0
        self._threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._last_failure: float = 0.0
        self._state = "closed"  # closed | open | half-open

    @property
    def state(self) -> str:
        if self._state == "open":
            if time.monotonic() - self._last_failure > self._recovery_timeout:
                self._state = "half-open"
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    async def call(self, coro_fn: Callable, *args, fallback: Any = None, **kwargs) -> Any:
        if self.state == "open":
            print(f"[circuit] {self.name} OPEN — skipping call, returning fallback")
            return fallback

        try:
            result = await coro_fn(*args, **kwargs)
            if self._state == "half-open":
                print(f"[circuit] {self.name} RECOVERED → closed")
                self._state = "closed"
                self._failures = 0
            return result
        except Exception as e:
            self._failures += 1
            self._last_failure = time.monotonic()
            if self._failures >= self._threshold:
                prev = self._state
                self._state = "open"
                if prev != "open":
                    print(f"[circuit] {self.name} OPENED after {self._failures} failures: {e}")
            else:
                print(f"[circuit] {self.name} failure {self._failures}/{self._threshold}: {e}")
            return fallback


# ── Module-level singletons — one per external provider ──────────────────────
breaker_gemini   = CircuitBreaker("Gemini",     failure_threshold=2, recovery_timeout=30.0)
breaker_places   = CircuitBreaker("Places",     failure_threshold=3, recovery_timeout=60.0)
breaker_booking  = CircuitBreaker("Booking",    failure_threshold=2, recovery_timeout=120.0)
breaker_expedia  = CircuitBreaker("Expedia",    failure_threshold=2, recovery_timeout=120.0)
breaker_emt      = CircuitBreaker("EaseMyTrip", failure_threshold=3, recovery_timeout=60.0)
breaker_redbus   = CircuitBreaker("RedBus",     failure_threshold=3, recovery_timeout=60.0)
breaker_flixbus  = CircuitBreaker("FlixBus",    failure_threshold=3, recovery_timeout=60.0)
