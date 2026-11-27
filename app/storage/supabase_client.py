"""
Supabase client — persists reel scan results to the `reel_scans` table.
Skipped gracefully if SUPABASE_URL or SUPABASE_SERVICE_KEY is not configured.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

def _get_client():
    """Thread-local, retry-wrapped, HTTP/1.1 service-role client.

    Previously a process-wide `create_client()` singleton shared across executor
    threads — the exact pattern that corrupted the TLS stream under concurrent
    writes (SSLV3_ALERT_BAD_RECORD_MAC). Delegating to `db.client.get_db()` gives
    one client PER THREAD plus the stale-connection retry wrapper.
    """
    from app.db.client import get_db
    return get_db()


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


def create_reel_job(
    job_id: str,
    user_id: str,
    reel_url: str,
    status: str = "pending",
    progress: int = 0,
    title: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
    video_url: Optional[str] = None,
    place_category: Optional[str] = None,
):
    """
    Upsert this user's row for a pipeline job into `reel_jobs` so the app's
    "developing reels" feed can show it. One row per (user_id, job_id) —
    cache hits create a row per user pointing at the same pipeline job.
    """
    try:
        client = _get_client()
        client.table("reel_jobs").upsert({
            "user_id": user_id,
            "job_id": job_id,
            "source_url": reel_url,
            "status": status,
            "progress": progress,
            "title": title,
            "thumbnail_url": thumbnail_url,
            "video_url": video_url,
            "place_category": place_category,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="user_id,job_id").execute()
    except Exception as e:
        logger.warning(f"Failed to create reel_jobs row for {job_id}: {e}")


def update_reel_job(job_id: str, **fields):
    """
    Update every user's row for this pipeline job (progress/status are
    identical for all riders of a shared cached job).
    """
    if not fields:
        return
    try:
        client = _get_client()
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        client.table("reel_jobs").update(fields).eq("job_id", job_id).execute()
    except Exception as e:
        logger.warning(f"Failed to update reel_jobs row for {job_id}: {e}")


def auto_save_completed_job(job_id: str, user_id: Optional[str] = None):
    """
    Insert a saved_reels row for every user riding this completed pipeline job
    (or just `user_id` if given) so it shows up in the app's Recent Reels
    without a manual save step. Idempotent via the (user_id, job_id) unique
    index — re-imports never duplicate or clobber an existing saved reel.
    """
    try:
        client = _get_client()
        q = (
            client.table("reel_jobs")
            .select("id, user_id, title, thumbnail_url, video_url, place_category")
            .eq("job_id", job_id)
        )
        if user_id:
            q = q.eq("user_id", user_id)
        rows = q.execute().data or []
        if not rows:
            return
        client.table("saved_reels").upsert(
            [
                {
                    "user_id": r["user_id"],
                    "job_id": r["id"],
                    "title": r.get("title") or "Imported Reel",
                    "category": r.get("place_category") or "other",
                    "thumbnail_url": r.get("thumbnail_url"),
                    "video_url": r.get("video_url"),
                    "place_category": r.get("place_category"),
                    "description": "Discovered with Safar AI.",
                    "vibes": ["AI Curated"],
                    "is_liked": False,
                }
                for r in rows
            ],
            on_conflict="user_id,job_id",
            ignore_duplicates=True,
        ).execute()
        logger.info(f"Auto-saved reel job {job_id} for {len(rows)} user(s)")
    except Exception as e:
        logger.warning(f"Auto-save failed for job {job_id}: {e}")


def get_scan_by_job_id(job_id: str) -> dict | None:
    """Fetch a persisted scan (status + result) — fallback when Redis TTL expired."""
    try:
        client = _get_client()
        resp = (
            client.table("reel_scans")
            .select("job_id, reel_url, status, result, created_at, completed_at")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]
    except Exception as e:
        logger.warning(f"Scan lookup by job_id failed for {job_id}: {e}")
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
