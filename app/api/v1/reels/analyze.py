"""
POST /api/v1/reels/analyze — submit a reel for processing.

Auth + abuse protection: this is the most expensive endpoint in the product
(video download, GCS storage, Gemini + Places calls), so it requires a valid
Supabase JWT and is rate-limited per user.

Dedup layers (cheapest first):
  1. reel_scans cache  — same canonical URL already fully processed → instant result
  2. in-flight lock    — same user submits the same reel twice while it's
                         still processing → reuse the running job
  3. fresh enqueue     — Celery task + Redis job + reel_jobs row
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from ulid import ULID

from app.config import settings
from app.core.limiter import limiter
from app.deps.auth import require_user
from app.models.request import AnalyzeReelRequest
from app.models.response import AnalyzeReelResponse
from app.pipeline.downloader import canonical_reel_url
from app.workers.job_store import create_job, get_job, store_job_result, _get_redis

logger = logging.getLogger(__name__)
router = APIRouter()

INFLIGHT_TTL_SECONDS = 900  # forget the in-flight lock after 15 min (job is stuck/dead by then)


def _job_urls(base_url: str, job_id: str) -> dict:
    return {
        "poll_url": f"{base_url}/api/v1/reels/jobs/{job_id}",
        "stream_url": f"{base_url}/api/v1/reels/jobs/{job_id}/stream",
    }


def _result_display_fields(result: dict) -> dict:
    """Pull title/thumbnail/video/category out of a ReelResult dict for the reel_jobs row."""
    from collections import Counter

    timeline = result.get("timeline") or []
    summary = result.get("summary") or {}
    first_place = next(
        (seg.get("place", {}).get("name") for seg in timeline if seg.get("place")),
        None,
    )
    categories = Counter(
        (seg.get("place") or {}).get("category")
        for seg in timeline
        if (seg.get("place") or {}).get("category")
    )
    return {
        "title": summary.get("dominant_city") or first_place or "Imported Reel",
        "thumbnail_url": next(
            (seg.get("frame_thumbnail_url") for seg in timeline if seg.get("frame_thumbnail_url")),
            None,
        ),
        "video_url": (result.get("reel") or {}).get("video_url"),
        "place_category": categories.most_common(1)[0][0] if categories else None,
    }


@router.post("/reels/analyze", response_model=AnalyzeReelResponse, status_code=202)
@limiter.limit("10/hour")
def analyze_reel(request: Request, body: AnalyzeReelRequest, user=Depends(require_user)):
    base_url = str(request.base_url).rstrip("/")

    canonical_url = canonical_reel_url(body.reel_url)
    if not canonical_url:
        raise HTTPException(status_code=422, detail="Could not parse an Instagram post from that URL")

    # ── 1. Cache: this exact reel was already fully processed ────────────────
    if settings.supabase_url and settings.supabase_service_key:
        try:
            from app.storage.supabase_client import get_cached_scan, create_reel_job, auto_save_completed_job
            cached = get_cached_scan(canonical_url)
            if cached and cached.get("result"):
                job_id = cached["job_id"]
                # Re-inject into Redis so polling/result endpoints work normally
                create_job(job_id, reel_url=canonical_url, metadata=body.metadata)
                store_job_result(job_id, cached["result"], events=[])
                # Track the job for THIS user, then drop it straight into their saved reels
                create_reel_job(
                    job_id=job_id,
                    user_id=str(user.id),
                    reel_url=canonical_url,
                    status="completed",
                    progress=100,
                    **_result_display_fields(cached["result"]),
                )
                auto_save_completed_job(job_id, user_id=str(user.id))
                logger.info(f"Cache hit for {canonical_url} → returning {job_id}")
                return AnalyzeReelResponse(
                    job_id=job_id,
                    status="completed",
                    estimated_duration_seconds=0,
                    created_at=datetime.now(timezone.utc),
                    metadata=body.metadata,
                    **_job_urls(base_url, job_id),
                )
        except Exception as e:
            logger.error("Cache check failed, processing fresh: %s: %s", type(e).__name__, e, exc_info=True)

    # ── 2. In-flight dedupe: same user, same reel, still processing ──────────
    new_job_id = f"job_{ULID()}"
    inflight_key = f"inflight:{user.id}:{canonical_url}"
    try:
        r = _get_redis()
        claimed = r.set(inflight_key, new_job_id, nx=True, ex=INFLIGHT_TTL_SECONDS)
        if not claimed:
            existing_id = r.get(inflight_key)
            existing = get_job(existing_id) if existing_id else None
            if existing and existing.get("status") in ("queued", "processing"):
                logger.info(f"In-flight dedupe: {canonical_url} → reusing {existing_id}")
                return AnalyzeReelResponse(
                    job_id=existing_id,
                    status=existing.get("status", "processing"),
                    estimated_duration_seconds=30,
                    created_at=datetime.now(timezone.utc),
                    metadata=body.metadata,
                    **_job_urls(base_url, existing_id),
                )
            # Stale lock (job finished or vanished) — take it over
            r.set(inflight_key, new_job_id, ex=INFLIGHT_TTL_SECONDS)
    except Exception as e:
        logger.warning(f"In-flight dedupe unavailable (continuing): {e}")

    # ── 3. Fresh enqueue ──────────────────────────────────────────────────────
    job_id = new_job_id
    create_job(job_id, reel_url=canonical_url, metadata=body.metadata)

    if settings.supabase_url and settings.supabase_service_key:
        from app.storage.supabase_client import create_reel_job
        create_reel_job(job_id=job_id, user_id=str(user.id), reel_url=canonical_url)

    from app.workers.celery_app import analyze_reel_task
    from app.workers.job_store import redis_available
    if redis_available():
        analyze_reel_task.delay(
            job_id=job_id,
            reel_url=canonical_url,
            options=body.options.model_dump(),
            metadata=body.metadata,
            user_id=str(user.id),
        )
    else:
        # Free single-instance deployment: no Redis, no Celery worker. Run the
        # pipeline in-process on a daemon thread (same pattern as the trip
        # planner's in-process generation) so the feature still works.
        import threading
        logger.info("No Redis/worker - running reel job %s in-process", job_id)
        threading.Thread(
            target=analyze_reel_task.apply,
            kwargs={"kwargs": {
                "job_id": job_id,
                "reel_url": canonical_url,
                "options": body.options.model_dump(),
                "metadata": body.metadata,
                "user_id": str(user.id),
            }},
            daemon=True,
        ).start()

    logger.info(f"Job {job_id} queued for {canonical_url} (user={user.id})")
    return AnalyzeReelResponse(
        job_id=job_id,
        status="queued",
        estimated_duration_seconds=30,
        created_at=datetime.now(timezone.utc),
        metadata=body.metadata,
        **_job_urls(base_url, job_id),
    )
