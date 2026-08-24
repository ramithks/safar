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


def cancel_reservation(db, reservation_id: str, *, reason: str = "e2e") -> bool:
    """
    Cancel a test booking the way the product does: stock first, status second.

    **Use this instead of `update reservations set status = 'cancelled'`.**

    `room_inventory.units_booked` is owned by the RPCs — `convert_hold` adds to
    it, `release_booking` subtracts — and an UPDATE that sets a reservation to
    'cancelled' goes past both. No trigger catches it. The counter moves one way
    only, and the effect is a night that reads as full with nobody in it, which
    is invisible until somebody tries to sell it and invisible *again* then,
    because "no rooms available" is a perfectly ordinary answer.

    On Casa Mandala it reached **377 room-nights sold to nobody**, and the
    replay could no longer sell the room its own script is about. Two sessions
    went looking for the regression that explained it. There was none: the
    suites had been eating the property they run on, one run at a time.

    Released first and cancelled second, deliberately. A crash in between leaves
    stock released against a booking still marked confirmed — visible to the
    night audit and fixable by a later sweep. The other order leaves the
    invisible failure, which is the one that took two sessions to find.

    Suites that want the *product's* cancellation — the policy, the refund, the
    folio line — should call `app.services.pms.cancellation.cancel` instead.
    This exists for the suites that only need the row to read as dead, and its
    single job is that they cannot corrupt inventory doing it.
    """
    rows = (
        db.table("reservations")
        .select("id,status,room_type_id,check_in,check_out,units")
        .eq("id", reservation_id).limit(1).execute().data or []
    )
    if not rows:
        return False
    row = rows[0]
    if row["status"] in ("cancelled", "no_show"):
        # Already dead. Releasing again would subtract a second time and put
        # inventory *above* what the hotel has, which is the same corruption
        # from the other end.
        return False

    if row["status"] == "confirmed" and row.get("room_type_id"):
        db.rpc("release_booking", {
            "p_room_type": row["room_type_id"],
            "p_check_in": str(row["check_in"]),
            "p_check_out": str(row["check_out"]),
            "p_units": row.get("units") or 1,
        }).execute()

    db.table("reservations").update({
        "status": "cancelled", "cancellation_reason": reason,
    }).eq("id", reservation_id).execute()
    return True


def cancel_reservation_sql(reservation_id: str, reason: str = "e2e") -> str:
    """
    The same rule, for the suites that talk to the management SQL endpoint.

    `b2b_e2e` and `creator_e2e` have no Supabase client — they drive everything
    through `sql()` against the management API — so they cannot call
    `cancel_reservation` above. Handing them a *string* rather than asking them
    to grow a database client keeps the rule in one place and keeps the suites
    in the style they are written in.

    Two statements, release first. Guarded on `status = 'confirmed'` inside the
    SQL rather than in Python, so it is safe to run twice: a second call finds
    the row already cancelled and releases nothing.
    """
    return f"""
        select public.release_booking(r.room_type_id, r.check_in, r.check_out, r.units)
          from public.reservations r
         where r.id = '{reservation_id}'
           and r.status = 'confirmed'
           and r.room_type_id is not null;
        update public.reservations
           set status = 'cancelled', cancellation_reason = '{reason}'
         where id = '{reservation_id}';
    """


def reinstate_reservation_sql(reservation_id: str, status: str = "confirmed") -> str:
    """
    Put a cancelled test booking back, stock included.

    The mirror of the above, and it exists because `creator_e2e` cancels a stay
    to prove a payout goes to zero and then *reinstates* it to prove the payout
    comes back. Cancelling through `release_booking` and reinstating with a bare
    UPDATE would leave the counter one room short for ever — the same
    corruption as the original bug, in the direction that oversells a night
    instead of hiding it.
    """
    return f"""
        select public.book_inventory(r.room_type_id, r.check_in, r.check_out, r.units)
          from public.reservations r
         where r.id = '{reservation_id}'
           and r.status in ('cancelled', 'no_show')
           and r.room_type_id is not null;
        update public.reservations
           set status = '{status}', cancellation_reason = null
         where id = '{reservation_id}';
    """


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
            db.table("reservations").select(
                "id,ref,room_type_id,check_in,check_out,units")
            .in_("property_id", property_ids)
            .like("ref", f"{prefix}%")
            .limit(500).execute().data or []
        )

    gone = 0
    for row in doomed:
        try:
            # Return the stock *before* the row goes, because afterwards there
            # is nothing left to compute the release from.
            #
            # `room_inventory.units_booked` is owned by the RPCs — confirm adds
            # to it, `release_booking` subtracts — and a delete goes past both.
            # No trigger catches it, because the product has no path that
            # deletes a reservation; only these suites do. So the counter went
            # one way only, and after enough runs a night that nobody had
            # booked reported itself full. That is how b2b_e2e came to pass on a
            # fresh property and fail on replay.
            #
            # Released first and deleted second, rather than the other way
            # round: a crash in between leaves stock that reads as sold, which
            # a later sweep can still fix. The opposite order leaves a night
            # oversold with no record of why.
            if row.get("room_type_id") and row.get("check_in") and row.get("check_out"):
                db.rpc("release_booking", {
                    "p_room_type": row["room_type_id"],
                    "p_check_in": row["check_in"],
                    "p_check_out": row["check_out"],
                    "p_units": row.get("units") or 1,
                }).execute()
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
