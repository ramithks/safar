#!/usr/bin/env python3
"""
Classify every oversold room-night, then repair the side that is actually wrong.

    ./venv/bin/python scripts/repair_inventory.py <property-id>
    ./venv/bin/python scripts/repair_inventory.py <property-id> --apply TEST_FIXTURE

── why this is not `reconcile_inventory --apply` ────────────────────────────

`reconcile_inventory` recomputes `units_booked` from the live reservations and
writes it back. For *stranded* nights — the counter says sold and nobody is —
that is exactly right, and it is what that script is for.

For an **oversold** night it is exactly wrong. The counter is below the
reservations, so recomputing raises it, and raising it is the hotel discovering
it is oversold rather than the hotel being fixed. Whether that is the right
write depends on something no counter can tell you: *who is on those nights.*

    six real guests, ledger never told   →  the ledger is wrong. Write it.
                                            Never touch the bookings.
    six fixture rows nobody swept        →  the fixtures are wrong. Retire them.
                                            Never touch the ledger — they took
                                            no inventory, and "releasing" it
                                            would strand six real units.

Same eighteen numbers, opposite repairs, and picking the wrong one either sells
a paying guest's room to somebody else or takes six sellable rooms off a hotel
for a month. So nothing here repairs a class it has not positively identified,
and `UNKNOWN` is a terminal state that prints and stops.

── the rules ────────────────────────────────────────────────────────────────

  - never delete a reservation
  - never release inventory belonging to a live booking
  - never raise a counter for a reservation that is not real
  - never repair a class the operator did not name on the command line
  - every write leaves an `admin_audit_log` row saying which class it was and
    what evidence put it there

Deletion is not a repair path here at all. A fixture is retired by being
cancelled with a reason, which takes it out of the live set — the only thing
that matters for the counter — and leaves the row readable afterwards. A
DELETE would fix the same number and destroy the evidence that it was ever
wrong, on the one table where "what did we sell" has to stay answerable.
"""
import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.db.client import get_db  # noqa: E402
from reconcile_inventory import DEAD, disagreements, nights  # noqa: E402

REAL_ACTIVE = "REAL_ACTIVE"
EXPIRED_HOLD = "EXPIRED_HOLD"
CANCELLED_NOT_RELEASED = "CANCELLED_NOT_RELEASED"
TEST_FIXTURE = "TEST_FIXTURE"
CORRUPT_STATE = "CORRUPT_STATE"
UNKNOWN = "UNKNOWN"

# Live statuses this script has been taught. `DEAD` is handled above it, and
# anything in neither list stops the row at UNKNOWN — see `classify`.
KNOWN_STATUSES = frozenset({"confirmed", "checked_in", "checked_out", "pending",
                            "hold", "held", "tentative"})

# Every ref prefix a suite in `scripts/` writes. A prefix on this list is a
# *necessary* condition for TEST_FIXTURE and nowhere near a sufficient one —
# see `classify`, which requires the whole signature.
FIXTURE_PREFIXES = ("TB-IBX", "TB-WCI", "TB-BE", "TB-OW", "TB-PL", "TB-EVAL")

# The phone numbers those suites use. A real guest could in principle be given
# one of these; that is why it is one clause of several rather than the test.
FIXTURE_PHONES = frozenset({
    "+919812345678", "+919800000001", "+919800000002", "+919800000003",
    "+919800000004", "+919800000009",
})

# A reservation a person actually made has at least one of these. A fixture
# INSERTed straight into the table has none of them, because none of the paths
# that set them ever ran.
#
# **`public_token` was on this list and had to come off.** It is a column
# default: all 86 reservations at Casa Mandala carry one, including the three
# rows whose INSERT statement never mentions it. A field the database fills in
# for everybody is evidence of nothing, and leaving it here made the signature
# unsatisfiable — the script correctly refused to classify anything, for a
# reason that was entirely its own.
#
# The test for membership is not "a real booking has this" but "*only* a real
# booking has this". A default fails it by construction.
TRACES_OF_A_REAL_BOOKING = ("conversation_id", "quote_id", "booking_id",
                            "agent_action_id", "guest_id", "source_ref",
                            "b2b_agent_id")


