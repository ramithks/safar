"""
The worker that runs a campaign.

Like `messaging_tasks`, the interesting part is what it does not do. It does not
select an audience, does not check consent, does not resolve a template and does
not decide anything — all of that is `marketing.campaigns.execute`, which the
inline path calls too. This module is the *scheduling* of that work and nothing
else, so the two paths cannot drift.

Three properties make it safe to retry, and none of them lives here:

**Recipients are durable.** `prepare()` wrote a row per guest before this task
was enqueued, so a worker that dies mid-campaign leaves the remainder `pending`
and the next attempt resumes from the database rather than from memory.

**The idempotency key is a unique index.** Each recipient's key is
`campaign:guest:channel:version`, computed from things that were already fixed.
A retry recomputes the same string, `messaging.send` finds the message already
written against that `client_reference`, and returns it as a duplicate without
touching Meta.

**Cancellation is checked per recipient.** The loop reloads the campaign each
iteration, so a campaign cancelled while this task is running stops within one
message.

So the task's own job is: take the lock, run, release. Safe to run twice, safe
to run late, and safe to lose — a campaign that never runs is one a person can
see waiting in the campaign list, which is a bad afternoon rather than four
hundred duplicate messages.
"""
import asyncio
import logging
from contextlib import contextmanager

from app.config import settings
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Long enough for a few hundred sends, short enough that a crashed worker does
# not wedge a campaign for the rest of the day. The recipients are durable, so
# the cost of the lock expiring early is a second worker doing work the first
# has already made idempotent.
LOCK_TTL_SECONDS = 900


@contextmanager
def campaign_lock(campaign_id: str):
    """
    One worker per campaign. Yields False when somebody else holds it.

    The same primitive as `pipeline.conversation_lock` and for the same reason:
    this is one Redis, the critical section is bounded, and losing the lock
    costs a duplicate *attempt* rather than a duplicate message — the recipient
    index is what makes that true.

    Degrades to a no-op without Redis, with a warning. A campaign that does not
    run at all is worse than one that risks a concurrent attempt whose writes
    the database will refuse.
    """
    client = None
    key = f"tb:campaign:lock:{campaign_id}"
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_timeout=2)
        acquired = bool(client.set(key, "1", nx=True, ex=LOCK_TTL_SECONDS))
    except Exception:  # noqa: BLE001
        logger.warning("no Redis for campaign locking; running unlocked")
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired:
            try:
                client.delete(key)
            except Exception:  # noqa: BLE001
                pass  # The TTL will clear it.


@celery_app.task(
    name="travbuddy.marketing.run_campaign",
    bind=True,
    # Retried, because a campaign that stopped half-way through has a resumable
    # remainder and abandoning it leaves a hotel with a partly-sent campaign and
    # no explanation. The backoff is what makes the second attempt worth making.
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=300,
    max_retries=3,
    acks_late=True,
)
def run_campaign_task(self, campaign_id: str, property_id: str) -> dict:
    from app.services.marketing.campaigns import CampaignRefused, execute

    with campaign_lock(campaign_id) as acquired:
        if not acquired:
            # Another worker has it. Come back rather than give up: that worker
            # may be about to die, and the remainder would then be nobody's.
            logger.info("campaign %s is already running, retrying shortly", campaign_id)
            raise self.retry(countdown=30, max_retries=10)

        try:
            result = asyncio.run(execute(campaign_id, property_id))
        except CampaignRefused as exc:
            # A campaign that may not run is not a task that failed. Retrying a
            # cancelled campaign three times with backoff would be three
            # pointless wake-ups and a misleading error in the log.
            logger.info("campaign %s refused: %s %s", campaign_id, exc.code, exc.detail)
            return {"campaignId": campaign_id, "status": "refused", "code": exc.code}

        return {
            "campaignId": campaign_id,
            "status": result.status,
            "sent": result.sent,
            "skipped": result.skipped,
            "failed": result.failed,
            "blocked": result.blocked,
            "reasons": result.reasons,
        }
