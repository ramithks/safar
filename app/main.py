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
from app.services import trace

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

# Every line from here on carries whichever of request/conversation/hotel/
# booking is known where it was emitted. Installed after basicConfig, on the
# handlers it just made — see services/trace, and §29 for why "why didn't this
# booking confirm?" has to be answerable from logs rather than from a hunch.
trace.install()

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

    # What StayOS needs, said out loud at boot.
    #
    # Same principle as the Redis line below and for the same reason: the
    # failure that cost a day was `REVALIDATE_SECRET` being unset, which
    # `seo_ping` handles correctly and reports at `debug`. The behaviour was
    # right and the volume was wrong. See services/pms/preflight.
    if settings.enable_hotel_os:
        from app.services.pms import preflight

        preflight.check()

    # Redis, said out loud at boot.
    #
    # It is the Celery broker as well as the job store, the rate limiter's
    # storage and the messaging pipeline's lock, and only one of those
    # complains when it is wrong. A worker started against an unreachable
    # broker consumes nothing and reports nothing: guest replies and campaigns
    # stop happening, silently. Naming the target here means the answer is in
    # the first ten lines of the log rather than in a health check somebody has
    # to think to call.
    #
    # Only production earns an error. On a laptop the same default is usually
    # right — Redis really is on localhost — and a red line for a working setup
    # teaches people to skim past red lines.
    from app.config import redis_is_unconfigured, redis_target

    if not redis_is_unconfigured():
        logger.info("Redis: %s", redis_target())
    elif settings.app_env.lower().startswith("prod"):
        logger.error(
            "REDIS_URL is not set — falling back to %s. In a container nothing "
            "listens there, so the job store, the rate limiter and the Celery "
            "broker are all pointed at nothing. Set REDIS_URL.",
            redis_target(),
        )
    else:
        logger.info("Redis: %s (REDIS_URL unset, using the default)",
                    redis_target())

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


@app.middleware("http")
async def _correlate(request, call_next):
    """
    One id per request, on every log line the request produces.

    Added after the timeout middleware, so it runs *inside* it: a request that
    the backstop kills should still have had a trace while it was running,
    which is the request you most want to be able to find.

    An inbound `X-Request-Id` is honoured so a trace can start at the CDN, but
    it is truncated and stripped rather than trusted — the value ends up in log
    lines, and a header that a stranger writes must not be able to inject
    newlines into them.
    """
    supplied = (request.headers.get("x-request-id") or "")[:64]
    safe = "".join(c for c in supplied if c.isalnum() or c in "-_") or None
    with trace.span(request_id=safe) as request_id:
        response = await call_next(request)
        # Echoed back so a hotelier reporting a problem can quote the id from
        # their own network tab, which is the difference between "it broke this
        # morning" and one grep.
        response.headers["X-Request-Id"] = request_id
        return response

# CORS — driven by ALLOWED_ORIGINS (comma-separated). Mobile clients don't
# enforce CORS, but the PMS is a browser app on a different origin to this API,
# so for HostOS this list is the difference between the front desk working and
# every request failing a preflight. The marketplace needs it too: lib/search.ts
# and lib/recommend.ts are called from Client Components on travbuddy.in, so
# this list is load-bearing for the traveller site as well as for hotels.
#
# **Nothing here can refuse to boot.** Every branch ends with a usable list.
# That is a deliberate reversal: the first two versions of this block raised —
# once on an empty list, once on a wildcard — and both were wrong for the same
# reason. This process also serves travbuddy.in. A misconfigured header is a
# security finding; a process that will not start is an outage for every
# traveller, every partner and both mobile apps at once, and the second is not
# a proportionate way to prevent the first.
#
# The wildcard case is the one that made this concrete. `APP_ENV` is currently
# unset in production and `ALLOWED_ORIGINS` is not in the deployment's
# environment at all, so `allowed_origins` falls back to the `"*"` default in
# config.py. Under the raising version, setting `APP_ENV=production` — on its
# own, a tidying change nobody would think twice about — took the whole site
# down at boot. Refusing the wildcard is still right; refusing it by *narrowing*
# leaves the site up while the log says exactly what happened.
# The origins this API is actually for, and the production answer to both
# misconfigurations: an unset list and a wildcard one. A deploy that forgets the
# variable, or inherits the `"*"` default, comes up *narrow* rather than either
# wide open or dead.
#
# Vercel preview deployments of the marketplace are deliberately not here. A
# preview is not production and should not be able to talk to production; if one
# needs to, name it in ALLOWED_ORIGINS for as long as that is true.
#
# `partner.travbuddy.in` is the PWA's old address and stays on this list after
# the move to `app.hostos.in`. A hotel that installed the PWA on the old origin
# serves its shell from a service worker and never sees the redirect, so it goes
# on calling this API from a host that no longer appears anywhere else in the
# configuration. Dropping it fails those desks on preflight, with nothing on
# screen to say why.
PRODUCTION_ORIGINS = [
    "https://app.hostos.in",
    "https://hostos.in",
    "https://www.hostos.in",
    "https://partner.travbuddy.in",
    "https://travbuddy.in",
    "https://www.travbuddy.in",
]


def resolve_cors_origins(configured: str, app_env: str) -> list[str]:
    """
    The list to hand CORSMiddleware, given what was configured and where we are.

    A function rather than four lines at import time so it can be tested without
    reimporting the app — this decides whether a browser can reach the API at
    all, and every wrong answer is either an outage or an exposure.
    """
    origins = [o.strip() for o in configured.split(",") if o.strip()]
    is_production = app_env.lower().startswith("prod")

    if is_production and "*" in origins:
        logger.error(
            "ALLOWED_ORIGINS contains '*' and APP_ENV is production — ignoring "
            "it and serving %s instead. A wildcard here lets any page on the "
            "internet make credentialed calls to a hotel's API. Set "
            "ALLOWED_ORIGINS explicitly; this is a refusal, not configuration.",
            ", ".join(PRODUCTION_ORIGINS),
        )
        return list(PRODUCTION_ORIGINS)

    if origins:
        return origins

    if is_production:
        logger.warning(
            "ALLOWED_ORIGINS is unset in production — defaulting to %s. Set it "
            "explicitly; this default is a safety net, not configuration.",
            ", ".join(PRODUCTION_ORIGINS),
        )
        return list(PRODUCTION_ORIGINS)

    return ["*"]


app.add_middleware(
    CORSMiddleware,
    allow_origins=resolve_cors_origins(settings.allowed_origins, settings.app_env),
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
