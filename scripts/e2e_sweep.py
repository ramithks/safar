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

# ── the sanctioned cleanup, and the invariant behind it ──────────────────────
#
# §10: *provide one sanctioned cleanup API* and *runtime-check test database
# mutations where possible*, rather than relying on one regex over test source.
#
# The regex is still there and still useful — it is what stops somebody typing
# the bad statement — but it cannot answer the question that actually matters.
# Whether a DELETE needs a release depends on whether that reservation ever
# consumed inventory: a fixture row INSERTed straight into `reservations` never
# moved the counter, and releasing one pushes `units_booked` *below* the truth,
# building an oversell out of a cleanup. A static reading of the statement
# cannot tell those apart. I tried; the heuristic flagged four suites that were
# fine, which is how a guard gets muted.
#
# So the decision is made per row, from the row, and the invariant is checked
# by recomputing rather than by reasoning.

def delete_reservations(db, reservation_ids: list[str]) -> dict:
    """
    Remove test reservations, giving back exactly the stock they took.

    The one sanctioned way to delete a reservation from a suite. It reads each
    row first and decides *per row*:

      * a live row consumed inventory through `convert_hold` or
        `book_inventory`, so its stock is released before the row goes;
      * a row already `cancelled` or `no_show` gave its stock back when it was
        cancelled, and releasing again would subtract a booking nobody made —
        the same corruption from the other end, and the direction that oversells
        a night rather than hiding it.

    The release itself is `cancel_reservation` above rather than a second call
    to the RPC. One spelling of the arguments, in one place: this function
    existing at all is an admission that a second spelling is how the original
    bug happened.

    Returns what it did, so a suite can print it and a reader can check that
    the two counts add up to the number of rows.
    """
    if not reservation_ids:
        return {"deleted": 0, "released": 0, "already_dead": 0}

    rows = (
        db.table("reservations").select("id,status")
        .in_("id", list(reservation_ids)).execute().data or []
    )
    released = already_dead = 0
    for row in rows:
        try:
            if cancel_reservation(db, row["id"], reason="e2e-delete"):
                released += 1
            else:
                already_dead += 1
        except Exception:  # noqa: BLE001
            # A release that fails must not take the delete with it. This runs
            # in teardown, where raising loses the suite's own result — and a
            # row left behind is debris somebody can see, which is strictly
            # better than the invisible kind.
            logger.warning("could not release %s before deleting it", row["id"],
                           exc_info=True)

    for row in rows:
        db.table("reservations").delete().eq("id", row["id"]).execute()

    return {"deleted": len(rows), "released": released, "already_dead": already_dead}


def inventory_disagreements(db, property_id: str) -> dict:
    """
    Recompute `room_inventory` from the reservations that are actually live.

    The runtime check. `scripts/reconcile_inventory.py` owns the arithmetic —
    this is the same function, imported rather than reimplemented, so a suite
    and the repair tool can never disagree about what "consistent" means.
    """
    import pathlib as _pathlib
    import sys as _sys

    _sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
    from reconcile_inventory import disagreements

    return disagreements(db, property_id)


def assert_inventory_agrees(db, property_id: str, *, label: str = "") -> None:
    """
    Fail the suite that left a hotel's availability wrong.

    **This is the invariant §10 states**, asserted rather than inferred:
    *tests must never leave production-style inventory inconsistent*. Call it
    at the end of any suite that books rooms, and it fails on the run that
    caused the damage instead of six weeks later when somebody notices a hotel
    cannot sell a room it has.

    Deliberately not called automatically from every sweep: it is one query per
    room type and a suite that never touched a booking should not pay for it.
    """
    found = inventory_disagreements(db, property_id)
    if not found["wrong"]:
        return

    where = f" after {label}" if label else ""
    detail = ", ".join(
        f"{found['rooms'].get(row['room_type_id'], row['room_type_id'][:8])} "
        f"{row['date'][:10]} counter={row.get('units_booked')} actual={actual}"
        for row, actual in sorted(found["wrong"], key=lambda x: x[0]["date"])[:6]
    )
    raise AssertionError(
        f"inventory on {property_id} disagrees with its reservations{where}: "
        f"{len(found['wrong'])} nights, {found['stranded']} room-nights stranded, "
        f"{found['oversold']} oversold. {detail}. "
        f"Run scripts/reconcile_inventory.py {property_id} --apply to repair, "
        f"and use e2e_sweep.delete_reservations() rather than a bare DELETE."
    )

