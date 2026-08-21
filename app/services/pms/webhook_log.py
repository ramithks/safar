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
            return False
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
