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
import ssl
from datetime import datetime, timedelta

import httpx
import redis as redis_lib
import sentry_sdk

from app.db.client import db_stats_delta, db_stats_snapshot, format_db_delta, run_db

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


def reset_stream(trip_id: str) -> None:
    """Clear the live replay buffer before a REGENERATION. The old run's
    terminal `complete` event would otherwise close a freshly opened stream
    instantly. The durable event log (Postgres) is untouched."""
    try:
        _get_redis().delete(_stream_key(trip_id))
    except Exception as exc:  # noqa: BLE001 — stream buffer is best-effort
        logger.debug("[trip-state] stream reset failed for %s: %s", trip_id, exc)


class _Emitter:
    """Streams patches to the live Redis stream immediately, and persists them to
    the durable event log EXACTLY ONCE — at the authoritative `critical` emit that
    ends generation.

    Why one write: the frontend consumes the live Redis stream, and a
    mid-generation read reconstructs full state by overlaying that stream on the
    snapshot (endpoint._overlay_stream_events). Per-phase DB writes therefore only
    duplicated what the final commit re-sets anyway — and each was a read-modify-
    write whose storm retries multiplied Supabase traffic (the SSL bad_record_mac
    storm). So intermediate patches are buffered in emit order and folded into the
    single commit: nothing (weather, agent errors) is lost, later authoritative
    sections win for overlapping paths, and the durable snapshot still converges
    to the live trip.

    If the one commit hits a TLS storm it hands the whole buffer to the background
    flush queue, which retries for minutes — the live stream already delivered
    everything to connected clients.
    """

    def __init__(self, trip_id: str):
        self.trip_id = trip_id
        # Weather runs in parallel with supervisor phases; the lock serializes
        # emits so buffer appends and the final commit never interleave.
        self._lock = asyncio.Lock()
        # Every streamed patch not yet persisted, in emit order. Drained by the
        # single commit at the critical emit.
        self._buffer: list[PatchOp] = []

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
        if critical:
            # The one durable write of the whole generation: everything buffered so
            # far, then this emit's authoritative sections (last-write-wins for any
            # overlapping path; buffered-only paths like /weather are preserved).
            to_persist = self._buffer + patches
            if to_persist:
                try:
                    await run_db(
                        lambda: store.append_events(
                            self.trip_id, actor, to_persist, phase=phase, reasoning=reasoning
                        )
                    )
                except (httpx.TransportError, ssl.SSLError) as exc:
                    flush.enqueue(
                        self.trip_id, actor, to_persist, phase=phase, reasoning=reasoning
                    )
                    logger.warning(
                        "[trip-state] %s on final commit for %s — handed %d patches to background flush",
                        type(exc).__name__, self.trip_id, len(to_persist),
                    )
                self._buffer = []
        else:
            # Live-stream only; persistence is deferred to the single commit.
            self._buffer.extend(patches)
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
    """Entry point: admit through the global generation queue, then generate.

    Beyond `max_concurrent_generations` in-flight (per process), this WAITS for a
    slot instead of piling on — the top-level load-shedder. The trip row already
    exists; a "queued" event tells the frontend to show a queue spinner."""
    from app.core.generation_queue import generation_slot, has_free_slot

    emit = _Emitter(trip_id)
    if not has_free_slot():
        try:
            await emit(
                "queued", "In queue — your trip starts shortly",
                [PatchOp(op="set", path="/meta/status", value="queued")], 2,
            )
        except Exception:  # noqa: BLE001 — a queued notice must never break generation
            pass
    async with generation_slot(trip_id):
        await _generate(trip_id, body, user_id, emit)


async def _generate(trip_id: str, body: PlanRequest, user_id: str, emit: "_Emitter") -> None:
    """Run the full plan pipeline for a trip, streaming patches as agents finish."""
    from app.agents.supervisor import supervisor_run

    from . import integrity

    # Generation Manifest — opened before any work, committed with the result:
    # records exactly which models/provider parsers produced this trip.
    manifest = integrity.build_manifest(spec_version=1)

    weather_task: asyncio.Task | None = None
    db_before = db_stats_snapshot()
    try:
        await emit("understanding", "Understanding your trip", [], 5)
        weather_task = asyncio.create_task(_fetch_and_emit_weather(trip_id, body, emit))

        # ── Unified spec-driven pipeline (v4 M3): the ONLY path that can build
        # multi-stop trips; zero-stop trips ride it behind the bake flag. A trip
        # that already HAS a graph must also regenerate through the composer —
        # the supervisor path doesn't write /graph, so it would leave a stale
        # multi-stop graph overriding the fresh transport at projection time.
        row = await run_db(store.get_trip_state, trip_id)
        stored = (row or {}).get("state") or {}
        use_composer = bool(
            getattr(body, "stops", None)
            or settings.unified_generator
            or stored.get("graph")
        )
        if use_composer:
            from . import compose

            spec = stored.get("spec") or {}
            if not spec:
                spec = {**body.model_dump(), "budget_tier": body.budget, "version": 1}
            manifest["spec_version"] = spec.get("version") or 1
            fd = await compose.generate_from_spec(
                trip_id, spec, user_id, emit, manifest,
                prior_graph=stored.get("graph") or None,
                weather_task=weather_task,   # awaited pre-commit so /weather persists
            )
            weather_task = None
            logger.info("[trip-state] generation complete for %s (composer)", trip_id)

            from .my_trips import upsert_my_trip

            await upsert_my_trip(trip_id, user_id, fd)
            try:
                from app.services.user_memory import distill_from_trip

                await distill_from_trip(user_id, fd)
            except Exception as exc:  # noqa: BLE001 — memory is advisory
                logger.warning("[trip-state] memory distill failed for %s: %s", trip_id, exc)
            logger.info(
                "[trip-state] generation %s cost — %s",
                trip_id, format_db_delta(db_stats_delta(db_before)),
            )
            return

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

        # v2 owns persistence (trip_states/events + upsert_my_trip); skip the
        # supervisor's legacy trips/trip_journey writes — they'd be orphan rows
        # whose resilient retries amplify the TLS-corruption storm.
        merged = await supervisor_run(
            body, user_id, on_phase=on_phase, save_legacy_tables=False
        )

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
        final.meta.generation = integrity.finalize_manifest(manifest)
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

        logger.info(
            "[trip-state] generation %s cost — %s",
            trip_id, format_db_delta(db_stats_delta(db_before)),
        )

    except Exception as exc:  # noqa: BLE001 — stream must terminate cleanly
        logger.error(
            "[trip-state] generation failed for %s (%s) — %s",
            trip_id, format_db_delta(db_stats_delta(db_before)), exc, exc_info=True,
        )
        sentry_sdk.capture_exception(exc)
        try:
            # critical: no further emit follows, so this is the only chance to
            # commit the failed status (and anything buffered) to the durable snapshot.
            await emit(
                "error", "Trip planning failed",
                [
                    PatchOp(op="set", path="/meta/status", value="error"),
                    PatchOp(op="set", path="/meta/generation",
                            value=integrity.finalize_manifest(manifest, status="failed")),
                ],
                100,
                critical=True,
            )
        except Exception:  # noqa: BLE001
            publish_event(trip_id, {
                "phase": "error", "label": "Trip planning failed",
                "progress": 100, "patches": [],
            })
