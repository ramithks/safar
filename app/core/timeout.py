"""
Global request-timeout middleware.

Per-endpoint timeouts exist (e.g. /plan caps its own upstreams), but there is no
single backstop: a handler that wedges *before* producing a response can pin a
worker connection open indefinitely. This middleware caps time-to-first-byte and
returns 504 past the ceiling.

Streaming endpoints (SSE) are unaffected: BaseHTTPMiddleware resolves
`call_next` as soon as the response starts, so only the setup phase is timed —
the long-lived body stream runs outside the wait_for.
"""
import asyncio
import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class TimeoutMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, timeout_seconds: float):
        super().__init__(app)
        self.timeout = timeout_seconds

    async def dispatch(self, request: Request, call_next):
        try:
            return await asyncio.wait_for(call_next(request), timeout=self.timeout)
        except asyncio.TimeoutError:
            logger.error(
                "[timeout] %s %s exceeded %.0fs ceiling",
                request.method, request.url.path, self.timeout,
            )
            return JSONResponse(
                status_code=504,
                content={
                    "error": {
                        "code": "REQUEST_TIMEOUT",
                        "message": "The request took too long to process.",
                        "retryable": True,
                    }
                },
            )
