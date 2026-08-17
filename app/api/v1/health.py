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
    """
    Ping Redis, and on failure say *which* Redis.

    The bare error was `Error 111 connecting to localhost:6379. Connection
    refused.`, which reads like Redis is down. In a container it means
    `REDIS_URL` was never set and the development default survived — a
    configuration problem wearing an outage's clothes. The two need telling
    apart from the response alone, because that response is usually all anyone
    pastes.

    Credentials are stripped: a Redis URL carries its password inline and this
    string ends up in chats and issue trackers. See `config.redis_target`.
    """
    from app.config import redis_is_unconfigured, redis_target

    try:
        from app.workers.job_store import _get_redis
        _get_redis().ping()
        return "ok"
    except Exception as e:
        if redis_is_unconfigured():
            return (f"error: REDIS_URL is not set, so this fell back to "
                    f"{redis_target()} — {e}")
        return f"error: connecting to {redis_target()} — {e}"


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
