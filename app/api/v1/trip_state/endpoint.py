"""
Trip State V2 API — persistent trips, streaming patch events, copilot edits.

  POST /trip-state                → create trip + start generation, returns trip_id
  GET  /trip-state/{id}           → current TripState snapshot
  GET  /trip-state/{id}/stream    → SSE: replay + live patch events
  POST /trip-state/{id}/edit      → copilot command → patches
"""
import asyncio
import json
import logging

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.background import spawn_background
from app.core.limiter import limiter
from app.core.sse import sse_event
from app.db.client import run_db
from app.deps.auth import require_user
from app.api.v1.plan_trip.models import PlanRequest
from app.trip_state import empty_trip_state
from app.trip_state import store
from app.trip_state.copilot import apply_edit
from app.trip_state.generator import read_events, run_generation

logger = logging.getLogger(__name__)
router = APIRouter()


class EditAction(BaseModel):
    """Structured UI command that bypasses the LLM intent router."""
    type: str
    direction: str | None = None
    mode: str | None = None
    index: int | None = None


class EditRequest(BaseModel):
    message: str = ""
    action: EditAction | None = None


def _authorize_read(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id and row["visibility"] not in ("link", "public"):
        raise HTTPException(status_code=404, detail="Trip not found")


def _authorize_write(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your trip")


async def _get_row(trip_id: str) -> dict:
    row = await run_db(store.get_trip_state, trip_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    return row


@router.post("/trip-state")
@limiter.limit("10/hour")
async def create_trip(request: Request, body: PlanRequest, user=Depends(require_user)):
    """Create a persistent trip and kick off streaming generation."""
    state = empty_trip_state({
        "origin": body.origin,
        "destination": body.destination,
        "departure_date": body.departure_date,
        "days": body.days,
        "stay_nights": max(1, body.days - 1),
        "travelers": body.travelers,
        "budget_tier": body.budget,
        "interests": body.interests,
    })
    trip_id = await run_db(store.create_trip_state, str(user.id), state)
    spawn_background(run_generation(trip_id, body, str(user.id)), name=f"trip_gen:{trip_id}")
    logger.info("[trip-state] %s → %s queued as %s", body.origin, body.destination, trip_id)
    return {"trip_id": trip_id, "status": "planning"}


@router.get("/trip-state/{trip_id}")
async def get_trip(trip_id: str, user=Depends(require_user)):
    row = await _get_row(trip_id)
    _authorize_read(row, str(user.id))
    return {
        "trip_id": row["id"],
        "status": row["status"],
        "visibility": row["visibility"],
        "last_seq": row["last_seq"],
        "state": row["state"],
    }


@router.get("/trip-state/{trip_id}/stream")
async def stream_trip(trip_id: str, user=Depends(require_user), after: int = 0):
    """
    SSE stream of patch events. Replays the Redis buffer from `after`, then
    tails for new events until a terminal phase (complete/error) or timeout.
    """
    row = await _get_row(trip_id)
    _authorize_read(row, str(user.id))

    async def event_source():
        loop = asyncio.get_event_loop()
        cursor = after
        idle_budget = 600  # seconds without terminal event before giving up

        # If Redis was flushed (restart), replay from the durable event log.
        buffered = await loop.run_in_executor(None, read_events, trip_id, cursor)
        if not buffered and cursor == 0 and row["last_seq"] > 0:
            db_events = await run_db(store.get_events, trip_id, 0)
            for ev in db_events:
                yield sse_event("patch", {
                    "phase": ev.get("phase") or "replay",
                    "label": ev.get("phase") or "",
                    "progress": 100,
                    "patches": ev.get("patches") or [],
                    "reasoning": ev.get("reasoning"),
                })
            if row["status"] in ("ready", "error"):
                yield sse_event("done", {"status": row["status"]})
                return

        while idle_budget > 0:
            events = await loop.run_in_executor(None, read_events, trip_id, cursor)
            for ev in events:
                cursor += 1
                yield sse_event("patch", ev)
                if ev.get("phase") in ("complete", "error"):
                    yield sse_event("done", {"status": "ready" if ev["phase"] == "complete" else "error"})
                    return
            await asyncio.sleep(0.3)
            idle_budget -= 0.3

        yield sse_event("done", {"status": "timeout"})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/trip-state/{trip_id}/edit")
@limiter.limit("60/hour")
async def edit_trip(request: Request, trip_id: str, body: EditRequest, user=Depends(require_user)):
    """Copilot command → intent router → patches over existing state."""
    row = await _get_row(trip_id)
    _authorize_write(row, str(user.id))
    message = body.message.strip()[:500]
    if not message and body.action is None:
        raise HTTPException(status_code=400, detail="Say what you'd like to change.")
    try:
        result = await apply_edit(
            trip_id, message, row["state"],
            action=body.action.model_dump() if body.action else None,
            user_id=str(user.id),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.error("[trip-state] edit failed for %s: %s", trip_id, exc, exc_info=True)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(status_code=500, detail="Edit failed. Please try again.") from exc
