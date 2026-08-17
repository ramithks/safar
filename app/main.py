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
from app.api.v3.router import router as router_v3
from app.config import settings
from app.core.limiter import limiter
from app.core.timeout import TimeoutMiddleware
from app.observability import init_sentry

# ── Sentry — must init before FastAPI() to capture startup errors ───────────────
init_sentry("api")

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
    # Off by default — see settings.expose_api_docs. Passing None to all three
    # is what actually unregisters the routes; leaving openapi_url set would
    # keep the schema fetchable even with the UIs disabled.
    docs_url="/docs" if settings.expose_api_docs else None,
    redoc_url="/redoc" if settings.expose_api_docs else None,
    openapi_url="/openapi.json" if settings.expose_api_docs else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Backstop timeout so a handler that wedges pre-response can't pin a connection
# open forever. Added first → runs outermost (wraps every other middleware).
if settings.request_timeout_seconds and settings.request_timeout_seconds > 0:
    app.add_middleware(TimeoutMiddleware, timeout_seconds=settings.request_timeout_seconds)

# CORS — driven by ALLOWED_ORIGINS (comma-separated). Mobile clients don't
# enforce CORS, but the PMS is a browser app on a different origin to this API,
# so for StayOS this list is the difference between the front desk working and
# every request failing a preflight.
#
# **The empty-list fallback is `*` only outside production.** It exists so a
# fresh checkout runs without configuration; shipping it would let any page on
# the internet make credentialed calls to a hotel's API. In production an empty
# list is a misconfiguration worth failing loudly on, at boot, rather than
# discovering as a security finding later.
_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
_is_production = settings.app_env.lower().startswith("prod")

if not _origins:
    if _is_production:
        raise RuntimeError(
            "ALLOWED_ORIGINS is empty and APP_ENV is production. Refusing to "
            "start with a wildcard CORS policy — set it to the exact origins, "
            "e.g. https://partner.travbuddy.in,https://travbuddy.in"
        )
    _origins = ["*"]
elif _is_production and "*" in _origins:
    raise RuntimeError(
        "ALLOWED_ORIGINS contains '*' and APP_ENV is production. Name the "
        "origins explicitly; a wildcard here lets any site call this API."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(router_v3)


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
