#!/usr/bin/env python3
"""
Rooms a hotel cannot sell because nobody gave them back.

    ./venv/bin/python scripts/reconcile_inventory.py <property-id>
    ./venv/bin/python scripts/reconcile_inventory.py <property-id> --apply

── what goes wrong ──────────────────────────────────────────────────────────

`room_inventory.units_booked` is owned by two RPCs: `convert_hold` adds to it
when a booking confirms, `release_booking` subtracts when one goes away.
Anything that changes a reservation without going through the second — an
UPDATE that sets `status = 'cancelled'`, a DELETE from a fixture teardown —
moves the counter one way only. No trigger catches it, because the product has
no cancellation path; only the test suites produce those rows.

The counter is what availability subtracts from, so the effect is a night that
reads as full with nobody in it. It is invisible until somebody tries to sell
that night, and then it is invisible *again*, because "no rooms available" is a
perfectly ordinary answer.

On Casa Mandala it reached **377 room-nights sold to nobody** — Deluxe Balcony,
the room the reference conversation is about, had fifteen of sixteen units gone
across the replay's own dates. The replay could no longer sell the room its own
script asks for. Two sessions went looking for the regression that explained
the failures. There was none: the suite had been eating the property it runs
on, one run at a time, and `e2e_sweep` had documented this exact failure for a
different suite months earlier.

── what this does ───────────────────────────────────────────────────────────

Recomputes `units_booked` per room type per night from the reservations that
are actually live, and reports every disagreement. **It does not use
`release_booking`**: releasing per cancelled row would double-subtract wherever
a sweep had already done it properly, and the truth here is not a delta but a
sum — how many rooms are sold on this night is a fact about the reservations,
not about the history of edits to a counter.

Reports by default and writes only with `--apply`, because a counter that
disagrees with the reservations is also what an oversell looks like, and that
is a conversation with a hotelier rather than a script.

`units_held` is deliberately untouched. Holds expire on their own clock and a
hold in flight while this runs is not debris.
"""
import pathlib
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db.client import get_db  # noqa: E402

DEAD = ("cancelled", "no_show")


def nights(check_in: str, check_out: str):
    day, end = date.fromisoformat(check_in[:10]), date.fromisoformat(check_out[:10])
    while day < end:
        yield day.isoformat()
        day += timedelta(days=1)


def disagreements(db, property_id: str) -> dict:
    """
    Where `room_inventory` and `reservations` do not agree, for one property.

    **Extracted from `main` so a test can ask the question.** §10 wants the
    invariant checked at *runtime* rather than inferred from the shape of a
    cleanup statement, and the reason is that the shape is not decidable: a
    DELETE against a fixture row written straight into `reservations` never
    touched the counter and must not release, while a DELETE against a booking
    that went through the payment path must. Only recomputing tells you which
    happened, which is how the five stranded room-nights were found.

    Returns counts and rows rather than printing, so `main` renders it for a
    person and `e2e_sweep.assert_inventory_agrees` fails a suite on it.

      stranded   the counter says sold, no live reservation says so — debris,
                 rooms the hotel can no longer sell.
      oversold   live reservations exceed the counter — the dangerous
                 direction: rooms sold that nothing is counting.
    """
    rooms = {
        r["id"]: r["name"] for r in (
            db.table("room_types").select("id,name")
            .eq("property_id", property_id).execute().data or []
        )
    }
    if not rooms:
        return {"room_types": 0, "reservations": 0, "live": 0, "inventory": 0,
                "wrong": [], "stranded": 0, "oversold": 0, "rooms": rooms}

    reservations = (
        db.table("reservations")
        .select("id,ref,status,check_in,check_out,units,room_type_id")
        .eq("property_id", property_id).limit(5000).execute().data or []
    )
    live = [r for r in reservations if r.get("status") not in DEAD]

    sold: dict[tuple[str, str], int] = defaultdict(int)
    for row in live:
        if not (row.get("room_type_id") and row.get("check_in") and row.get("check_out")):
            continue
        for night in nights(row["check_in"], row["check_out"]):
            sold[(row["room_type_id"], night)] += row.get("units") or 1

    inventory = (
        db.table("room_inventory").select("id,room_type_id,date,units_booked")
        .eq("property_id", property_id).limit(20000).execute().data or []
    )

    wrong = [
        (row, sold.get((row["room_type_id"], row["date"][:10]), 0))
        for row in inventory
        if (row.get("units_booked") or 0) != sold.get(
            (row["room_type_id"], row["date"][:10]), 0
        )
    ]
    return {
        "room_types": len(rooms),
        "reservations": len(reservations),
        "live": len(live),
        "inventory": len(inventory),
        "wrong": wrong,
        "stranded": sum((row.get("units_booked") or 0) - actual
                        for row, actual in wrong),
        "oversold": sum(actual - (row.get("units_booked") or 0)
                        for row, actual in wrong if actual > (row.get("units_booked") or 0)),
        "rooms": rooms,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if not args:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    property_id = args[0]
    db = get_db()

    found = disagreements(db, property_id)
    if not found["room_types"]:
        print(f"no room types on {property_id}")
        return 1

    rooms, wrong = found["rooms"], found["wrong"]
    stranded, oversold = found["stranded"], found["oversold"]

    print(f"{found['reservations']} reservations, {found['live']} live")
    print(f"{found['inventory']} inventory rows, {len(wrong)} disagree")
    if not wrong:
        print("nothing to reconcile")
        return 0

    for row, actual in sorted(wrong, key=lambda x: x[0]["date"])[:40]:
        name = rooms.get(row["room_type_id"], row["room_type_id"][:8])
        print(f"  {name:<20} {row['date'][:10]}  booked={row.get('units_booked')}  "
              f"actually sold={actual}")
    if len(wrong) > 40:
        print(f"  … and {len(wrong) - 40} more")

    print(f"\n{stranded} room-nights recorded as sold that nobody has booked")
    if oversold:
        # The other direction, and the one to stop for. A counter *below* the
        # reservations means rooms were sold without the counter being told,
        # and correcting it downward is not tidying up — it is the hotel
        # finding out it is oversold.
        print(f"⚠ {oversold} room-nights are booked but not counted — this is an "
              f"oversell risk, not debris. Do not --apply without looking at it.")

    if not apply:
        print("\nre-run with --apply to write the counters back")
        return 0

    for row, actual in wrong:
        db.table("room_inventory").update({"units_booked": actual}).eq("id", row["id"]).execute()
    print(f"\n{len(wrong)} rows reconciled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
