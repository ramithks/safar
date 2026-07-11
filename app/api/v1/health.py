"""GET /api/v1/health — dependency health check."""
import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health_check():
    checks = {
        "redis": _check_redis(),
        "supabase": await _check_supabase(),
        "google_vision_api": _check_vision_api(),
        "google_places_api": _check_places_api(),
        "yt_dlp": _check_ytdlp(),
    }
    overall = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "dependencies": checks, "version": "1.0.0"}


async def _check_supabase() -> str:
    """Verify PostgREST is reachable — a boot-time misconfig surfaces here, not
    on the first user request. Runs on the dedicated DB executor."""
    from app.config import settings
    if not (settings.supabase_url and settings.supabase_service_key):
        return "not configured"
    try:
        from app.db.client import get_db, run_db
        await run_db(lambda: get_db().table("profiles").select("id").limit(1).execute())
        return "ok"
    except Exception as e:
        return f"error: {type(e).__name__}"


def _check_redis() -> str:
    try:
        from app.workers.job_store import _get_redis
        r = _get_redis()
        r.ping()
        return "ok"
    except Exception as e:
        return f"error: {e}"


def _check_vision_api() -> str:
    from app.config import settings
    if not settings.gcp_project:
        return "not configured (GCP_PROJECT missing)"
    return "ok"


def _check_places_api() -> str:
    from app.config import settings
    if not settings.google_maps_api_key:
        return "not configured"
    return "ok"


def _check_ytdlp() -> str:
    try:
        import yt_dlp
        return "ok"
    except ImportError:
        return "not installed"
