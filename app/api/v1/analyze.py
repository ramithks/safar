"""POST /api/v1/reels/analyze — submit a reel for processing."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from ulid import ULID

from app.models.request import AnalyzeReelRequest
from app.models.response import AnalyzeReelResponse
from app.workers.job_store import create_job, store_job_result

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/reels/analyze", response_model=AnalyzeReelResponse, status_code=202)
async def analyze_reel(request: Request, body: AnalyzeReelRequest):
    base_url = str(request.base_url).rstrip("/")

    # ── Cache check: already processed this URL? ─────────────────────────────
    from app.config import settings
    if settings.supabase_url and settings.supabase_service_key:
        try:
            from app.storage.supabase_client import get_cached_scan
            cached = get_cached_scan(body.reel_url)
            if cached:
                job_id = cached["job_id"]
                # Re-inject into Redis so polling/result endpoints work normally
                create_job(job_id, reel_url=body.reel_url, metadata=body.metadata)
                store_job_result(job_id, cached["result"], events=[])
                logger.info(f"Cache hit for {body.reel_url} → returning {job_id}")
                return AnalyzeReelResponse(
                    job_id=job_id,
                    status="completed",
                    estimated_duration_seconds=0,
                    poll_url=f"{base_url}/api/v1/reels/jobs/{job_id}",
                    stream_url=f"{base_url}/api/v1/reels/jobs/{job_id}/stream",
                    created_at=datetime.now(timezone.utc),
                    metadata=body.metadata,
                )
        except Exception as e:
            logger.warning(f"Cache check failed, processing fresh: {e}")

    # ── No cache — enqueue fresh processing ──────────────────────────────────
    job_id = f"job_{ULID()}"
    create_job(job_id, reel_url=body.reel_url, metadata=body.metadata)

    from app.workers.celery_app import analyze_reel_task
    analyze_reel_task.delay(
        job_id=job_id,
        reel_url=body.reel_url,
        options=body.options.model_dump(),
        metadata=body.metadata,
        user_id=body.user_id,
    )

    logger.info(f"Job {job_id} queued for {body.reel_url}")
    return AnalyzeReelResponse(
        job_id=job_id,
        status="queued",
        estimated_duration_seconds=30,
        poll_url=f"{base_url}/api/v1/reels/jobs/{job_id}",
        stream_url=f"{base_url}/api/v1/reels/jobs/{job_id}/stream",
        created_at=datetime.now(timezone.utc),
        metadata=body.metadata,
    )
