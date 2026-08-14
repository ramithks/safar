"""
Clearing up after an E2E run.

Every real-database suite here creates reservations against one shared property
and none of them removed anything. That was invisible for a while, because the
assertions that mattered were differential — "is *my* stay on the arrivals
list" — and a hundred extra bookings do not change that answer.

It stopped being invisible at `pms_e2e`, which asserts on numbers that are
properties of the *hotel* rather than of the run:

    /today computes occupancy server-side   →  261.9%

Casa Mandala has 42 units and 110 of them were sold, because five suites had
each left a fortnight of test stays behind on 2026-08-14. Nothing was broken.
The number was correct, arithmetic and all, about a property that had been
quietly turned into a fiction.

So each suite sweeps its own fixtures, by the reference prefix it writes. Two
rules make that safe:

**Prefixes, never dates or counts.** A sweep that deletes "old" rows deletes
whatever a colleague was halfway through debugging. `TB-AI…` is unambiguously
this suite's and unambiguously nobody's booking.

**At the start as well as the end.** A run that dies in the middle leaves its
rows behind, and the run that finds them should not have to inherit them.
Sweeping only on exit means one crash poisons the property until somebody
notices — which is exactly how it got to 110.

Rooms are swept the same way, by `frontdesk_e2e` alone, because it is the only
suite that creates them and rooms are the denominator.
"""
import logging

logger = logging.getLogger(__name__)


def sweep_reservations(db, property_ids: list[str], prefixes: list[str]) -> int:
    """
    Delete every reservation whose ref starts with one of these prefixes.

    Cascades to `reservation_nights` and `folio_entries` by foreign key, and
    nulls `tasks.room_id` / `conversations.reservation_id` rather than taking
    them with it. Returns how many went.

    Deleted one at a time rather than with an `in_` filter: PostgREST's delete
    needs a filter it can express, the lists are small, and a loop that fails
    halfway has still removed something rather than nothing.
    """
    doomed: list[dict] = []
    for prefix in prefixes:
        doomed += (
            db.table("reservations").select("id,ref")
            .in_("property_id", property_ids)
            .like("ref", f"{prefix}%")
            .limit(500).execute().data or []
        )

    gone = 0
    for row in doomed:
        try:
            db.table("reservations").delete().eq("id", row["id"]).execute()
            gone += 1
        except Exception:  # noqa: BLE001
            # A stay something else still points at. Left alone: a sweep that
            # fails the suite it is tidying up after is worse than a stray row.
            logger.debug("could not sweep reservation %s", row["ref"], exc_info=True)
    return gone


def announce(label: str, stays: int, rooms: int = 0) -> None:
    if stays or rooms:
        bits = [f"{stays} stays"] + ([f"{rooms} rooms"] if rooms else [])
        print(f"  · swept {' and '.join(bits)} left by previous {label} runs")
