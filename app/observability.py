"""
Shared Sentry initialization for both the API and the Celery worker.

Both processes call init_sentry() so worker-side errors (pipeline crashes,
frame-upload failures, etc.) reach the same Sentry project as the API. Before
this, Sentry was only initialized in app/main.py, so nothing the worker did was
ever reported.
"""
import logging

import sentry_sdk

from app.config import settings

# DSN is pinned here; SENTRY_DSN env var overrides if set.
_SENTRY_DSN = (
    settings.sentry_dsn
    or "https://fe1488d1895bed2fc5f0435e4df3d136@o4511167749029888.ingest.us.sentry.io/4511325237346304"
)

logger = logging.getLogger(__name__)


def init_sentry(component: str) -> None:
    """Initialize Sentry for the given process component ("api" | "worker").

    For the worker, CeleryIntegration is added so task failures are captured
    automatically. Safe to call more than once (e.g. once per forked worker
    child); the last init wins.
    """
    integrations = []
    if component == "worker":
        from sentry_sdk.integrations.celery import CeleryIntegration

        integrations.append(CeleryIntegration())

    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        environment=settings.app_env,
        send_default_pii=True,           # capture request headers + user IPs
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=settings.sentry_profiles_sample_rate,
        integrations=integrations,
    )
    sentry_sdk.set_tag("component", component)
    logger.info("Sentry initialized (component=%s, env=%s)", component, settings.app_env)
