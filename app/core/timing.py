"""
Request-scoped latency tracer.

Usage:
    trace = RequestTrace("trip_abc123")

    async with trace.track("gemini"):
        raw = await vertex_generate(prompt)

    print(trace.summary())
    # [trace] trip_abc123
    #   ✓ places_discovery: 1240ms
    #   ✓ gemini: 8420ms
    #   ✗ hotel_search: 320ms  error: CSRF failed
    #   Total: 9980ms | Errors: 1
"""
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass
class RequestTrace:
    request_id: str
    _calls: list = field(default_factory=list, init=False)
    token_input: int = 0
    token_output: int = 0

    @asynccontextmanager
    async def track(self, name: str):
        start = time.perf_counter()
        status = "ok"
        try:
            yield
        except Exception as e:
            status = f"error: {str(e)[:120]}"
            raise
        finally:
            ms = round((time.perf_counter() - start) * 1000)
            self._calls.append({"name": name, "ms": ms, "status": status})

    def summary(self) -> str:
        lines = [f"[trace] {self.request_id}"]
        for c in self._calls:
            icon = "✓" if c["status"] == "ok" else "✗"
            lines.append(f"  {icon} {c['name']}: {c['ms']}ms"
                         + (f"  ← {c['status']}" if c["status"] != "ok" else ""))
        if self.token_input or self.token_output:
            lines.append(f"  Gemini tokens: {self.token_input} in / {self.token_output} out")
        errors = sum(1 for c in self._calls if c["status"] != "ok")
        total = sum(c["ms"] for c in self._calls)
        lines.append(f"  Total: {total}ms | Errors: {errors}")
        return "\n".join(lines)
