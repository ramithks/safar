"""
Every Razorpay event, once.

Razorpay retries — on a non-2xx, on a timeout, and sometimes on neither. Every
handler downstream of this is idempotent on its own key, which is the real
protection; this is the cheaper one in front of it, and it exists for two
reasons beyond that.

**It makes a redelivery a lookup rather than a re-execution that has to be
safe.** Those are different claims. The second is true today and is one
refactor away from not being.

**It is the reconciliation log.** When a hotel's state and Razorpay's disagree,
the only question worth asking is "what did they send us, and when". Nothing
else in this system can answer it, and by the time anybody needs the answer the
event is gone from Razorpay's dashboard.

Recording is best-effort and never blocks a handler: an event we could not log
but did process correctly is a gap in an audit trail, and an event we refused to
process because we could not log it is a hotel that did not get its credits.
"""
import logging

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


def _claim(event_id: str, name: str, payload: dict,
           subscription_id: str | None, payment_id: str | None,
           order_id: str | None) -> bool:
    """
    Insert the event. False means we have seen it before.

    Insert-and-catch rather than select-then-insert: the window between the two
    is exactly where Razorpay's near-simultaneous retries land, and the unique
    index is the only thing that closes it.
    """
    try:
        get_db().table("razorpay_webhook_events").insert({
            "event_id": event_id,
            "event_name": name,
            "subscription_id": subscription_id,
            "payment_id": payment_id,
            "order_id": order_id,
            # Trimmed: a webhook body carries the whole entity and we want the
            # shape for reconciliation, not a copy of Razorpay's database.
            "payload": payload,
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "duplicate key" in message or "23505" in message:
            # Seen before — but "seen" and "finished" are not the same thing.
            #
            # A row this handler released after failing to fulfil is one
            # Razorpay is *supposed* to send again, and answering "already
            # seen" to that retry is what made `payments.release` unreachable.
            # `_reclaim` is a conditional UPDATE, so of two simultaneous
            # retries exactly one is handed the work. See `release` below.
            return _reclaim(event_id)
        # Anything else — a dropped connection, a pool timeout — must not stop
        # the event being handled. Logged and treated as new: handling twice is
        # safe by construction, and not handling at all is not.
        logger.warning("could not log webhook %s: %s", event_id, type(exc).__name__)
        return True


async def claim(
    event_id: str | None,
    name: str,
    payload: dict,
    *,
    subscription_id: str | None = None,
    payment_id: str | None = None,
    order_id: str | None = None,
) -> bool:
    """
    True if this event is new and should be handled.

    An event with no id is always handled. Razorpay sends `x-razorpay-event-id`
    on everything current, but a missing header must mean "process it" rather
    than "drop it" — the downstream keys are what actually protect us, and
    silently discarding money-bearing events because a header changed shape is
    the failure nobody would find.
    """
    if not event_id:
        return True
    return await run_db(
        _claim, event_id, name, payload or {}, subscription_id, payment_id, order_id,
    )


def _mark(event_id: str, status: str, error: str | None) -> None:
    get_db().table("razorpay_webhook_events").update(
        {"status": status, "error": (error or "")[:400] or None}
    ).eq("event_id", event_id).execute()


async def mark(event_id: str | None, status: str, error: str | None = None) -> None:
    """
    How it went. Best-effort by design — see the module note.

    A `failed` row is the one an operator goes looking for: it is an event
    Razorpay will retry, and knowing it has already failed three times is the
    difference between waiting and intervening.
    """
    if not event_id:
        return
    try:
        await run_db(_mark, event_id, status, error)
    except Exception:  # noqa: BLE001
        logger.debug("could not mark webhook %s as %s", event_id, status)


# ── giving an event back ─────────────────────────────────────────────────────
#
# The gap this closes, found by reading the two halves together rather than
# separately.
#
# `payments.release()` hands a claimed order back to 'created' when fulfilment
# fails, and the handler then answers 500 so that Razorpay — which retries for
# twenty-four hours — comes back and finishes the job. That is the whole
# recovery story for "the guest paid and the room sold in the seconds between".
#
# It could not work. `claim()` above inserts the event id, and Razorpay's retry
# carries **the same** `x-razorpay-event-id`. So the retry hit the unique index,
# read as "already seen", and was answered 200. The order sat at 'created' with
# a failure_reason and nobody ever came back for it: a guest charged, with no
# booking, permanently, and a 200 in the log saying everything was fine.
#
# The dedup log has to be released alongside the order it guarded. Not deleted —
# the row is the reconciliation record, and losing it is losing the only
# evidence of what Razorpay actually sent — so it is marked `failed`, and
# `_claim` treats a `failed` row as claimable again.
#
# The reclaim is a conditional UPDATE for the same reason `payments._claim` is:
# two retries arriving together must produce one handler, and `where status =
# 'failed'` is Postgres deciding rather than us hoping.

RECLAIMABLE = "failed"


def _reclaim(event_id: str) -> bool:
    rows = (
        get_db().table("razorpay_webhook_events")
        .update({"status": "retrying"})
        .eq("event_id", event_id).eq("status", RECLAIMABLE)
        .execute().data or []
    )
    return bool(rows)


async def release(event_id: str | None, reason: str) -> None:
    """
    Mark an event as needing Razorpay to send it again.

    Called on exactly the paths that answer non-2xx. An event left `processed`
    after a handler failed is an event that can never be retried, which is the
    bug this exists for.
    """
    if not event_id:
        return
    await mark(event_id, RECLAIMABLE, reason)
