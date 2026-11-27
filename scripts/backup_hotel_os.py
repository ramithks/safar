#!/usr/bin/env python3
"""
A nightly copy of everything a hotel cannot afford to lose.

**This is not a substitute for point-in-time recovery, and it should not be
described as one.** The hotel OS is on Supabase's free tier, which has no PITR,
no daily managed backups you can restore from, and pauses a project after a
week of inactivity. That was a deliberate choice while the pilot is small. This
script is the cheap half of softening it: if the project is lost, this is the
difference between "we can rebuild the hotels from a file" and "we cannot".

What it captures is the **operational record** — the rows that describe money
owed, guests expected and work done, none of which can be reconstructed from
anywhere else:

    reservations · reservation_nights · folio_entries · guests
    guest_registrations · payment_orders · credit_ledger · usage_events
    creator_payouts · creator_payout_events · attribution_touches
    properties · room_types · rate_plans · room_inventory · rate_plan_rates
    partners · partner_members · b2b_agents

What it does **not** capture: `auth.users` (Supabase owns those, and a copy of
password hashes is a liability rather than an asset), the schema itself (that
is the migrations, in git), and conversation history (large, and recoverable in
the sense that matters — nobody bills from it).

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/backup_hotel_os.py                  # ./backups/
    ./venv/bin/python scripts/backup_hotel_os.py --out /some/dir
    ./venv/bin/python scripts/backup_hotel_os.py --verify latest  # read one back

Run it from cron on a machine that is not the database. A backup that lives
only in the thing it is backing up is not a backup.
"""
import argparse
import base64
import gzip
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

PROJECT = "rdlozqvuywtgsodunait"

# Ordered so a restore could be replayed top to bottom without tripping a
# foreign key. That ordering is the only reason this is a list and not a set.
TABLES = [
    "partners",
    "profiles",
    "partner_members",
    "billing_accounts",
    "subscription_plans",
    "subscriptions",
    "credit_wallets",
    "credit_buckets",
    "credit_ledger",
    "properties",
    "room_types",
    "rooms",
    "rate_plans",
    "room_inventory",
    "rate_plan_rates",
    "services",
    "guests",
    "quotes",
    "reservations",
    "reservation_nights",
    "folio_entries",
    "guest_registrations",
    "payment_orders",
    "hotel_websites",
    "b2b_agents",
    "creators",
    "creator_campaigns",
    "campaign_creators",
    "attribution_touches",
    "creator_payouts",
    "creator_payout_events",
    "usage_events",
    "tasks",
]

# One request's worth. Tables are read in pages until the row count is met, so
# this is a transport limit rather than a ceiling on what gets backed up — the
# first version had no paging and quietly stopped at 5,000, which is exactly the
# failure that makes a backup worse than none: it would have been trusted.
PAGE = 2000

_raw = subprocess.check_output(
    ["security", "find-generic-password", "-s", "Supabase CLI", "-w"], text=True
).strip()
MGMT = base64.b64decode(_raw.removeprefix("go-keyring-base64:")).decode()


def sql(query: str, *, attempts: int = 4):
    import time as _time
    last = ""
    for attempt in range(attempts):
        req = urllib.request.Request(
            f"https://api.supabase.com/v1/projects/{PROJECT}/database/query",
            method="POST", data=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {MGMT}",
                     "User-Agent": "SupabaseCLI/2.0.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read() or b"[]")
        except urllib.error.HTTPError as e:
            last = f"{e.code} {e.read().decode()[:200]}"
            if e.code < 500:
                break
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        _time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"could not read: {last}")


def dump_table(name: str) -> tuple[list, bool]:
    """
    Every row, and whether we know we got them all.

    Paged by `ctid`, not by `offset`. Offset paging re-scans and, on a table
    being written to while the backup runs, silently skips or repeats rows as
    the ordering shifts underneath it. `ctid` is the physical row address: it is
    stable for the duration of a read, it exists on every table without needing
    a sortable key, and it needs no assumption about what the primary key is
    called.

    The row count is taken first and checked at the end. A mismatch is reported
    rather than absorbed — a table that grew mid-backup is a fact worth knowing,
    not a rounding error.
    """
    count = int(sql(f"select count(*) as n from public.{name};")[0]["n"])
    if count == 0:
        return [], True

    rows: list = []
    cursor = "(0,0)"
    while True:
        page = sql(f"""
            select ctid::text as _ctid, to_jsonb(t) as row
              from public.{name} t
             where ctid > '{cursor}'::tid
             order by ctid
             limit {PAGE};""")
        if not page:
            break
        rows.extend(r["row"] for r in page)
        cursor = page[-1]["_ctid"]
        if len(page) < PAGE:
            break

    return rows, len(rows) >= count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backups", help="directory to write into")
    parser.add_argument("--verify", metavar="FILE",
                        help="read a backup back and report what is in it")
    args = parser.parse_args()

    if args.verify:
        path = pathlib.Path(args.verify)
        if args.verify == "latest":
            found = sorted(pathlib.Path(args.out).glob("hotel-os-*.json.gz"))
            if not found:
                print(f"  no backups in {args.out}/")
                return 1
            path = found[-1]
        with gzip.open(path, "rt") as fh:
            data = json.load(fh)
        print(f"\n══ {path.name} ══")
        print(f"  taken     {data['taken_at']}")
        print(f"  project   {data['project']}")
        total = 0
        for name, rows in sorted(data["tables"].items()):
            if rows:
                print(f"  {name:<26} {len(rows):>6} rows")
                total += len(rows)
        print(f"\n  {total:,} rows across {len(data['tables'])} tables")
        if data.get("truncated"):
            print(f"  ⚠ TRUNCATED: {', '.join(data['truncated'])}")
        return 0

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out / f"hotel-os-{stamp}.json.gz"

    print(f"\n══ backing up {PROJECT} ══\n")
    tables, truncated, total = {}, [], 0
    for name in TABLES:
        try:
            rows, complete = dump_table(name)
        except RuntimeError as exc:
            print(f"  ─ {name:<26} FAILED: {exc}")
            truncated.append(name)
            continue
        tables[name] = rows
        total += len(rows)
        if not complete:
            truncated.append(name)
            print(f"  ⚠ {name:<26} {len(rows):>6} rows — FEWER THAN THE COUNT SAID")
        elif rows:
            print(f"    {name:<26} {len(rows):>6} rows")

    payload = {
        "project": PROJECT,
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "note": "Operational data only. Schema lives in the migrations; auth.users "
                "is Supabase's and deliberately not copied.",
        "truncated": truncated,
        "tables": tables,
    }
    with gzip.open(path, "wt") as fh:
        json.dump(payload, fh)

    size = path.stat().st_size
    print(f"\n  wrote {path}  ({size / 1024:,.0f} KB, {total:,} rows)")
    if truncated:
        print(f"\n  ⚠ INCOMPLETE — {', '.join(truncated)}")
        print("    Do not trust this file until that is understood.")
        return 1
    print("\n  Copy this somewhere that is not this laptop and not this Supabase "
          "project.\n  It is not point-in-time recovery; it is the difference "
          "between rebuilding and not.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
