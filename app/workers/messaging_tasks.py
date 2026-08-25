"""
The worker that answers guests.

One task, and the interesting part is what it does *not* do: it does not parse a
webhook, does not verify a signature and does not persist a message. All of that
happened in the request, before this was enqueued, which is what makes a Meta
retry harmless — the retry finds the message already recorded and never reaches
here.

So this task's only job is: take the conversation lock, answer everything
outstanding, release. It is safe to run twice, safe to run late, and safe to
lose — a task that never runs leaves a guest unanswered in an inbox a human can
see, which is a bad afternoon rather than a corrupted hotel.
"""
import asyncio
import logging

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


# How long a task waits for a busy conversation before giving up, and how.
#
# **This has to exceed `pipeline.LOCK_TTL_SECONDS`, and it did not.** The task
# was declared `max_retries=3` and the busy branch asked for `max_retries=10`;
# Celery checks the *decorator's* number, so the real budget was three attempts
# three seconds apart — nine seconds — against a lock a holder may legitimately
# keep for ninety while it answers up to eight messages with a real model.
#
# The consequence was a guest message dropped in silence: the waiting task
# exhausted its retries, `MaxRetriesExceededError` was then caught by
# `autoretry_for=(Exception,)` and retried into the same wall, and the message
# stayed `agent_run_id is null` with nobody coming back. It showed up as
# `celery_ordering_e2e` reporting "1 left" and "2 runs for 3 messages" and was
# filed for weeks as an unexplained flake.
#
# 24 × 5s = 120s, comfortably past the 90s TTL: if the holder is alive we wait
# it out, and if it died the lock expires and we take it.
BUSY_RETRY_SECONDS = 5
BUSY_MAX_RETRIES = 24


@celery_app.task(
    name="travbuddy.messaging.process_conversation",
    bind=True,
    # Retried by us, not by Celery's default. A model call that failed once will
    # usually fail again immediately; the backoff is what makes the second
    # attempt worth making.
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_backoff_max=120,
    # Must cover the busy-lock wait above, because Celery enforces this number
    # rather than the one passed to `self.retry()`.
    max_retries=BUSY_MAX_RETRIES,
    acks_late=True,
)
def process_conversation_task(self, conversation_id: str, request_id: str | None = None) -> dict:
    """
    `request_id` is the webhook's, handed across the queue.

    Without it the two halves of one guest message live in different processes
    with no shared id, and "why didn't this booking confirm?" needs a timestamp
    range and the hope that no other hotel was busy. `span` restores the
    previous context on exit, which matters here: one worker handles many
    conversations, and a leaked id would label the next hotel's lines with the
    last one's.
    """
    from app.services import trace
    from app.services.messaging.pipeline import conversation_lock, process_conversation

    with trace.span(request_id=request_id, conversation_id=conversation_id), \
            conversation_lock(conversation_id) as acquired:
        if not acquired:
            # Somebody else is answering this conversation. Come back rather
            # than give up.
            #
            # Returning here was a real bug: the holder computed its work list
            # before this message was persisted, so nobody ever answered it and
            # the guest sat in silence with no error recorded anywhere. The
            # drain now re-queries, which fixes it from the other side — this
            # retry is the belt to that pair of braces, and it is what covers
            # the case where the holder finishes in the gap between its last
            # query and releasing the lock.
            if self.request.retries < BUSY_MAX_RETRIES:
                logger.debug("conversation %s is busy, retrying shortly", conversation_id)
                raise self.retry(countdown=BUSY_RETRY_SECONDS,
                                 max_retries=BUSY_MAX_RETRIES)

            # Two minutes of a conversation held by somebody else is not a busy
            # hotel, it is something stuck. The guest gets a person rather than
            # silence — the same degradation every other dead end on this path
            # takes — and it is logged at error because nothing should reach it.
            logger.error(
                "conversation %s was locked for %ss; handing it to a human",
                conversation_id, BUSY_MAX_RETRIES * BUSY_RETRY_SECONDS,
            )
            asyncio.run(_hand_to_human(conversation_id))
            return {"processed": 0, "reason": "conversation locked too long"}

        return asyncio.run(process_conversation(conversation_id))


async def _hand_to_human(conversation_id: str) -> None:
    """
    Give up on answering, visibly.

    An unanswered guest must always end up in front of a person. Failing to
    acquire the lock for two minutes is the one path here that produces no run,
    no action and no reply, so it is the one that most needs to leave a trace.
    """
    from app.db.client import get_db, run_db
    from app.services.messaging import service as messaging

    def _property_of():
        rows = (
            get_db().table("conversations").select("property_id")
            .eq("id", conversation_id).limit(1).execute().data or []
        )
        return rows[0]["property_id"] if rows else None

    try:
        property_id = await run_db(_property_of)
        if property_id:
            await messaging.request_human(conversation_id, property_id)
    except Exception:  # noqa: BLE001 — a failed handoff must not mask the cause
        logger.exception("could not hand conversation %s to a human", conversation_id)