def classify(reservation: dict, *, property_is_demo: bool) -> tuple[str, dict]:
    """
    Which of the six classes this reservation is, and the evidence for it.

    Ordered most-certain-first, and every branch that cannot prove itself falls
    through to UNKNOWN rather than to a default. The asymmetry is deliberate:
    misfiling a real booking as a fixture cancels a guest's room, and misfiling
    a fixture as real writes six phantom units into a hotel's ledger. UNKNOWN
    costs somebody ten minutes.
    """
    ref = (reservation.get("ref") or "").upper()
    status = (reservation.get("status") or "").lower()
    evidence: dict = {"ref": ref, "status": status}

    # ── a reservation that is already dead ──────────────────────────────────
    #
    # It is not on an oversold night by being live; it is here because the
    # counter still counts it. Nothing about the reservation needs repairing.
    if status in DEAD:
        evidence["why"] = "status is already terminal; the counter did not follow"
        return CANCELLED_NOT_RELEASED, evidence

    # ── contradictions, before anything is believed ─────────────────────────
    #
    # Checked before the fixture signature so a row that is both fixture-shaped
    # *and* internally impossible is reported rather than quietly retired.
    if reservation.get("check_out") and reservation.get("check_in"):
        if reservation["check_out"][:10] <= reservation["check_in"][:10]:
            evidence["why"] = "check_out is not after check_in"
            return CORRUPT_STATE, evidence
    if (reservation.get("units") or 0) <= 0:
        evidence["why"] = f"units = {reservation.get('units')!r}"
        return CORRUPT_STATE, evidence
    if not reservation.get("room_type_id"):
        evidence["why"] = "no room type; the night it occupies is unknowable"
        return CORRUPT_STATE, evidence
    paid = reservation.get("paid_paise") or 0
    total = reservation.get("total_paise") or 0
    if paid > total > 0:
        evidence["why"] = f"paid {paid} against a total of {total}"
        return CORRUPT_STATE, evidence

    # ── a state this script does not understand ─────────────────────────────
    #
    # Before the signature, not after. A perfect fixture signature is strong
    # evidence about *where the row came from* and says nothing about what
    # `status = 'reinstated'` means — and a row whose state we cannot name is
    # exactly the row not to retire on the strength of its costume. New statuses
    # get added to this list by somebody who knows what they mean.
    if status not in KNOWN_STATUSES:
        evidence["why"] = f"status {status!r} is not one this script knows"
        return UNKNOWN, evidence

    # ── a fixture, and only on the whole signature ──────────────────────────
    signature = {
        "demo_property": property_is_demo,
        "ref_prefix": any(ref.startswith(p) for p in FIXTURE_PREFIXES),
        "created_by_import": reservation.get("created_by") == "import",
        "fixture_phone": reservation.get("guest_phone") in FIXTURE_PHONES,
        "no_trace_of_a_real_booking": not any(
            reservation.get(f) for f in TRACES_OF_A_REAL_BOOKING),
    }
    evidence["signature"] = signature
    if all(signature.values()):
        return TEST_FIXTURE, evidence

    # A row wearing *some* of the costume is the most dangerous thing on this
    # list, and the one case where a wrong guess is unrecoverable. It is never
    # repaired automatically.
    if any(signature.values()) and signature["ref_prefix"]:
        evidence["why"] = ("carries a fixture ref but not the rest of the "
                           "signature — a person has to look at this")
        return UNKNOWN, evidence

    # ── a real, live booking ────────────────────────────────────────────────
    evidence["why"] = "live, and does not carry the fixture signature"
    evidence["traces"] = [f for f in TRACES_OF_A_REAL_BOOKING if reservation.get(f)]
    return REAL_ACTIVE, evidence


def expired_holds(db, property_id: str, when: datetime) -> list[dict]:
    """
    Holds past their expiry that `units_held` is still counting.

    Separate from the reservation classes because a hold is not a reservation:
    it has its own row, its own clock, and its own release RPC. A hold that is
    merely *old* is not debris — `release_hold` is the only thing that may end
    one, and this reports which ones are due.
    """
    try:
        rows = (db.table("inventory_holds")
                .select("id,room_type_id,check_in,check_out,units,expires_at,status")
                .eq("property_id", property_id).limit(2000).execute().data or [])
    except Exception:  # noqa: BLE001
        return []
    return [
        r for r in rows
        if (r.get("status") or "").lower() in ("active", "held", "open")
        and r.get("expires_at") and r["expires_at"] < when.isoformat()
    ]


def audit(db, property_id: str, entries: list[dict]) -> None:
    """
    One row per repaired thing, written before the repair rather than after.

    Before, because the failure this protects against is a repair that half
    happens: a row that says "we were about to cancel TB-IBX5FC as a fixture,
    here is the evidence" is useful whether or not the update lands, and a row
    written afterwards only exists in the case where nothing went wrong.
    """
    if not entries:
        return
    try:
        db.table("admin_audit_log").insert([{
            "actor": "scripts/repair_inventory.py",
            "action": "inventory_repair",
            "entity_type": e["table"],
            "entity_id": e["id"],
            "entity_label": e.get("label") or property_id,
            "reason": e["repair"],
            "metadata": json.loads(json.dumps(e, default=str)),
        } for e in entries]).execute()
    except Exception as exc:  # noqa: BLE001
        # The one thing that must stop the repair. Everywhere else in this
        # codebase an audit failure is swallowed so it cannot block the work —
        # `services/pms/audit.py` says so explicitly, and is right, because
        # there the work is a hotelier saving a form. Here the work is an
        # unattended write to production inventory, and a repair nobody can
        # reconstruct afterwards is worse than a repair that did not happen.
        raise SystemExit(f"refusing to repair: the audit row failed to write ({exc})")


