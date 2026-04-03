"""
Job status polling, result retrieval, and SSE stream endpoints.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.models.response import (
    ErrorDetail, JobProgress, JobStatusResponse, ReelResult, StageProgress
)
from app.workers.job_store import get_job

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/reels/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    progress = _parse_progress(job.get("progress"))
    error = _parse_error(job)

    return JobStatusResponse(
        job_id=job_id,
        status=job.get("status", "queued"),
        progress=progress,
        created_at=_parse_dt(job.get("created_at")),
        updated_at=_parse_dt(job.get("updated_at")),
        error=error,
        metadata=job.get("metadata") if isinstance(job.get("metadata"), dict) else None,
    )


@router.get("/reels/jobs/{job_id}/result", response_model=ReelResult)
async def get_job_result(job_id: str):
    job = get_job(job_id)
    if not job:
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
async def stream_job_events(job_id: str):
    """
    Server-Sent Events stream.
    Polls Redis every 500ms and pushes new events to the client.
    Closes automatically when the job is completed or failed.
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
    max_polls = 300  # 300 * 500ms = 150s timeout

    for _ in range(max_polls):
        job = get_job(job_id)
        if not job:
            yield _sse_event("error", {"message": "Job not found"})
            return

        events = job.get("events", [])
        if isinstance(events, str):
            try:
                events = json.loads(events)
            except Exception:
                events = []

        # Send any new events since last poll
        for event in events[sent_count:]:
            yield _sse_event(
                event.get("type", "message"),
                event.get("data", {}),
            )
            sent_count += 1

        status = job.get("status")
        if status in ("completed", "failed"):
            # Send final status event
            result_data = job.get("result")
            if result_data:
                if isinstance(result_data, str):
                    result_data = json.loads(result_data)
                total = (
                    result_data.get("summary", {}).get("total_locations", 0)
                    if isinstance(result_data, dict) else 0
                )
                yield _sse_event("completed" if status == "completed" else "failed", {
                    "job_id": job_id,
                    "status": status,
                    "total_locations": total,
                })
            return

        await asyncio.sleep(0.5)

    # Timeout
    yield _sse_event("timeout", {"message": "Stream timeout. Use polling endpoint."})


def _sse_event(event_type: str, data: dict) -> str:
    payload = json.dumps(data)
    return f"event: {event_type}\ndata: {payload}\n\n"


def _parse_progress(progress_data) -> JobProgress | None:
    if not progress_data:
        return None
    if isinstance(progress_data, str):
        try:
            progress_data = json.loads(progress_data)
        except Exception:
            return None

    stages = [
        StageProgress(
            name=s.get("name", ""),
            status=s.get("status", "pending"),
            duration_ms=s.get("duration_ms"),
        )
        for s in progress_data.get("stages", [])
    ]

    return JobProgress(
        stage=progress_data.get("stage", ""),
        stage_index=progress_data.get("stage_index", 0),
        total_stages=progress_data.get("total_stages", 5),
        percent_complete=progress_data.get("percent_complete", 0),
        stages=stages,
    )


def _parse_error(job: dict) -> ErrorDetail | None:
    result = job.get("result")
    if not result:
        return None
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            return None
    if isinstance(result, dict) and result.get("error"):
        err = result["error"]
        return ErrorDetail(
            code=err.get("code", "UNKNOWN"),
            message=err.get("message", ""),
            retryable=err.get("retryable", False),
        )
    return None


def _parse_dt(dt_str: str | None) -> datetime:
    if not dt_str:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return datetime.now(timezone.utc)
