"""
TravBuddy Backend
FastAPI application entry point.
"""
import logging
import os
from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.v1.router import router
from app.config import settings
from app.core.limiter import limiter

# ── Sentry — must init before FastAPI() to capture startup errors ───────────────
# DSN is pinned here; SENTRY_DSN env var overrides if set.
_SENTRY_DSN = (
    settings.sentry_dsn
    or "https://fe1488d1895bed2fc5f0435e4df3d136@o4511167749029888.ingest.us.sentry.io/4511325237346304"
)
sentry_sdk.init(
    dsn=_SENTRY_DSN,
    environment=settings.app_env,
    send_default_pii=True,           # capture request headers + user IPs
    traces_sample_rate=settings.sentry_traces_sample_rate,
    profiles_sample_rate=settings.sentry_profiles_sample_rate,
)

# ── Logging setup ────────────────────────────────────────────────────────────────
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(settings.log_level)
    ),
)
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
# httpx logs every request URL at INFO — that exposes API keys in plain text.
# Keep WARNING+ (connection errors, timeouts) but drop the URL lines.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ── Lifespan: initialize resources at startup ────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure temp directory exists
    os.makedirs(settings.temp_dir, exist_ok=True)
    os.makedirs(settings.places_cache_dir, exist_ok=True)

    # Eagerly initialize Google API clients to avoid cold-start on first request
    try:
        from app.pipeline.places_client import _get_client, _get_cache
        _get_cache()
        _get_client()
        logger.info("Places API client initialized")
    except Exception as e:
        logger.warning(f"Places API client init failed (non-fatal): {e}")

    logger.info(f"TravBuddy backend started (env={settings.app_env})")
    yield
    logger.info("TravBuddy backend shutting down")


# ── App ──────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="TravBuddy API",
    description="Extract location timelines from Instagram Reels",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


# ── Exception handlers ───────────────────────────────────────────────────────────
@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    # Raised by service-layer authorization checks (e.g. group membership)
    return JSONResponse(
        status_code=403,
        content={"detail": str(exc) or "Forbidden"},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception on %s %s: %s",
        request.method, request.url.path, exc,
        exc_info=True,
    )
    sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred.",
                "retryable": True,
            }
        },
    )


@app.get("/")
async def root():
    return {
        "service": "TravBuddy API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/api/v1/health",
    }


@app.get("/sentry-debug")
async def trigger_error():
    """Intentionally raises ZeroDivisionError to verify Sentry is capturing errors + transactions."""
    division_by_zero = 1 / 0
