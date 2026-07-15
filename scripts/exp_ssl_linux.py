"""
Experiment 4 — reproduce (or not) the Supabase bad_record_mac SSL storm on Linux.

Runs N real trip generations (real scrapers + real Gemini — the exact prod
workload) in ONE process and tallies TLS/transport errors, so we can compare the
prod Linux VM against local macOS, where full generations hit bad_record_mac at a
low, clumpy rate (~4 in 40 gens). Same Supabase project in both environments, so
the ONLY variable is the client host/OS/network.

Run inside the deployed container, from the repo root:

    python scripts/exp_ssl_linux.py --n 30

Batch-write instrumentation (thread id, client id, cross-thread detector) is
turned on automatically. Created trips are deleted at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

ROUTES = [
    ("bhiwani", "manali"), ("jaipur", "udaipur"), ("delhi", "rishikesh"),
    ("mumbai", "goa"), ("bengaluru", "coorg"), ("kolkata", "darjeeling"),
    ("pune", "lonavala"), ("ahmedabad", "kutch"), ("chennai", "pondicherry"),
    ("lucknow", "ayodhya"),
]
DEPARTURE = (date.today() + timedelta(days=24)).isoformat()


def num_fds() -> int:
    for p in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(p))
        except OSError:
            continue
    return -1


class TlsCounter(logging.Handler):
    CATS = {
        "bad_record_mac": ("bad record mac", "sslv3_alert"),
        "ssl_error": ("ssl:", "sslerror", "record layer failure"),
        "read_error": ("readerror",),
        "connect_error": ("connecterror",),
        "cross_thread": ("cross-thread client",),
    }

    def __init__(self) -> None:
        super().__init__()
        self.counts: Counter = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            m = record.getMessage().lower()
        except Exception:
            return
        for cat, needles in self.CATS.items():
            if any(n in m for n in needles):
                self.counts[cat] += 1


async def _valid_user_id() -> str:
    """A FK-valid user id from an existing trip_states row (shared Supabase)."""
    from app.db.client import run_db, get_db

    def _q():
        r = (get_db().table("trip_states").select("user_id")
             .not_.is_("user_id", "null").limit(1).execute())
        return r.data[0]["user_id"] if r.data else None
    uid = await run_db(_q)
    if not uid:
        raise SystemExit("no existing user_id found in trip_states to attribute test trips to")
    return uid


async def main(n: int) -> None:
    from app.config import settings
    settings.instrument_batches = True

    from app.trip_state import store
    from app.trip_state.generator import run_generation
    from app.trip_state.models import empty_trip_state
    from app.api.v1.plan_trip.models import PlanRequest
    from app.db.client import run_db, get_db, db_stats_snapshot, db_stats_delta, format_db_delta

    tls = TlsCounter()
    logging.getLogger().addHandler(tls)

    user_id = await _valid_user_id()
    created: list[str] = []
    fd_start = num_fds()
    fd_peak = fd_start

    for i in range(n):
        origin, dest = ROUTES[i % len(ROUTES)]
        body = PlanRequest(origin=origin, destination=dest, departure_date=DEPARTURE,
                           days=3, travelers=2, budget="low")
        state = empty_trip_state({
            "origin": origin, "destination": dest, "departure_date": DEPARTURE,
            "days": 3, "stay_nights": 2, "travelers": 2, "budget_tier": "low", "interests": [],
        })
        before = tls.counts.copy()
        db_before = db_stats_snapshot()
        t0 = time.monotonic()
        err = None
        trip_id = None
        try:
            trip_id = await run_db(store.create_trip_state, user_id, state)
            created.append(trip_id)
            await run_generation(trip_id, body, user_id)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        dur = time.monotonic() - t0
        fd_peak = max(fd_peak, num_fds())
        d_tls = {k: tls.counts[k] - before.get(k, 0) for k in TlsCounter.CATS if tls.counts[k] - before.get(k, 0)}
        print(f"[{i+1:>2}/{n}] {origin}->{dest:<12} {dur:6.1f}s  "
              f"{format_db_delta(db_stats_delta(db_before)):<42} fd={num_fds():<4} "
              f"tls={d_tls or '—'}{'  ERR ' + err if err else ''}", flush=True)

    print("\n" + "=" * 70)
    print(f"LINUX REPRO  |  generations: {n}")
    print(f"TLS/SSL events (total): {dict(tls.counts) or 'NONE'}")
    print(f"fd: start={fd_start} peak={fd_peak} end={num_fds()}")
    print("=" * 70)

    def _cleanup():
        db = get_db()
        for tid in created:
            for table, col in (("trip_state_events", "trip_id"), ("trip_states", "id"), ("trips", "id")):
                try:
                    db.table(table).delete().eq(col, tid).execute()
                except Exception:
                    pass
    try:
        await run_db(_cleanup)
    except Exception:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main(args.n))
