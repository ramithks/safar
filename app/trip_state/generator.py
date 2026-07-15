"""
Streaming trip generation — runs the supervisor and turns agent completions
into TripState patch events.

Event flow:
  emit → persist to trip_state_events (snapshot updated) → publish to a Redis
  list the SSE endpoint tails. Initial generation is just patches applied to
  an empty TripState — the same pipeline copilot edits use.

Event shape (what the frontend receives):
  {seq, phase, label, progress, patches: [PatchOp], reasoning?}
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx
import redis as redis_lib
import sentry_sdk

from app.db.client import run_db

from app.api.v1.plan_trip.models import PlanRequest
from app.api.v1.plan_trip.plan import _shape_journey
from app.config import settings
from app.core.background import spawn_background

from . import batches, flush, store
from .models import trip_state_from_merged
from .patch import PatchOp

logger = logging.getLogger(__name__)

# 24h: the stream doubles as the read-repair source when DB persistence rides
# out a TLS storm (endpoint._overlay_stream_events) — it must outlive any storm
# plus the user coming back later the same day.
STREAM_TTL_SECONDS = 86400
_redis: redis_lib.Redis | None = None


def _get_redis() -> redis_lib.Redis:
    global _redis
    if _redis is None:
        _redis = redis_lib.from_url(settings.redis_url, decode_responses=True)
    return _redis


def _stream_key(trip_id: str) -> str:
    return f"tripstate:{trip_id}:stream"


def publish_event(trip_id: str, event: dict) -> None:
    r = _get_redis()
    key = _stream_key(trip_id)
    r.rpush(key, json.dumps(event, default=str))
    r.expire(key, STREAM_TTL_SECONDS)


def read_events(trip_id: str, start: int = 0) -> list[dict]:
    r = _get_redis()
    raw = r.lrange(_stream_key(trip_id), start, -1)
    return [json.loads(x) for x in raw]


class _Emitter:
    """Persists patches to the event log, then publishes to the live stream.

    Persistence failures must not kill the generation: the live stream (Redis)
    still gets the event, and the unpersisted patches are carried forward and
    folded into the next successful append, so the stored snapshot converges.
    A `critical=True` emit (the final authoritative one — there is no "next
    emit" after it) hands its patches to the background flush queue instead,
    which retries for minutes.
    """

    def __init__(self, trip_id: str):
        self.trip_id = trip_id
        # Weather runs in parallel with supervisor phases; append_events is a
        # read-modify-write on the snapshot, so emits must never interleave.
        self._lock = asyncio.Lock()
        # Patches whose persist failed, ordered oldest-first. Folded into the
        # next append (their per-event actor/phase granularity is lost — the
        # snapshot result is identical since later sets of a path win).
        self._pending: list[PatchOp] = []

    async def __call__(
        self,
        phase: str,
        label: str,
        patches: list[PatchOp],
        progress: int,
        reasoning: str | None = None,
        actor: str = "supervisor",
        critical: bool = False,
    ) -> None:
        async with self._lock:
            await self._emit(phase, label, patches, progress, reasoning, actor, critical)

    async def _emit(
        self,
        phase: str,
        label: str,
        patches: list[PatchOp],
        progress: int,
        reasoning: str | None,
        actor: str,
        critical: bool,
    ) -> None:
        to_persist = self._pending + patches
        if to_persist:
            try:
                await run_db(
                    lambda: store.append_events(
                        self.trip_id, actor, to_persist, phase=phase, reasoning=reasoning
                    )
                )
                self._pending = []
            except httpx.TransportError as exc:
                if critical:
                    flush.enqueue(
                        self.trip_id, actor, to_persist, phase=phase, reasoning=reasoning
                    )
                    self._pending = []
                else:
                    self._pending = to_persist
                    logger.warning(
                        "[trip-state] %s persisting %s for %s — deferring %d patches to next emit",
                        type(exc).__name__, phase, self.trip_id, len(to_persist),
                    )
        publish_event(self.trip_id, {
            "phase": phase,
            "label": label,
            "progress": progress,
            "patches": [p.model_dump(mode="json") for p in patches],
            "reasoning": reasoning,
        })


def _section_patches(state_dict: dict, sections: list[str]) -> list[PatchOp]:
    return [PatchOp(op="set", path=f"/{s}", value=state_dict[s]) for s in sections]


def _reco_ctx(body: PlanRequest) -> dict:
    """Minimal traveller context for the reco ranker (persona is derived from it)."""
    return {"budget": body.budget, "intent": {"interests": list(body.interests or [])}}


def _return_date(body: PlanRequest) -> str | None:
    """The return leg departs on the last day of the trip. PlanRequest carries no
    return_date field, so derive it the same way the supervisor does
    (departure_date + days) instead of reading a non-existent attribute."""
    if not body.departure_date:
        return None
    try:
        dep = datetime.fromisoformat(body.departure_date)
        return (dep + timedelta(days=body.days)).strftime("%Y-%m-%d")
    except ValueError:
        return None


async def _save_and_score_transport(trip_id, scope_id, origin, destination,
                                    depart_date, shaped, ctx) -> None:
    from app.reco import index
    batch_id = await batches.save_transport_batch(
        trip_id, scope_id=scope_id, origin=origin, destination=destination,
        depart_date=depart_date, shaped=shaped)
    if batch_id:
        await index.score_batch(trip_id, batch_id, scope_id=scope_id,
                                kind="transport", ctx=ctx)


async def _save_and_score_hotels(trip_id, scope_id, destination, depart_date,
                                 hotels_block, ctx) -> None:
    from app.reco import index
    batch_id = await batches.save_hotel_batch(
        trip_id, scope_id=scope_id, destination=destination,
        depart_date=depart_date, hotels_block=hotels_block)
    if batch_id:
        await index.score_batch(trip_id, batch_id, scope_id=scope_id,
                                kind="hotel", ctx=ctx)


def _populate_search_store(trip_id: str, body: PlanRequest, shaped: dict, direction: str) -> None:
    """Mirror this journey's scraped transport (+ hotels, going only) into the search
    store AND populate the reco index for it. This store is what the workspace API's
    /options + /recommendations endpoints read. Fire-and-forget and best-effort — it
    must never affect generation."""
    going = direction == "going"
    origin = body.origin if going else body.destination
    destination = body.destination if going else body.origin
    depart_date = body.departure_date if going else _return_date(body)
    ctx = _reco_ctx(body)
    spawn_background(
        _save_and_score_transport(trip_id, direction, origin, destination,
                                  depart_date, shaped, ctx),
        name=f"search-store:transport:{direction}:{trip_id}",
    )
    hotels = shaped.get("hotels") if going else None
    if isinstance(hotels, dict) and hotels.get("items"):
        spawn_background(
            _save_and_score_hotels(trip_id, body.destination or "", body.destination,
                                   body.departure_date, hotels, ctx),
            name=f"search-store:hotel:{trip_id}",
        )


async def _fetch_and_emit_weather(trip_id: str, body: PlanRequest, emit: "_Emitter") -> None:
    """Runs alongside the supervisor; weather is optional and never blocks the plan."""
    from app.services.weather import get_trip_weather

    try:
        weather = await get_trip_weather(
            body.destination, body.departure_date, days=body.days
        )
        if weather.get("daily"):
            await emit(
                "weather_ready", f"Weather in {weather.get('location') or body.destination}",
                [PatchOp(op="set", path="/weather", value=weather)],
                50, actor="weather",
            )
    except Exception as exc:  # noqa: BLE001 — weather is a nice-to-have
        logger.warning("[trip-state] weather fetch failed for %s: %s", trip_id, exc)


async def run_generation(trip_id: str, body: PlanRequest, user_id: str) -> None:
    """Run the full plan pipeline for a trip, streaming patches as agents finish."""
    from app.agents.supervisor import supervisor_run

    emit = _Emitter(trip_id)
    weather_task: asyncio.Task | None = None
    try:
        await emit("understanding", "Understanding your trip", [], 5)
        weather_task = asyncio.create_task(_fetch_and_emit_weather(trip_id, body, emit))

        async def on_phase(tag: str, result, error) -> None:
            if error is not None:
                await emit(
                    f"{tag}_failed", f"{tag.replace('_', ' ').title()} unavailable",
                    [PatchOp(op="set", path=f"/agent_meta/errors/{tag}", value=str(error))],
                    40,
                )
                return

            if tag == "journey":
                shaped = _shape_journey(result, include_hotels=True)
                hotels = shaped.get("hotels") or {}
                _populate_search_store(trip_id, body, shaped, "going")
                patches = [
                    PatchOp(op="set", path="/transport/going", value=shaped),
                    PatchOp(op="set", path="/transport/journey_summary",
                            value=result.get("journey_summary")),
                ]
                if isinstance(hotels, dict) and hotels.get("items"):
                    patches += [
                        PatchOp(op="set", path="/stay/hotels", value=hotels["items"]),
                        PatchOp(op="set", path="/stay/selected", value=hotels.get("recommended")),
                    ]
                await emit("transport_ready", "Transport & stays found", patches, 45,
                           reasoning=(shaped.get("reasoning") or {}).get("recommendation")
                           if isinstance(shaped.get("reasoning"), dict) else None)

            elif tag == "itinerary":
                patches = [
                    PatchOp(op="set", path="/itinerary/days", value=result.get("days") or []),
                    PatchOp(op="set", path="/itinerary/intelligence",
                            value=result.get("trip_intelligence") or {}),
                    PatchOp(op="set", path="/destination/description",
                            value=result.get("destination_description")),
                    PatchOp(op="set", path="/destination/cover_photo_ref",
                            value=result.get("cover_photo_ref")),
                    PatchOp(op="set", path="/meta/title", value=result.get("trip_name")),
                ]
                await emit("itinerary_ready", "Day-by-day itinerary built", patches, 70)

            elif tag == "return_journey":
                shaped_return = _shape_journey(result, include_hotels=False)
                _populate_search_store(trip_id, body, shaped_return, "return")
                await emit(
                    "return_ready", "Return journey planned",
                    [PatchOp(op="set", path="/transport/return_journey", value=shaped_return)],
                    80,
                )

        merged = await supervisor_run(body, user_id, on_phase=on_phase)

        # Weather must land before "complete" — the SSE stream closes on it.
        if weather_task is not None:
            try:
                await asyncio.wait_for(weather_task, timeout=10)
            except Exception:  # noqa: BLE001 — optional section
                pass

        # Authoritative final pass — budget interventions may have reshuffled
        # transport/costs after the early events, so re-set every section from
        # the reconciled merged plan.
        final = trip_state_from_merged(merged, body.model_dump())
        final.meta.trip_id = trip_id
        final.meta.status = "ready"
        fd = final.model_dump(mode="json")
        await emit(
            "complete", "Trip ready",
            _section_patches(fd, [
                "meta", "request", "destination", "transport", "stay",
                "itinerary", "costs", "documents", "agent_meta",
            ]),
            100,
            critical=True,
        )
        logger.info("[trip-state] generation complete for %s", trip_id)

        # Mirror the finished trip into the shared `trips` table so it appears
        # in My Trips next to classic /plan trips. Best-effort by design.
        from .my_trips import upsert_my_trip

        await upsert_my_trip(trip_id, user_id, fd)

        # Learn this traveller's durable preferences from the finished plan so
        # their next trip + copilot session starts warm. Best-effort, off the
        # critical path — a memory failure must not touch the completed trip.
        try:
            from app.services.user_memory import distill_from_trip

            await distill_from_trip(user_id, fd)
        except Exception as exc:  # noqa: BLE001 — memory is advisory
            logger.warning("[trip-state] memory distill failed for %s: %s", trip_id, exc)

    except Exception as exc:  # noqa: BLE001 — stream must terminate cleanly
        logger.error("[trip-state] generation failed for %s: %s", trip_id, exc, exc_info=True)
        sentry_sdk.capture_exception(exc)
        try:
            await emit(
                "error", "Trip planning failed",
                [PatchOp(op="set", path="/meta/status", value="error")],
                100,
            )
        except Exception:  # noqa: BLE001
            publish_event(trip_id, {
                "phase": "error", "label": "Trip planning failed",
                "progress": 100, "patches": [],
            })
