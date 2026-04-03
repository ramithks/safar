"""
Supabase client — persists job results to the `jobs` table.
Skipped gracefully if SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from supabase import create_client
        from app.config import settings
        _client = create_client(settings.supabase_url, settings.supabase_service_key)
    return _client


def save_job_result(
    job_id: str,
    reel_url: str,
    status: str,
    result_dict: dict,
    video_gcs_url: Optional[str] = None,
):
    """
    Upsert a completed/failed job into the Supabase `jobs` table.

    Expected table schema:
        job_id          text PRIMARY KEY
        reel_url        text
        status          text
        video_gcs_url   text
        result          jsonb
        created_at      timestamptz DEFAULT now()
        completed_at    timestamptz DEFAULT now()
    """
    try:
        client = _get_client()
        client.table("jobs").upsert({
            "job_id": job_id,
            "reel_url": reel_url,
            "status": status,
            "video_gcs_url": video_gcs_url,
            "result": result_dict,
        }).execute()
        logger.info(f"Saved job {job_id} to Supabase")
    except Exception as e:
        logger.error(f"Failed to save job {job_id} to Supabase: {e}")
