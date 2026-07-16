"""
Trip State API — persistent trips, streaming patch events, copilot edits.

Routes are RELATIVE; the same router is mounted twice:
  /api/v1/trip-state/*   → original mount (kept for existing clients)
  /api/v2/plan/*         → the v2 planner surface the website uses
(/api/v1/plan stays the classic one-shot planner the mobile app runs on.)

  POST  {base}              → create trip + start generation, returns trip_id
  GET   {base}/{id}         → current TripState snapshot
  GET   {base}/{id}/stream  → SSE: replay + live patch events
  POST  {base}/{id}/edit    → copilot command → patches
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
from app.trip_state.patch import PatchOp, apply_patches

logger = logging.getLogger(__name__)
router = APIRouter()


class EditAction(BaseModel):
    """Structured UI command that bypasses the LLM agent — deterministic,
    instant edits the user drives directly from the workspace.

      {"type": "select_transport_option", "direction", "mode", "index"}
      {"type": "select_leg_option", "leg_id", "mode", "index", "hop_index"?}
      {"type": "select_hotel", "hotel_index"}
      {"type": "select_stop_hotel", "stop_id", "hotel_index"}
    """
    type: str
    direction: str | None = None
    mode: str | None = None
    index: int | None = None
    hotel_index: int | None = None
    leg_id: str | None = None
    hop_index: int | None = None
    stop_id: str | None = None


class EditRequest(BaseModel):
    message: str = ""
    action: EditAction | None = None


def _authorize_read(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id and row["visibility"] not in ("link", "public"):
        raise HTTPException(status_code=404, detail="Trip not found")


def _authorize_write(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your trip")


def _overlay_stream_events(trip_id: str, state: dict) -> dict:
    """Read-repair: the DB snapshot can run BEHIND the live Redis stream while
    persistence rides out a TLS storm (see flush.py — writes are queued and
    retried for minutes). Serving or editing the stale snapshot then corrupts
    the trip: edits re-derive the graph from a state missing earlier edits.
    Every streamed patch is an idempotent `set` in emit order, so replaying the
    whole stream over the snapshot always converges to the live trip."""
    try:
        events = read_events(trip_id, 0)
    except Exception as exc:  # noqa: BLE001 — repair is best-effort, never 500 a read
        logger.warning("[trip-state] stream overlay unavailable for %s: %s", trip_id, exc)
        return state
    for ev in events:
        try:
            patches = [PatchOp(**p) for p in (ev.get("patches") or [])]
            if patches:
                apply_patches(state, patches, actor="supervisor")
        except Exception as exc:  # noqa: BLE001 — one bad event must not drop the rest
            logger.warning("[trip-state] skipped stream event during overlay of %s: %s", trip_id, exc)
    return state


async def _get_row(trip_id: str) -> dict:
    row = await run_db(store.get_trip_state, trip_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    row["state"] = _overlay_stream_events(trip_id, row["state"] or {})
    meta = (row["state"].get("meta") or {})
    if meta.get("status"):
        row["status"] = meta["status"]
    return row


@router.post("")
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


@router.get("/{trip_id}")
async def get_trip(trip_id: str, user=Depends(require_user)):
    from app.trip_state.graph import projected_state
    from app.trip_state.retrieval import hydrate_graph_option_raws

    row = await _get_row(trip_id)
    _authorize_read(row, str(user.id))
    # Regenerate the compatibility projections (transport/itinerary/costs and the
    # DERIVED recommendation) from the canonical graph on every read — nothing
    # derived is served from a stored snapshot. No-op for graph-less legacy trips.
    # A thin persisted graph (post-edit) first gets its option raws back from the
    # search store so the projection can re-emit the full transport buckets.
    state = row["state"] or {}
    await hydrate_graph_option_raws(trip_id, state)
    return {
        "trip_id": row["id"],
        "status": row["status"],
        "visibility": row["visibility"],
        "last_seq": row["last_seq"],
        "state": projected_state(state),
    }


@router.get("/{trip_id}/chat")
async def get_trip_chat(trip_id: str, user=Depends(require_user), limit: int = 100):
    """Durable copilot chat transcript — the frontend loads this on mount to
    rehydrate the thread after a refresh."""
    row = await _get_row(trip_id)
    _authorize_read(row, str(user.id))
    messages = await run_db(store.get_chat, trip_id, min(max(limit, 1), 300))
    return {"trip_id": trip_id, "messages": messages}


@router.get("/{trip_id}/stream")
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


@router.post("/{trip_id}/edit")
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


@router.post("/{trip_id}/edit/stream")
@limiter.limit("60/hour")
async def edit_trip_stream(request: Request, trip_id: str, body: EditRequest, user=Depends(require_user)):
    """
    Streaming copilot edit — same agent/handlers as /edit, but progress lands
    live. The agent may chain many tool calls for one message; each completed
    step is emitted as a `step` event (label + its patches) THE MOMENT it lands,
    so the board fills in while the agent keeps working. Then `token` chunks
    type out the final reply, and a single `done` event carries the
    authoritative result. Persistence (chat + patches) happens inside
    apply_edit, once.
    """
    row = await _get_row(trip_id)
    _authorize_write(row, str(user.id))
    message = body.message.strip()[:500]
    if not message and body.action is None:
        raise HTTPException(status_code=400, detail="Say what you'd like to change.")
    action = body.action.model_dump() if body.action else None
    user_id = str(user.id)
    state = row["state"]

    async def event_source():
        steps: asyncio.Queue = asyncio.Queue()
        # apply_edit awaits between tool calls on this same loop, so a plain
        # put_nowait from the callback is safe (no cross-thread hops).
        task = asyncio.create_task(apply_edit(
            trip_id, message, state, action=action, user_id=user_id,
            on_step=lambda step: steps.put_nowait(step),
        ))

        def _step_event(step: dict) -> str:
            return sse_event("step", {
                "label": step.get("label") or "",
                "tool": step.get("tool"),
                "patches": [p.model_dump(mode="json") for p in (step.get("patches") or [])],
            })

        # Relay steps live until the run finishes, then drain any stragglers.
        while not task.done():
            getter = asyncio.ensure_future(steps.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                yield _step_event(getter.result())
            else:
                getter.cancel()
        while not steps.empty():
            yield _step_event(steps.get_nowait())

        try:
            result = task.result()
        except Exception as exc:  # noqa: BLE001 — stream must terminate cleanly
            logger.error("[trip-state] stream edit failed for %s: %s", trip_id, exc, exc_info=True)
            sentry_sdk.capture_exception(exc)
            yield sse_event("error", {"detail": "Edit failed. Please try again."})
            return

        reply = result.get("reply") or ""
        # Type the reply out word-by-word for a live feel. (Latency to first
        # token is the agent run; the steps above are the visible progress.)
        words = reply.split(" ")
        for i, word in enumerate(words):
            yield sse_event("token", {"text": word if i == 0 else " " + word})
            await asyncio.sleep(0.02)

        yield sse_event("done", {
            "reply": reply,
            "intent": result.get("intent"),
            "applied": result.get("applied"),
            "patches": result.get("patches") or [],
            "clarify_options": result.get("clarify_options") or [],
            "steps": result.get("steps") or [],
            "cards": result.get("cards") or [],
        })

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
