"""
Celery worker configuration.
Jobs are stored in Redis so API endpoints can poll status.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from celery import Celery

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "travbuddy",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # one task at a time per worker
    result_expires=86400,          # results kept for 24 hours
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
    Stores progress and result in Redis so the API can serve them.
    """
    from app.pipeline.orchestrator import run_pipeline, PipelineEvent
    from app.workers.job_store import update_job_progress, store_job_result

    # Mark job as processing
    update_job_progress(job_id, stage="download", percent=0)

    events_buffer: list[dict] = []

    def on_event(event: PipelineEvent):
        events_buffer.append({"type": event.event_type, "data": event.data})
        _handle_progress_event(job_id, event)

    # Run the async pipeline in a sync context
    result = asyncio.get_event_loop().run_until_complete(
        run_pipeline(
            job_id=job_id,
            reel_url=reel_url,
            options=options,
            metadata=metadata,
            user_id=user_id,
            on_event=on_event,
        )
    )

    store_job_result(job_id, result, events_buffer)
    return {"job_id": job_id, "status": result.status}


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

    elif event.event_type == "completed":
        update_job_progress(job_id, stage="completed", percent=100)
