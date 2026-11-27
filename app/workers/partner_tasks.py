"""
Housekeeping for partner commercial terms.

Exactly one job so far: moving partners off an introductory commission rate when
it runs out. "0% for three months, then 10%" is one code and two rates, and
something has to notice the calendar.

Worth being precise about what this job is NOT. It does not keep billing
correct — `partner_terms.resolve` already reports the reverted rate the instant
the clock passes, so a booking made an hour after an intro period ends is
charged the real rate whether or not this has run. What it fixes is the
DISAGREEMENT: the stored row and the partner's listings still showing 0% while
they are being charged 10%. A partner reading their own dashboard should see
what we are actually doing.

That ordering is deliberate. The alternative — the job as the thing that ends
the intro period — means a cron that skips a night quietly extends a discount to
every partner on one, and nobody finds out until the numbers are reconciled.
"""
import asyncio
import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="safar.partners.revert_intro_commission", max_retries=0)
def revert_intro_commission_task() -> dict:
    """
    Move every partner whose intro rate has lapsed onto their base rate.

    Idempotent: a partner already reverted has no `commission_expires_at` left
    to match, so a re-run finds nothing. Safe to fire as often as you like.
    """
    from app.services import partner_terms

    moved = asyncio.run(partner_terms.revert_expired())
    if moved:
        logger.info("[terms] intro commission ended for %s partner(s)", moved)
    return {"reverted": moved}