def repair(db, property_id: str, plan: list[tuple[str, dict, dict]],
           classes: set[str], *, now: datetime) -> list[str]:
    """Apply only the classes the operator named. Returns what was done."""
    done = []

    fixtures = [(r, e) for cls, r, e in plan if cls == TEST_FIXTURE]
    if TEST_FIXTURE in classes and fixtures:
        audit(db, property_id, [{
            "table": "reservations", "id": r["id"], "class": TEST_FIXTURE,
            "label": r.get("ref"), "property_id": property_id,
            "repair": "status -> cancelled (unswept e2e fixture)", "evidence": e,
            "inventory_touched": False,
        } for r, e in fixtures])
        for r, _ in fixtures:
            # **Cancelled, not deleted, and the counter is not touched.**
            #
            # These rows were INSERTed straight into `reservations` and never
            # called `convert_hold`, so they never added to `units_booked`.
            # Releasing what they never took would subtract six units a real
            # guest may be holding. Taking them out of the live set is the
            # entire repair.
            db.table("reservations").update({
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
                "cancellation_reason":
                    "inventory repair: unswept e2e fixture (scripts/repair_inventory.py)",
            }).eq("id", r["id"]).eq("property_id", property_id).execute()
            done.append(f"cancelled fixture {r.get('ref')}")

    reals = [(r, e) for cls, r, e in plan if cls == REAL_ACTIVE]
    if REAL_ACTIVE in classes and reals:
        # The ledger is what is wrong here, never the booking. Recompute the
        # affected nights from the live reservations and write the counter up
        # to the truth, creating the row where there is none.
        raise SystemExit(
            "REAL_ACTIVE repair writes a hotel's ledger up to match bookings it "
            "did not know about. That is the hotel finding out it is oversold, "
            "and it is a conversation with the hotelier, not a script run. "
            "Use reconcile_inventory.py --apply once they have agreed."
        )

    for cls in (CANCELLED_NOT_RELEASED, EXPIRED_HOLD, CORRUPT_STATE, UNKNOWN):
        if cls in classes:
            raise SystemExit(
                f"{cls} has no automatic repair here. "
                f"{CANCELLED_NOT_RELEASED} is what reconcile_inventory.py --apply "
                f"is for; {EXPIRED_HOLD} is release_hold's; the other two are a "
                f"person's."
            )
    return done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("property_id")
    parser.add_argument("--apply", nargs="*", default=None, metavar="CLASS",
                        help="classes to repair, e.g. --apply TEST_FIXTURE")
    args = parser.parse_args()

    db = get_db()
    now = datetime.now(timezone.utc)
    prop = (db.table("properties").select("id,name,is_demo")
            .eq("id", args.property_id).limit(1).execute().data or [])
    if not prop:
        print(f"no property {args.property_id}")
        return 1
    is_demo = bool(prop[0].get("is_demo"))
    print(f"{prop[0].get('name')}  (is_demo={is_demo})\n")

    found = disagreements(db, args.property_id)
    oversold = [(row, sold) for row, sold in found["wrong"]
                if sold > (row.get("units_booked") or 0)]
    if not oversold:
        print("no oversold nights")
        return 0

    print(f"{found['oversold']} oversold unit-nights across {len(oversold)} nights"
          f" ({len(found['unledgered'])} with no ledger row at all)\n")

    reservations = (
        db.table("reservations").select("*")
        .eq("property_id", args.property_id).limit(5000).execute().data or [])
    live = [r for r in reservations if (r.get("status") or "").lower() not in DEAD]

    on_oversold_nights = {(row["room_type_id"], row["date"][:10]) for row, _ in oversold}
    culprits: dict[str, dict] = {}
    for r in reservations:
        if not (r.get("room_type_id") and r.get("check_in") and r.get("check_out")):
            continue
        for night in nights(r["check_in"], r["check_out"]):
            if (r["room_type_id"], night) in on_oversold_nights:
                culprits[r["id"]] = r
                break

    plan = []
    for r in sorted(culprits.values(), key=lambda x: x.get("created_at") or ""):
        cls, evidence = classify(r, property_is_demo=is_demo)
        plan.append((cls, r, evidence))
        print(f"  {cls:<24} {r.get('ref'):<12} {r.get('check_in','')[:10]}"
              f" → {r.get('check_out','')[:10]}  units={r.get('units')}"
              f"  status={r.get('status')}")
        print(f"      {evidence.get('why') or 'full fixture signature matched'}")

    stale = expired_holds(db, args.property_id, now)
    if stale:
        print(f"\n  {len(stale)} expired holds still counted — release_hold's job")

    counts: dict[str, int] = {}
    for cls, _, _ in plan:
        counts[cls] = counts.get(cls, 0) + 1
    print(f"\n{counts}")
    if UNKNOWN in counts or CORRUPT_STATE in counts:
        print("⚠ unexplained rows present — reconciliation cannot finish clean")

    if args.apply is None:
        print("\nre-run with --apply <CLASS> to repair one class")
        return 0

    done = repair(db, args.property_id, plan, set(args.apply), now=now)
    for line in done:
        print(f"  ✓ {line}")

    after = disagreements(db, args.property_id)
    print(f"\nafter: {after['oversold']} oversold unit-nights, "
          f"{after['stranded']} stranded")
    return 0 if not after["oversold"] else 1


if __name__ == "__main__":
    sys.exit(main())
