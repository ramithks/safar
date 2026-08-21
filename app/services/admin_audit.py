"""
Admin audit log.

Every privileged action writes one row before the event fires. This is the
record of what happened and who did it — the thing you go looking for months
later when a partner says "I never agreed to that" or money went somewhere
unexpected.

Deliberately NOT an event subscriber: subscribers are best-effort and isolated,
which is right for email and wrong for an audit trail. The endpoint awaits this
explicitly.

A failed write is logged loudly but does not fail the action — the database
change has already committed at that point, and refusing to return success
would leave the admin thinking nothing happened when it did.

**But it must not be reported as if it had worked.** `record()` returns whether
the row landed, and a caller that moves money is expected to say so out loud:
for a year the StayOS deployment had no `admin_audit_log` table at all, every
credit grant logged "[audit] FAILED to record", and the operator's screen said
"1,000 credits granted" with no hint that nothing had been written down. The
loud log was doing its job and nobody was reading it. Now the person who took
the action is told, because they are the one who can act on it.
"""
from __future__ import annotations

import logging
from typing import Any

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


async def record(
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    entity_label: str | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    ip: str | None = None,
) -> bool:
    """
    Append one audit row. Returns whether it landed.

    `entity_label` is stored as a snapshot on purpose — six months from now the
    listing may be renamed or gone, and an id alone tells you nothing.
    """
    row = {
        "actor": actor,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_label": (entity_label or "")[:200] or None,
        "reason": (reason or "")[:500] or None,
        "metadata": metadata or {},
        "ip": ip,
    }

    try:
        # supabase-py is sync; run_db keeps it off the event loop so an audit
        # write can't stall every other in-flight request.
        await run_db(lambda: get_db().table("admin_audit_log").insert(row).execute())
        return True
    except Exception as exc:  # noqa: BLE001 — never fail a committed action
        logger.error(
            "[audit] FAILED to record %s by %s on %s (%s): %s",
            action, actor, entity_type, entity_id, exc,
        )
        return False


def recent(limit: int = 200, action: str | None = None) -> list[dict]:
    """Read the trail back, newest first."""
    try:
        q = get_db().table("admin_audit_log").select("*")
        if action:
            q = q.eq("action", action)
        return q.order("created_at", desc=True).limit(min(limit, 500)).execute().data or []
    except Exception as exc:  # noqa: BLE001
        logger.exception("[audit] read failed: %s", exc)
        return []
