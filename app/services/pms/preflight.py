"""
What StaySuite needs configured, checked out loud at boot.

**The failure this exists for is a silence, not an error.** `REVALIDATE_SECRET`
was never set on the StaySuite backend. `seo_ping.hotel_changed` handles that
correctly — it skips, logs at `debug`, and never fails a hotelier's publish —
and the consequence was that every hotel that published sat stale on the website
for up to an hour, for a day, with nothing anywhere saying why. The behaviour was
right. The volume was wrong.

Two failures with the same shape were already in the deployment notes:
`GCS_BUCKET_NAME` (photo upload 503s, which reads to a hotelier as their
connection) and `STAYSUITE_API_BASE_URL` on the website (every hotel page 404s).

So: a **degradation** is announced at WARNING with the variable's name and the
sentence describing what stops working. A **hard requirement** stops the boot,
because a StaySuite process with no database is not a degraded StaySuite.

Deliberately not a health endpoint. A check nobody looks at is the same as no
check — this runs once, at startup, into the log a deploy tails anyway.

Nothing here reads a secret's *value* into a log line. It says whether something
is set, never what it is set to.
"""
import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Requirement:
    env: str
    # The attribute on `settings`, which is not always the lower-cased env name.
    attr: str
    # What stops working without it, in a sentence somebody deploying can act
    # on. Not "missing config" — that tells nobody what to do.
    consequence: str
    # True when the process should refuse to start rather than run degraded.
    fatal: bool = False


REQUIREMENTS = (
    Requirement(
        env="SUPABASE_URL", attr="supabase_url", fatal=True,
        consequence="StaySuite has no database; nothing will work.",
    ),
    Requirement(
        env="SUPABASE_SERVICE_KEY", attr="supabase_service_key", fatal=True,
        consequence="StaySuite cannot write; every booking and message will fail.",
    ),
    Requirement(
        env="GCS_BUCKET_NAME", attr="gcs_bucket_name",
        consequence=(
            "Hotels cannot upload room photographs. The upload answers 503, "
            "which a hotelier reads as their own connection — and a hotel with "
            "no photos is noindex on its own website."
        ),
    ),
    Requirement(
        env="REVALIDATE_SECRET", attr="revalidate_secret",
        consequence=(
            "Publishing a hotel will not purge its pages on safar.example.com. The "
            "site stays stale for up to an hour and the sitemap for up to a "
            "day, with nothing telling the hotelier why."
        ),
    ),
    Requirement(
        env="INSTAGRAM_APP_SECRET", attr="instagram_app_secret",
        consequence=(
            "Every Instagram webhook is rejected as unsigned. Guests message "
            "the hotel and nothing arrives."
        ),
    ),
    Requirement(
        env="INSTAGRAM_VERIFY_TOKEN", attr="instagram_verify_token",
        consequence="Meta's subscription handshake cannot be completed.",
    ),
    Requirement(
        env="RAZORPAY_WEBHOOK_SECRET", attr="razorpay_webhook_secret",
        consequence=(
            "Payment webhooks answer 503. Guests pay and no booking is ever "
            "confirmed — the browser callback is the only path left, and it "
            "does not exist for a guest who closed the tab."
        ),
    ),
    Requirement(
        env="SUPABASE_JWT_SECRET", attr="supabase_jwt_secret",
        consequence=(
            "Hoteliers cannot sign in to the partner app; every authenticated "
            "route answers 401."
        ),
    ),
)


def check() -> list[Requirement]:
    """
    Say what is missing. Returns the degradations, raises on a fatal gap.

    Called from the lifespan only when `enable_hotel_os` is on: a marketplace
    box has none of these and should not be told about them.
    """
    missing = [r for r in REQUIREMENTS if not getattr(settings, r.attr, None)]
    fatal = [r for r in missing if r.fatal]

    if fatal:
        names = ", ".join(r.env for r in fatal)
        for requirement in fatal:
            logger.critical("[staysuite] %s is not set — %s",
                            requirement.env, requirement.consequence)
        raise RuntimeError(f"StaySuite cannot start without: {names}")

    for requirement in missing:
        # WARNING, and naming the variable. The whole point: a degradation
        # nobody is told about is one that lasts a day.
        logger.warning("[staysuite] %s is not set — %s",
                       requirement.env, requirement.consequence)

    if not missing:
        logger.info("[staysuite] configuration complete: %d required variables present",
                    len(REQUIREMENTS))
    else:
        logger.warning("[staysuite] running degraded: %d of %d variables missing (%s)",
                       len(missing), len(REQUIREMENTS),
                       ", ".join(r.env for r in missing))
    return missing
