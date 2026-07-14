"""
Feedback logging (Part 9) — the learning flywheel's front door.

Every impression / click / wishlist / booking is written to ``rec_feedback`` and,
for positive engagement, folded into the online session bias (app.reco.session).
All fire-and-forget: a logging failure must NEVER affect the response. Off the hot
path — callers ``asyncio.create_task`` these.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_EVENTS = {
    "impression", "click", "wishlist_add", "wishlist_remove", "book_start",
    "book_complete", "skip", "dwell", "compare_open", "cancel", "review_submit",
    "trip_complete",
}


def _rows_sync(rows: list[dict]) -> None:
    from app.db.client import get_db
    get_db().table("rec_feedback").insert(rows).execute()


async def _write(rows: list[dict]) -> None:
    if not rows:
        return
    try:
        from app.db.client import run_db
        await run_db(_rows_sync, rows)
    except Exception as exc:  # noqa: BLE001 — feedback is best-effort
        logger.debug("[reco.feedback] write failed (%d rows): %s", len(rows), exc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def log_event(
    *, user_id: str | None, item_type: str, item_id: str, event: str,
    trip_id: str | None = None, session_id: str | None = None,
    position: int | None = None, dwell_ms: int | None = None,
    context: dict | None = None, item: dict | None = None,
) -> None:
    """Log one feedback event + update session bias for positive engagement."""
    if event not in _EVENTS:
        logger.debug("[reco.feedback] unknown event %r ignored", event)
        return
    await _write([{
        "user_id": user_id, "trip_id": trip_id, "session_id": session_id,
        "item_type": item_type, "item_id": str(item_id), "event": event,
        "position": position, "dwell_ms": dwell_ms,
        "context": context or {}, "created_at": _now(),
    }])
    if session_id and item and event in ("click", "wishlist_add", "book_start", "skip", "wishlist_remove"):
        try:
            from app.reco import session
            session.record_engagement(session_id, item, event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[reco.feedback] session bias failed: %s", exc)


async def log_impressions(
    items: list[dict], *, user_id: str | None, item_type: str,
    trip_id: str | None = None, session_id: str | None = None, context: dict | None = None,
) -> None:
    """Batch-log an impression per shown item (position = rank). Feeds CTR later."""
    rows = []
    for pos, it in enumerate(items):
        iid = it.get("hotel_id") or it.get("id") or it.get("booking_token") or it.get("name")
        if not iid:
            continue
        rows.append({
            "user_id": user_id, "trip_id": trip_id, "session_id": session_id,
            "item_type": item_type, "item_id": str(iid), "event": "impression",
            "position": pos, "dwell_ms": None,
            "context": {"match": it.get("match"), **(context or {})}, "created_at": _now(),
        })
    await _write(rows)
