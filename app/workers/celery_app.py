"""
Celery worker configuration.
Jobs are stored in Redis so API endpoints can poll status.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import sentry_sdk
from celery import Celery
from celery.signals import celeryd_init, worker_process_init

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "travbuddy",
    broker=settings.redis_url,
    backend=settings.redis_url,
    # Task modules the worker must import so their @task functions register. Without
    # this, tasks defined outside celery_app.py (e.g. the recommendation workers) are
    # never discovered and beat's schedule fires into the void.
    include=["app.workers.reco_tasks"],
)


# ── Sentry ────────────────────────────────────────────────────────────────────
# Initialize Sentry only when running AS a worker — not on mere import (the API
# imports this module to enqueue tasks and must keep its own "api" Sentry init).
# celeryd_init covers the main worker process; worker_process_init covers each
# forked prefork child so every task-executing process reports to Sentry.
@celeryd_init.connect
def _sentry_celeryd_init(**_kwargs):
    from app.observability import init_sentry
    init_sentry("worker")


@worker_process_init.connect
def _sentry_worker_process_init(**_kwargs):
    from app.observability import init_sentry
    init_sentry("worker")

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,           # one task at a time per worker process
    worker_concurrency=settings.celery_worker_concurrency,  # cap concurrent workers (rate-limit protection)
    result_expires=86400,                   # results kept for 24 hours
    # Recommendation-engine nightly/weekly maintenance (docs/recommendation_engine.md
    # Part 9.3 / 14.3). Defined here — not in reco_tasks — so `celery beat` sees the
    # schedule from conf regardless of task-module import order. Task functions are
    # registered via `include` above and executed by the worker.
    beat_schedule={
        "reco-expected-price-nightly": {
            "task": "travbuddy.reco.refresh_expected_price", "schedule": 24 * 3600,
        },
        "reco-destination-priors-nightly": {
            "task": "travbuddy.reco.refresh_destination_priors", "schedule": 24 * 3600,
        },
        "reco-fit-centroids-weekly": {
            "task": "travbuddy.reco.fit_centroids", "schedule": 7 * 24 * 3600,
        },
    },
)


@celery_app.task(bind=True, name="travbuddy.analyze_reel", max_retries=0)
def analyze_reel_task(
    self,
    job_id: str,
    reel_url: str,
    options: dict,
    metadata: Optional[dict],
    user_id: Optional[str] = None,
):
    """
    Celery task that runs the full pipeline.
    Stores progress and result in Redis (API polling) and mirrors job state
    into the Supabase `reel_jobs` table (app's developing-reels feed).
    """
    from app.pipeline.orchestrator import run_pipeline, PipelineEvent
    from app.workers.job_store import update_job_progress, store_job_result, _get_redis

    # Tag Sentry with the Supabase user + job so worker errors are traceable to a
    # specific user/row. Set on the global scope because the pipeline offloads
    # work (e.g. frame uploads) to executor threads that don't inherit the
    # task's scope; the worker runs one task at a time so there's no cross-talk.
    _scope = sentry_sdk.get_global_scope()
    _scope.set_tag("job_id", job_id)
    _scope.set_user({"id": user_id} if user_id else None)

    # Idempotency guard: with acks_late a worker crash redelivers the task —
    # never re-run a job that already started (each run costs Gemini/Places $).
    try:
        claimed = _get_redis().set(f"job:{job_id}:claimed", "1", nx=True, ex=3600)
        if not claimed:
            logger.warning(f"Job {job_id} already claimed by another run — skipping redelivery")
            return {"job_id": job_id, "status": "duplicate"}
    except Exception as e:
        logger.warning(f"Idempotency check unavailable for {job_id} (continuing): {e}")

    update_job_progress(job_id, stage="download", percent=0)
    _update_supabase_job(job_id, status="processing", progress=0)

    events_buffer: list[dict] = []

    def on_event(event: PipelineEvent):
        events_buffer.append({"type": event.event_type, "data": event.data})
        _handle_progress_event(job_id, event)

    try:
        result = asyncio.run(
            run_pipeline(
                job_id=job_id,
                reel_url=reel_url,
                options=options,
                metadata=metadata,
                user_id=user_id,
                on_event=on_event,
            )
        )
    except Exception as e:
        logger.exception(f"Pipeline crashed for {job_id}: {e}")
        _update_supabase_job(job_id, status="failed", error_message=str(e)[:500])
        raise

    store_job_result(job_id, result, events_buffer)
    _finalize_supabase_job(job_id, result)
    return {"job_id": job_id, "status": result.status}


def _update_supabase_job(job_id: str, **fields):
    """Mirror job state into the reel_jobs table; never fail the pipeline over it."""
    from app.config import settings
    if not (settings.supabase_url and settings.supabase_service_key):
        return
    try:
        from app.storage.supabase_client import update_reel_job
        update_reel_job(job_id, **fields)
    except Exception as e:
        logger.warning(f"reel_jobs mirror update failed for {job_id}: {e}")


def _finalize_supabase_job(job_id: str, result):
    """Write final status + display fields (title/thumbnail/video/category) to reel_jobs."""
    from collections import Counter

    fields: dict = {"progress": 100, "status": result.status}

    if result.status == "completed":
        first_place = next(
            (seg.place.name for seg in result.timeline if seg.place and seg.place.name),
            None,
        )
        fields["title"] = result.summary.dominant_city or first_place or "Imported Reel"
        fields["thumbnail_url"] = next(
            (seg.frame_thumbnail_url for seg in result.timeline if seg.frame_thumbnail_url),
            None,
        )
        fields["video_url"] = result.reel.video_url
        # Dominant place category drives the app's "Plan trip" CTA
        # (shown for tourist places, hidden for cafes/restaurants/etc.)
        categories = Counter(
            seg.place.category for seg in result.timeline
            if seg.place and seg.place.category
        )
        if categories:
            fields["place_category"] = categories.most_common(1)[0][0]
    elif result.error:
        fields["error_message"] = result.error.message[:500]

    _update_supabase_job(job_id, **fields)

    # Completed reels land in the app's Recent Reels automatically
    if result.status == "completed":
        from app.config import settings
        if settings.supabase_url and settings.supabase_service_key:
            try:
                from app.storage.supabase_client import auto_save_completed_job
                auto_save_completed_job(job_id)
            except Exception as e:
                logger.warning(f"saved_reels auto-save failed for {job_id}: {e}")


def _handle_progress_event(job_id: str, event):
    """Update Redis job progress based on pipeline events."""
    from app.workers.job_store import update_job_progress

    stage_order = {
        "download": (0, 20),
        "scene_detection": (1, 40),
        "frame_extraction": (2, 55),
        "vision_analysis": (3, 80),
        "places_enrichment": (4, 95),
    }

    if event.event_type == "stage_start":
        stage = event.data.get("stage", "")
        if stage in stage_order:
            idx, pct = stage_order[stage]
            update_job_progress(job_id, stage=stage, percent=pct)
            # Mirror to reel_jobs — only on stage transitions (~5 writes/job)
            _update_supabase_job(job_id, status="processing", progress=pct)

    elif event.event_type == "completed":
        update_job_progress(job_id, stage="completed", percent=100)
