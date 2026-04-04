"""
Supabase client — persists reel scan results to the `reel_scans` table.
Skipped gracefully if SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured.
"""
import logging
from datetime import datetime, timezone
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


def get_cached_scan(reel_url: str) -> dict | None:
    """
    Return the result dict of the most recent completed scan for this URL,
    or None if no cached result exists.
    """
    try:
        client = _get_client()
        resp = (
            client.table("reel_scans")
            .select("job_id, result")
            .eq("reel_url", reel_url)
            .eq("status", "completed")
            .order("completed_at", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]
    except Exception as e:
        logger.warning(f"Cache lookup failed for {reel_url}: {e}")
    return None


def save_reel_scan(
    job_id: str,
    reel_url: str,
    user_id: Optional[str],
    caption: Optional[str],
    video_url: Optional[str],
    frame_urls: list[str],
    result_dict: dict,
    status: str,
):
    """
    Upsert a completed/failed reel scan into the `reel_scans` table.

    Table schema (create with the SQL below):

        CREATE TABLE reel_scans (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id       uuid REFERENCES auth.users(id) ON DELETE CASCADE,
            job_id        text UNIQUE NOT NULL,
            reel_url      text NOT NULL,
            caption       text,
            video_url     text,
            frame_urls    text[] DEFAULT '{}',
            result        jsonb,
            status        text NOT NULL DEFAULT 'queued',
            created_at    timestamptz NOT NULL DEFAULT now(),
            completed_at  timestamptz
        );

        ALTER TABLE reel_scans ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "users see own scans"
            ON reel_scans FOR ALL USING (auth.uid() = user_id);
    """
    try:
        client = _get_client()
        client.table("reel_scans").upsert({
            "job_id": job_id,
            "reel_url": reel_url,
            "user_id": user_id,
            "caption": caption,
            "video_url": video_url,
            "frame_urls": frame_urls,
            "result": result_dict,
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        logger.info(f"Saved reel scan {job_id} to Supabase (user={user_id})")
    except Exception as e:
        logger.error(f"Failed to save reel scan {job_id} to Supabase: {e}")
