#!/usr/bin/env python3
"""
Ten guests, one room. §6 and §7 of the reliability brief.

**What is being proved.** `hold_inventory` is the single point at which stock
stops being available, and everything a guest is ever told about availability is
downstream of it. Read carefully, it looks right: the night rows are claimed
with `insert … on conflict do update … where booked + held + blocked + p_units
<= total`, which is one statement per night and therefore atomic, and a night
whose guard fails is silently not returned, so the function counts the rows it
got back and raises if any are missing. Reading is not proof. This runs it.

    ten concurrent holds · one unit of stock
        exactly one may succeed
        units_held must equal exactly one
        nine callers must be told no, and told it without an error

**Why it needs a real database.** The property under test is a subtransaction
rollback inside a plpgsql exception block, a row lock taken by ON CONFLICT, and
the serialisation Postgres does between them. `tests/fake_supabase` models none
of that and cannot: it is a dict. A test that "passes" against it would be
asserting the fixture.

**Isolation.** The probe builds its own room type, holds against its own dates a
decade out, and deletes what it made. It never touches a room a hotel sells —
§10, and the 377 room-nights that suite teardown once ate at Casa Mandala.

    venv/bin/python scripts/concurrency_probe.py
"""
import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.client import get_db  # noqa: E402

# A decade out, so nothing the probe does can collide with a real stay or show
# up on anybody's arrivals list.
CHECK_IN = date.today() + timedelta(days=3650)
CHECK_OUT = CHECK_IN + timedelta(days=2)

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(label)
        print(f"  {GREEN}✓{OFF} {label}")
    else:
        FAILED.append((label, detail))
        print(f"  {RED}✗{OFF} {label}  {DIM}{detail}{OFF}")
    return bool(condition)


def make_room_type(db, property_id: str, partner_id: str, units: int) -> str:
    """A room type nobody sells, with exactly `units` of stock."""
    row = db.table("room_types").insert({
        "property_id": property_id,
        "partner_id": partner_id,
        "name": f"Concurrency Probe {uuid.uuid4().hex[:6]}",
        "code": f"CPROBE{uuid.uuid4().hex[:4].upper()}",
        "total_units": units,
        "max_occupancy": 2,
        "is_active": True,
    }).execute().data[0]
    return row["id"]


def teardown(db, room_type_id: str) -> None:
    """
    Everything this probe made, removed — holds first, then the nights, then
    the room type. Ordered by foreign key, not by convenience.
    """
    db.table("inventory_holds").delete().eq("room_type_id", room_type_id).execute()
    db.table("room_inventory").delete().eq("room_type_id", room_type_id).execute()
    db.table("room_types").delete().eq("id", room_type_id).execute()


def nights(db, room_type_id: str) -> list[dict]:
    return (db.table("room_inventory")
            .select("date,units_held,units_booked,units_blocked")
            .eq("room_type_id", room_type_id).order("date").execute().data or [])


async def race(db, room_type_id: str, attempts: int, units_each: int) -> list:
    """`attempts` callers asking for `units_each` at the same instant."""
    loop = asyncio.get_running_loop()

    def _hold(_n):
        return db.rpc("hold_inventory", {
            "p_room_type": room_type_id,
            "p_check_in": CHECK_IN.isoformat(),
            "p_check_out": CHECK_OUT.isoformat(),
            "p_units": units_each,
            "p_ttl_seconds": 900,
            "p_rate_plan": None,
            "p_source": "employee",
            "p_conversation": None,
        }).execute().data

    return await asyncio.gather(
        *(loop.run_in_executor(None, _hold, n) for n in range(attempts)),
        return_exceptions=True,
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--stock", type=int, default=1,
                        help="units the room type has in total")
    args = parser.parse_args()

    db = get_db()
    prop = (db.table("properties").select("id,partner_id,name")
            .eq("status", "active").limit(1).execute().data or [])
    if not prop:
        print("no active property to attach a probe room type to")
        return 2
    property_id, partner_id = prop[0]["id"], prop[0]["partner_id"]
    print(f"{DIM}property {prop[0]['name']} · {CHECK_IN} → {CHECK_OUT} "
          f"· {args.attempts} callers · {args.stock} unit(s) of stock{OFF}\n")

    room_type_id = make_room_type(db, property_id, partner_id, args.stock)
    try:
        print("── the race ──")
        results = await race(db, room_type_id, args.attempts, units_each=1)

        raised = [r for r in results if isinstance(r, Exception)]
        granted = [r for r in results if not isinstance(r, Exception) and r]
        refused = [r for r in results if not isinstance(r, Exception) and not r]

        check(f"exactly {args.stock} caller(s) got the stock",
              len(granted) == args.stock, f"{len(granted)} granted")
        check("everybody else was refused",
              len(refused) == args.attempts - args.stock, f"{len(refused)} refused")
        check("and refused cleanly, without an error reaching the caller",
              not raised, f"{len(raised)} raised: {[str(e)[:80] for e in raised[:2]]}")
        check("every granted hold is a distinct hold",
              len(set(map(str, granted))) == len(granted))

        print("\n── the ledger ──")
        rows = nights(db, room_type_id)
        check(f"one row per night ({(CHECK_OUT - CHECK_IN).days})",
              len(rows) == (CHECK_OUT - CHECK_IN).days, str(len(rows)))
        for row in rows:
            held = row["units_held"]
            check(f"   {row['date']}: units_held == {args.stock}",
                  held == args.stock, f"units_held={held}")
            check(f"   {row['date']}: nothing went negative",
                  held >= 0 and row["units_booked"] >= 0 and row["units_blocked"] >= 0,
                  str(row))

        print("\n── the losers left nothing behind ──")
        holds = (db.table("inventory_holds").select("id,status,units")
                 .eq("room_type_id", room_type_id).execute().data or [])
        active = [h for h in holds if h["status"] == "active"]
        released = [h for h in holds if h["status"] == "released"]
        check(f"exactly {args.stock} hold row(s) still active",
              len(active) == args.stock, f"{len(active)} active of {len(holds)}")
        check("a caller that lost the race released its own hold row",
              len(released) == len(refused),
              f"{len(released)} released, {len(refused)} refused")
        check("no hold is holding more than the room type has",
              all(h["units"] <= args.stock for h in active))

        print("\n── stale state: a second race against stock already gone ──")
        # §7. The first race consumed everything. A caller arriving now is
        # working from an availability read that was true a moment ago.
        second = await race(db, room_type_id, 3, units_each=1)
        check("every one of them is refused",
              all(not r for r in second if not isinstance(r, Exception)),
              str(second))
        after = nights(db, room_type_id)
        check("and the ledger did not move",
              [r["units_held"] for r in after] == [r["units_held"] for r in rows],
              f"{[r['units_held'] for r in after]}")
    finally:
        teardown(db, room_type_id)
        left = (db.table("room_types").select("id").eq("id", room_type_id)
                .execute().data or [])
        print(f"\n{DIM}── cleaned up: probe room type "
              f"{'REMAINS — investigate' if left else 'removed'}{OFF}")

    print(f"\n══ {len(PASSED)} passed, {len(FAILED)} failed ══")
    for label, detail in FAILED:
        print(f"  {RED}FAIL{OFF} {label}  {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
