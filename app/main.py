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
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        send_default_pii=True,
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


# ── Global exception handler ─────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
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
