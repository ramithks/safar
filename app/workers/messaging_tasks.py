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


@celery_app.task(
    name="travbuddy.messaging.process_conversation",
    # Retried by us, not by Celery's default. A model call that failed once will
    # usually fail again immediately; the backoff is what makes the second
    # attempt worth making.
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_backoff_max=120,
    max_retries=3,
    acks_late=True,
)
def process_conversation_task(conversation_id: str) -> dict:
    from app.services.messaging.pipeline import conversation_lock, process_conversation

    with conversation_lock(conversation_id) as acquired:
        if not acquired:
            # Somebody else is answering this conversation. Their drain will
            # pick up whatever arrived while they were working, because it
            # reads pending messages rather than the one it was handed.
            logger.debug("conversation %s is already being processed", conversation_id)
            return {"processed": 0, "reason": "locked"}

        return asyncio.run(process_conversation(conversation_id))