@celery_app.task(
    name="travbuddy.messaging.sweep_waiting_guests",
    bind=True,
    # No retry. This runs again on its own in a few minutes and the next run
    # sees the same waiting guests — a retry would only bring the notification
    # forward by less than the sweep interval, at the cost of a second pass over
    # every hotel while the first one's failure is still being logged.
    max_retries=0,
    acks_late=True,
)
def sweep_waiting_guests_task(self) -> dict:
    """
    Tell each hotel about the guests nobody came back to.

    Routed to the `hotelos` queue by `travbuddy.messaging.*`, which is what
    makes it read the right database: it must never be executed by the
    marketplace's worker, which has no `conversations` table.

    **Enqueued by the StayOS beat, not the marketplace's.** This docstring used
    to say the marketplace beat was "the only deployment that runs one", and
    that sentence is why `agent_escalations` was empty for the whole life of the
    feature: the marketplace beat enqueues onto the marketplace broker, StayOS
    brings up its own Redis, and the StayOS worker was listening to a queue
    nothing was writing to. Routing was correct and irrelevant — the message
    never crossed. See `celery_app._beat_schedule` and
    `tests/test_escalation_beat.py`, which runs this function rather than
    inspecting the configuration around it.
    """
    import asyncio

    from app.services import trace
    from app.services.pms import waiting

    with trace.span(request_id=f"sweep-{self.request.id}"):
        try:
            return asyncio.run(waiting.sweep())
        except Exception:  # noqa: BLE001 — a scheduled task that raises is a
            # red task in a dashboard nobody watches. A logged failure is one
            # somebody can search for, and the next sweep is five minutes away.
            logger.exception("the waiting-guest sweep failed")
            return {"examined": 0, "waiting": 0, "raised": 0, "notified": 0,
                    "error": "sweep failed"}


# ── inventory drift, noticed rather than discovered ─────────────────────────

@celery_app.task(
    name="travbuddy.pms.reconcile_inventory",
    # No retry. It runs again in an hour and the next run sees the same drift —
    # a retry would only report it twice.
    max_retries=0,
    acks_late=True,
)
def reconcile_inventory_task() -> dict:
    """
    Does `room_inventory` still agree with the reservations that exist?

    **The reconciliation existed and nothing ran it.** `scripts/reconcile_
    inventory.py` has found real drift twice — 377 room-nights sold to nobody
    at Casa Mandala, then eighteen units oversold from fixture rows that were
    INSERTed straight into `reservations` and never moved the counter. Both were
    found because somebody happened to run it by hand.

    Drift is the one class of defect here that does not announce itself. An
    oversold night sells a room the hotel does not have and nobody learns until
    two guests arrive for it; a stranded night quietly takes a sellable room off
    the market and looks like a slow week.

    **It reports and never corrects.** Recomputing the counter from live
    reservations is the right answer for a *report* and a dangerous one for a
    write: a hold in flight, a webhook mid-delivery and a booking being made in
    the same second all look like disagreement for a moment, and a task that
    "fixed" them would take a room off a guest who is paying for it. So this
    logs, loudly and structurally, and a person decides.
    """
    import logging
    import sys
    from pathlib import Path

    from app.db.client import get_db

    logger = logging.getLogger(__name__)
    scripts = Path(__file__).resolve().parent.parent.parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from reconcile_inventory import disagreements  # noqa: E402

    db = get_db()
    try:
        properties = (
            db.table("properties").select("id,name")
            .eq("status", "active").execute().data or []
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not list properties to reconcile", exc_info=True)
        return {"checked": 0, "drifting": 0}

    drifting = []
    for row in properties:
        try:
            found = disagreements(db, row["id"])
        except Exception:  # noqa: BLE001
            # One property that cannot be read must not stop the rest being
            # checked — the whole point is the sweep across all of them.
            logger.warning("could not reconcile %s", row["id"], exc_info=True)
            continue

        if not (found.get("oversold") or found.get("stranded") or found.get("wrong")):
            continue

        drifting.append(row["id"])
        logger.error(
            "[inventory-drift] %s",
            {
                "property_id": row["id"],
                "property": row.get("name"),
                # Rooms sold that nothing is counting. The dangerous direction:
                # the hotel can sell them again.
                "oversold": found.get("oversold"),
                # The counter says sold and no live reservation agrees. Rooms
                # the hotel has taken off the market for nobody.
                "stranded": found.get("stranded"),
                "nights_affected": len(found.get("wrong") or []),
            },
        )

    logger.info("reconciled %d properties, %d drifting", len(properties), len(drifting))
    return {"checked": len(properties), "drifting": len(drifting),
            "properties": drifting}
