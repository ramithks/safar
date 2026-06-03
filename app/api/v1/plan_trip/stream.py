"""
SSE streaming for plan-trip.

Client receives progress events as the pipeline runs, then the full itinerary
in a final "complete" event. Renders live instead of waiting 20s for a JSON blob.
"""
import asyncio
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from app.core.sse import sse_progress, sse_event, sse_error
from app.core.timing import RequestTrace

from .models import PlanTripRequest
from .orchestrator import run_plan_trip


async def plan_trip_stream_response(body: PlanTripRequest, user) -> StreamingResponse:
    return StreamingResponse(
        _generate(body, user),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _generate(body: PlanTripRequest, user) -> AsyncGenerator[str, None]:
    trace = RequestTrace(f"trip_{user.id[:8]}")
    try:
        yield sse_progress("discovering_places", 8,
                           f"Finding the best places in {body.destination}...")

        # Run the full orchestration. The trace records per-stage timings.
        # We emit synthetic progress events around the two slowest stages
        # so the client never waits more than ~2s without feedback.

        gemini_done = asyncio.Event()

        async def _run():
            return await run_plan_trip(body, user.id, trace=trace)

        task = asyncio.create_task(_run())

        # Emit intermediate progress while waiting for Gemini
        yield sse_progress("generating_itinerary", 22,
                           "AI is crafting your personalised trip...")
        while not task.done():
            await asyncio.sleep(2)
            if not task.done():
                yield sse_progress("generating_itinerary", 45,
                                   "Building day-by-day activities...")
                break

        while not task.done():
            await asyncio.sleep(2)
            if not task.done():
                yield sse_progress("enriching", 68,
                                   "Enriching with real-world data...")
                break

        while not task.done():
            await asyncio.sleep(2)
            if not task.done():
                yield sse_progress("enriching", 82, "Fetching hotels and routes...")
                break

        result = await task

        yield sse_progress("finalizing", 96, "Polishing your itinerary...")
        yield sse_event("complete", result)

    except Exception as e:
        print(f"[stream] plan_trip_stream failed: {e}")
        yield sse_error("Trip generation failed. Please try again.")
