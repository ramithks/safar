"""
Sync — what changed for this property since the PMS last looked.

The PWA holds a bounded working set in IndexedDB and refreshes it by merging.
Merging adds and updates; it cannot remove. So this endpoint returns two things
that are not the same shape:

  changed  rows whose `updated_at` is past the cursor — merged as usual
  deleted  ids that no longer exist — removed, which nothing else can do

and one thing that matters more than either:

  fullResync  the server can no longer account for everything this cursor
              missed, so throw the local copy away and start again

That last flag is the difference between a sync protocol and a hopeful one.
Tombstones are pruned after 90 days; a device that was in a drawer for longer
has no way to discover the deletions it slept through, and quietly returning an
incomplete list would leave it permanently wrong with nothing to show for it.
The honest answer is "I don't know what you missed".

The cursor is the server's own clock, echoed back from the previous response,
never the client's. A front desk PC with a wrong clock would otherwise skip
every change in the gap.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# Per entity, per call. A property that changed more than this since the cursor
# is one where a full resync is cheaper than paging anyway.
PAGE = 1000

# The same window `src/data/db.ts` prunes to. Syncing wider would ship rows the
# client deletes on arrival; syncing narrower would leave holes it never fills.
# These two numbers have to stay equal to the ones over there.
WORKING_SET_PAST_DAYS = 2
WORKING_SET_FUTURE_DAYS = 14


async def changes_since(property_id: str, since: str | None) -> dict:
    def _read():
        db = get_db()
        # Taken before the reads, not after. A row written during this function
        # would otherwise fall between the cursor the client stores and the data
        # it received, and be missed until something else touched it.
        server_time = datetime.now(timezone.utc).isoformat()

        def _table(name: str, column: str = "updated_at"):
            q = db.table(name).select("*").eq("property_id", property_id)
            if since:
                q = q.gt(column, since)
            return q.order(column).limit(PAGE).execute().data or []

        today = date.today()
        floor = (today - timedelta(days=WORKING_SET_PAST_DAYS)).isoformat()
        ceiling = (today + timedelta(days=WORKING_SET_FUTURE_DAYS)).isoformat()

        # Half-open overlap with the working-set window: a stay belongs if it
        # has not ended before the window and has not started after it. A stay
        # edited into or out of the window still moves its updated_at, so the
        # cursor catches the arrival and the client's own prune handles the exit.
        res_q = (
            db.table("reservations")
            .select("*")
            .eq("property_id", property_id)
            .gte("check_out", floor)
            .lte("check_in", ceiling)
        )
        if since:
            res_q = res_q.gt("updated_at", since)
        reservations = res_q.order("updated_at").limit(PAGE).execute().data or []

        rooms = _table("rooms")
        tasks = _table("tasks")
        # Append-only, so there is no updated_at to move — created_at is the
        # only time a folio line has.
        folio = _table("folio_entries", "created_at")

        tombstones = db.rpc(
            "sync_tombstones_since",
            {"p_property": property_id, "p_since": since, "p_limit": 2000},
        ).execute().data or []

        horizon = db.rpc(
            "sync_tombstone_horizon", {"p_property": property_id}
        ).execute().data

        return server_time, reservations, rooms, tasks, folio, tombstones, horizon

    server_time, reservations, rooms, tasks, folio, tombstones, horizon = await run_db(_read)

    deleted: dict[str, list[str]] = {}
    for t in tombstones:
        deleted.setdefault(t["entity"], []).append(t["entity_id"])

    # Two ways the answer can be incomplete, and both mean the same thing to the
    # client, so they are reported as one flag rather than as a puzzle to solve.
    truncated = any(len(rows) >= PAGE for rows in (reservations, rooms, tasks, folio))
    behind_horizon = bool(since and horizon and str(horizon) > since)

    return {
        "serverTime": server_time,
        "fullResync": bool(since is None or truncated or behind_horizon),
        "reservations": reservations,
        "rooms": rooms,
        "tasks": tasks,
        "folioEntries": folio,
        "deleted": deleted,
    }
