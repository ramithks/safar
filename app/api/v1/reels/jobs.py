"""
Job status polling, result retrieval, and SSE stream endpoints.

Source of truth: Redis (live jobs, 24h TTL). When Redis has expired the job,
status/result fall back to the persisted `reel_scans` row in Supabase so old
links keep working.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings
from app.deps.auth import require_user
from app.models.response import (
    ErrorDetail, JobProgress, JobStatusResponse, ReelResult, StageProgress
)
from app.workers.job_store import get_job

logger = logging.getLogger(__name__)
router = APIRouter()


def _scan_fallback(job_id: str) -> Optional[dict]:
    """Look up a job in the persisted reel_scans table once Redis has expired."""
    if not (settings.supabase_url and settings.supabase_service_key):
        return None
    from app.storage.supabase_client import get_scan_by_job_id
    return get_scan_by_job_id(job_id)


@router.get("/reels/jobs/{job_id}", response_model=JobStatusResponse)
def get_job_status(job_id: str, user=Depends(require_user)):
    job = get_job(job_id)

    if not job:
        scan = _scan_fallback(job_id)
        if not scan:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        result = scan.get("result") or {}
        return JobStatusResponse(
            job_id=job_id,
            status="completed" if scan.get("status") == "completed" else "failed",
            created_at=_parse_dt(scan.get("created_at")),
            updated_at=_parse_dt(scan.get("completed_at")),
            result=result if scan.get("status") == "completed" else None,
        )

    progress = _parse_progress(job.get("progress"))
    error = _parse_error(job)

    result = job.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = None

    return JobStatusResponse(
        job_id=job_id,
        status=_normalize_status(job.get("status")),
        progress=progress,
        created_at=_parse_dt(job.get("created_at")),
        updated_at=_parse_dt(job.get("updated_at")),
        error=error,
        metadata=job.get("metadata") if isinstance(job.get("metadata"), dict) else None,
        result=result if job.get("status") == "completed" and isinstance(result, dict) else None,
    )


@router.get("/reels/jobs/{job_id}/result", response_model=ReelResult)
def get_job_result(job_id: str, user=Depends(require_user)):
    job = get_job(job_id)

    if not job:
        scan = _scan_fallback(job_id)
        if scan and scan.get("result"):
            return ReelResult(**scan["result"])
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    status = job.get("status")
    if status in ("queued", "processing"):
        raise HTTPException(
            status_code=202,
            detail=f"Job is still {status}. Poll /reels/jobs/{job_id} for progress.",
        )

    result_data = job.get("result")
    if not result_data:
        raise HTTPException(status_code=500, detail="Result not found for completed job")

    if isinstance(result_data, str):
        result_data = json.loads(result_data)

    return ReelResult(**result_data)


@router.get("/reels/jobs/{job_id}/stream")
async def stream_job_events(job_id: str, user=Depends(require_user)):
    """
    Server-Sent Events stream.
    Polls Redis and pushes new events to the client; closes when the job
    completes or fails.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    return StreamingResponse(
        _event_generator(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


async def _event_generator(job_id: str) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events as they appear in Redis."""
    sent_count = 0
    poll_seconds = 1.0
    max_polls = 180  # 180 * 1s = 3 min hard stop

    for _ in range(max_polls):
        job = await asyncio.to_thread(get_job, job_id)
        if not job:
            yield _sse_event("error", {"message": "Job not found"})
            return

        events = job.get("events", [])
        if isinstance(events, str):
            try:
                events = json.loads(events)
            except Exception:
                events = []

        for event in events[sent_count:]:
            yield _sse_event(event.get("type", "message"), event.get("data", {}))
        sent_count = len(events)

        status = job.get("status")
        if status in ("completed", "failed"):
            yield _sse_event("done", {"job_id": job_id, "status": status})
            return

        await asyncio.sleep(poll_seconds)

    yield _sse_event("timeout", {"job_id": job_id})


def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _normalize_status(status: Optional[str]) -> str:
    return status if status in ("queued", "processing", "completed", "failed") else "queued"


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _parse_progress(raw) -> Optional[JobProgress]:
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    try:
        return JobProgress(
            stage=raw.get("stage", "queued"),
            stage_index=raw.get("stage_index", 0),
            total_stages=raw.get("total_stages", 5),
            percent_complete=raw.get("percent_complete", 0),
            stages=[StageProgress(**s) for s in raw.get("stages", [])],
        )
    except Exception:
        return None


def _parse_error(job: dict) -> Optional[ErrorDetail]:
    result = job.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return None
    if isinstance(result, dict) and result.get("error"):
        err = result["error"]
        try:
            return ErrorDetail(**err)
        except Exception:
            return ErrorDetail(code="UNKNOWN", message=str(err))
    return None