# ── shared configuration a suite borrows and must give back ──────────────────
#
# §11: *tests must not fight over shared state.*
#
# `agents.autonomy_level` on the demo property is one row, and six suites write
# to it. `ai_e2e`, `frontdesk_e2e`, `marketing_e2e`, `sales_e2e`,
# `whatsapp_e2e` and `payment_status_e2e` each set the level their own
# assertions need — correctly, and for good reasons — and none of them put it
# back. The last one to run decides what the property is until somebody
# notices.
#
# **What it cost.** A post-deployment evaluation reported negotiation accuracy
# of 7% and a 90% handoff rate, which reads as a catastrophic model regression
# and was very nearly published as one. It was an earlier suite's leftover 2:
# `get_quote` and `negotiate_rate` sit at autonomy floor 3, and at level 2 they
# queue for approval instead of executing, so the agent handed off — correctly,
# and exactly as designed. Every one of those thirteen "failures" was the
# autonomy ladder working.
#
# The ideal fix is a per-suite agent, so there is nothing shared to fight over.
# That is a change across six suites that each assert against the demo
# property's own configuration, and doing it while also changing the agent's
# decision path would be two large changes at once — rule 11. So the smaller
# one: a suite *borrows* the level and the borrowing is registered, and it goes
# back whether the suite ends, fails or crashes.

_BORROWED_AUTONOMY: dict[tuple[str, str], int | None] = {}


def borrow_autonomy(db, property_id: str, level: int, *,
                    agent_type: str = "guest") -> int | None:
    """
    Set an agent's autonomy for the duration of this suite, and arrange to
    give it back.

    Returns the level it was at, which is what a suite should print — a run
    that had to change the property's configuration to get its result should
    say so, because that is a fact about the measurement.

    The restore is registered with `atexit`, so a suite that raises halfway
    through still leaves the property as it found it. That is the whole point:
    the suites that leaked were not careless, they were the ones that died
    before their own teardown.
    """
    import atexit

    rows = (
        db.table("agents").select("id,autonomy_level")
        .eq("property_id", property_id).eq("agent_type", agent_type)
        .limit(1).execute().data or []
    )
    if not rows:
        return None
    agent_id, was = rows[0]["id"], rows[0].get("autonomy_level")

    key = (property_id, agent_type)
    if key not in _BORROWED_AUTONOMY:
        # Only the *first* borrow records the baseline. A suite that raises the
        # level twice must restore to what it found, not to its own first
        # change.
        _BORROWED_AUTONOMY[key] = was
        atexit.register(restore_autonomy, db)

    if was != level:
        db.table("agents").update({"autonomy_level": level}).eq(
            "id", agent_id).execute()
    return was


def restore_autonomy(db) -> int:
    """Put every borrowed autonomy level back. Safe to call more than once."""
    restored = 0
    for (property_id, agent_type), was in list(_BORROWED_AUTONOMY.items()):
        _BORROWED_AUTONOMY.pop((property_id, agent_type), None)
        if was is None:
            continue
        try:
            db.table("agents").update({"autonomy_level": was}).eq(
                "property_id", property_id).eq("agent_type", agent_type).execute()
            restored += 1
        except Exception:  # noqa: BLE001 — teardown must not mask the result
            logger.warning("could not restore autonomy on %s", property_id,
                           exc_info=True)
    return restored


def configuration_snapshot(db, property_id: str, *,
                           agent_type: str = "guest") -> dict:
    """
    What the thing being measured was configured as, at the moment of measuring.

    §11: *every evaluator must record hotel_id, agent_id, autonomy_level, model
    and a configuration snapshot with the evaluation result.* A number without
    this is not reproducible, and the 7% above is what that looks like — a real
    measurement of a configuration nobody recorded, indistinguishable from a
    measurement of the build.
    """
    from app.services.ai import model_router

    rows = (
        db.table("agents")
        .select("id,agent_type,autonomy_level,status,active_config_id,limits")
        .eq("property_id", property_id).eq("agent_type", agent_type)
        .limit(1).execute().data or []
    )
    agent = rows[0] if rows else {}
    prop = (
        db.table("properties").select("id,name,is_demo")
        .eq("id", property_id).limit(1).execute().data or [{}]
    )[0]
    return {
        "hotel_id": property_id,
        "hotel_name": prop.get("name"),
        "is_demo": prop.get("is_demo"),
        "agent_id": agent.get("id"),
        "agent_type": agent.get("agent_type"),
        "autonomy_level": agent.get("autonomy_level"),
        "agent_status": agent.get("status"),
        "config_id": agent.get("active_config_id"),
        # The per-agent ceilings a discount is checked against. Part of the
        # configuration a negotiation number depends on, and invisible without
        # it: an agent with max_discount_pct of 5 and one with 15 are two
        # different products being measured under one name.
        "limits": agent.get("limits") or {},
        "models": {
            tier: model_router.model_for_tier(tier)
            for tier in ("classify", "fast", "conversation", "reasoning")
        },
    }
