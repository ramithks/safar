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
    bind=True,
    # Retried by us, not by Celery's default. A model call that failed once will
    # usually fail again immediately; the backoff is what makes the second
    # attempt worth making.
    autoretry_for=(Exception,),
    retry_backoff=5,
    retry_backoff_max=120,
    max_retries=3,
    acks_late=True,
)
def process_conversation_task(self, conversation_id: str) -> dict:
    from app.services.messaging.pipeline import conversation_lock, process_conversation

    with conversation_lock(conversation_id) as acquired:
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
            logger.debug("conversation %s is busy, retrying shortly", conversation_id)
            raise self.retry(countdown=3, max_retries=10)

        return asyncio.run(process_conversation(conversation_id))
