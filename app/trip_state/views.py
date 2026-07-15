"""
View layer — mutable personalization over immutable search results
(docs/persistence_architecture.md §3A).

A result row never changes; a user's *relationship* to it (hidden / favourited /
pinned / annotated) lives here, one row per (trip, result). Copilot edits like
"hide buses" or "favourite this hotel" are one upsert, not a new search.

Broad rules ("hide all Indigo") live in trip_memory (app.trip_state.memory) and
may be *materialized* here as visible=false rows tagged source='rule:<key>' for
instant UI — but the rule stays the source of truth and re-applies on re-search.

Sync DB access (run via run_db); static methods so callers hop them onto the DB
executor. Writes go through the service role.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_FIELDS = {"visible", "favorite", "selected", "score_override", "notes", "source"}


class ViewLayer:
    @staticmethod
    def set_view(trip_id: str, result_id: str, **fields) -> None:
        """Upsert one view row. Only known fields are written."""
        payload = {k: v for k, v in fields.items() if k in _FIELDS}
        payload.update({
            "trip_id": trip_id,
            "result_id": result_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        from app.db.client import get_db
        (get_db().table("trip_result_views")
         .upsert(payload, on_conflict="trip_id,result_id").execute())

    @staticmethod
    def load(trip_id: str) -> dict[str, dict]:
        """All view rows for a trip, keyed by result_id."""
        from app.db.client import get_db
        res = (get_db().table("trip_result_views")
               .select("result_id, visible, favorite, selected, score_override, notes, source")
               .eq("trip_id", trip_id).execute())
        return {r["result_id"]: r for r in (res.data or [])}

    @staticmethod
    def materialize_rule(trip_id: str, result_ids: list[str], key: str,
                         visible: bool = False) -> int:
        """Apply a broad memory rule as hidden/visible flags on specific results,
        tagged with the rule key so a re-search can rebuild them. Best-effort."""
        if not result_ids:
            return 0
        from app.db.client import get_db
        now = datetime.now(timezone.utc).isoformat()
        rows = [{"trip_id": trip_id, "result_id": rid, "visible": visible,
                 "source": f"rule:{key}", "updated_at": now} for rid in result_ids]
        (get_db().table("trip_result_views")
         .upsert(rows, on_conflict="trip_id,result_id").execute())
        return len(rows)


async def set_view(trip_id: str, result_id: str, **fields) -> bool:
    """Async best-effort wrapper for copilot tool handlers."""
    try:
        from app.db.client import run_db
        await run_db(lambda: ViewLayer.set_view(trip_id, result_id, **fields))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[views] set_view failed for %s/%s: %s", trip_id, result_id, exc)
        return False
