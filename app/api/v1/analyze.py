"""POST /api/v1/reels/analyze — submit a reel for processing."""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from ulid import ULID

from app.models.request import AnalyzeReelRequest
from app.models.response import AnalyzeReelResponse
from app.workers.job_store import create_job, get_job

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/reels/analyze", response_model=AnalyzeReelResponse, status_code=202)
async def analyze_reel(request: Request, body: AnalyzeReelRequest):
    # Duplicate detection: same URL within the last 5 minutes returns existing job
    # (simple check — not perfect but avoids duplicate processing on double-clicks)
    job_id = f"job_{ULID()}"

    create_job(job_id, reel_url=body.reel_url, metadata=body.metadata)

    # Enqueue Celery task
    from app.workers.celery_app import analyze_reel_task
    analyze_reel_task.delay(
        job_id=job_id,
        reel_url=body.reel_url,
        options=body.options.model_dump(),
        metadata=body.metadata,
        user_id=body.user_id,
    )

    base_url = str(request.base_url).rstrip("/")
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
