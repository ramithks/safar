"""
Redis-backed job store.
Stores job status, progress, SSE events, and final results.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis as redis_lib

from app.config import settings

logger = logging.getLogger(__name__)

JOB_TTL_SECONDS = 86400  # 24 hours

_redis = None


class _MemoryRedis:
    """In-memory stand-in for the Redis ops this module uses. Lets the reel
    pipeline run on a single free instance with no Redis: job state lives and
    dies with the web process (acceptable for one dyno, no persistence)."""

    def __init__(self):
        self._hashes: dict = {}
        self._values: dict = {}

    def hset(self, name, key=None, value=None, mapping=None):
        h = self._hashes.setdefault(name, {})
        if mapping:
            h.update(mapping)
        elif key is not None:
            h[key] = value

    def hgetall(self, name):
        return dict(self._hashes.get(name, {}))

    def expire(self, name, seconds):
        return True

    def set(self, name, value, nx=False, ex=None):
        if nx and name in self._values:
            return None
        self._values[name] = value
        return True

    def get(self, name):
        return self._values.get(name)

    def ping(self):
        return True


def _get_redis():
    global _redis
    if _redis is None:
        try:
            client = redis_lib.from_url(settings.redis_url, decode_responses=True)
            client.ping()
            _redis = client
        except Exception:
            logger.warning("Redis unreachable - using in-memory job store (single process, no persistence)")
            _redis = _MemoryRedis()
    return _redis


def redis_available() -> bool:
    """True when a real Redis broker answered; False when running on memory."""
    return not isinstance(_get_redis(), _MemoryRedis)


def create_job(job_id: str, reel_url: str, metadata: Optional[dict] = None) -> dict:
    r = _get_redis()
    now = datetime.now(timezone.utc).isoformat()
    job = {
        "job_id": job_id,
        "reel_url": reel_url,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "progress": json.dumps({
            "stage": "queued",
            "stage_index": 0,
            "total_stages": 5,
            "percent_complete": 0,
            "stages": [
                {"name": "download", "status": "pending", "duration_ms": None},
                {"name": "scene_detection", "status": "pending", "duration_ms": None},
                {"name": "frame_extraction", "status": "pending", "duration_ms": None},
                {"name": "vision_analysis", "status": "pending", "duration_ms": None},
                {"name": "places_enrichment", "status": "pending", "duration_ms": None},
            ],
        }),
        "metadata": json.dumps(metadata or {}),
        "events": json.dumps([]),
    }
    r.hset(f"job:{job_id}", mapping=job)
    r.expire(f"job:{job_id}", JOB_TTL_SECONDS)
    return job


def get_job(job_id: str) -> Optional[dict]:
    r = _get_redis()
    job = r.hgetall(f"job:{job_id}")
    if not job:
        return None
    # Deserialize JSON fields
    for field in ("progress", "metadata", "events", "result"):
        if field in job and job[field]:
            try:
                job[field] = json.loads(job[field])
            except Exception:
                pass
    return job


def update_job_progress(
    job_id: str,
    stage: str,
    percent: int,
    stage_duration_ms: Optional[int] = None,
):
    r = _get_redis()
    job = r.hgetall(f"job:{job_id}")
    if not job:
        return

    stage_names = ["download", "scene_detection", "frame_extraction", "vision_analysis", "places_enrichment"]

    try:
        progress = json.loads(job.get("progress", "{}"))
    except Exception:
        progress = {}

    stage_index = stage_names.index(stage) if stage in stage_names else 0
    stages = progress.get("stages", [])

    for s in stages:
        if s["name"] == stage:
            s["status"] = "processing"
            if stage_duration_ms:
                s["duration_ms"] = stage_duration_ms
                s["status"] = "done"
        elif stage_names.index(s["name"]) < stage_index:
            s["status"] = "done"

    progress.update({
        "stage": stage,
        "stage_index": stage_index,
        "percent_complete": percent,
        "stages": stages,
    })

    now = datetime.now(timezone.utc).isoformat()
    r.hset(
        f"job:{job_id}",
        mapping={
            "status": "processing" if percent < 100 else "completed",
            "updated_at": now,
            "progress": json.dumps(progress),
        },
    )
    r.expire(f"job:{job_id}", JOB_TTL_SECONDS)


def store_job_result(job_id: str, result, events: list[dict]):
    """Store the final pipeline result and mark job as completed/failed."""
    r = _get_redis()
    now = datetime.now(timezone.utc).isoformat()

    result_dict = result if isinstance(result, dict) else result.model_dump(mode="json")
    status = result_dict.get("status") if isinstance(result, dict) else result.status

    r.hset(
        f"job:{job_id}",
        mapping={
            "status": status,
            "updated_at": now,
            "completed_at": now,
            "result": json.dumps(result_dict),
            "events": json.dumps(events),
        },
    )
    r.expire(f"job:{job_id}", JOB_TTL_SECONDS)


def append_event(job_id: str, event: dict):
    """Append a single SSE event to the job's event list."""
    r = _get_redis()
    job = r.hgetall(f"job:{job_id}")
    if not job:
        return
    try:
        events = json.loads(job.get("events", "[]"))
    except Exception:
        events = []
    events.append(event)
    r.hset(f"job:{job_id}", "events", json.dumps(events))
