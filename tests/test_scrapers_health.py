"""
Per-scraper health check — hits every flight and stay provider DIRECTLY (no
aggregator merge), so a provider that's actually broken can't hide behind the
others. Runs each scraper twice (retry) against a known-good query and reports
pass/fail + result count + latency, then writes ``scraper_health.json``.

    venv/bin/python tests/test_scrapers_health.py
    venv/bin/python tests/test_scrapers_health.py --routes intl   # international probes

A provider "passes" if any attempt returns ≥1 result. Use this before/after
deploys and whenever /metrics/scrapers shows a provider degrading.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEP = (date.today() + timedelta(days=24)).isoformat()
CI = (date.today() + timedelta(days=24)).isoformat()
CO = (date.today() + timedelta(days=26)).isoformat()
ATTEMPTS = 2


async def _probe(name: str, coro_factory) -> dict:
    """Run a scraper up to ATTEMPTS times; pass on the first non-empty result."""
    last_err = None
    best = 0
    t0 = time.perf_counter()
    for attempt in range(1, ATTEMPTS + 1):
        try:
            res = await coro_factory()
            n = len(res or [])
            best = max(best, n)
            if n > 0:
                return {"provider": name, "ok": True, "results": n,
                        "attempt": attempt, "latency_sec": round(time.perf_counter() - t0, 1)}
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.5)
    return {"provider": name, "ok": False, "results": best,
            "error": last_err, "latency_sec": round(time.perf_counter() - t0, 1)}


def _flight_probes(routes: str):
    from app.services.booking.flights import search_flights as bk
    from app.services.easemytrip.flights import search_flights as emt
    from app.services.gotogate.flights import search_flights as gg

    # domestic: DEL→BOM ; international: DEL→DXB
    o, d = ("DEL", "BOM") if routes == "domestic" else ("DEL", "DXB")
    return [
        ("flights:booking",  lambda: bk(o, d, DEP, adults=1)),
        ("flights:easemytrip", lambda: emt(o, d, DEP, adults=1)),
        ("flights:gotogate", lambda: gg(o, d, DEP, adults=1)),
    ]


def _hotel_probes(routes: str):
    from app.services.booking.hotels import search_hotels as bk
    from app.services.easemytrip.hotels import search_hotels as emt
    from app.services.agoda.hotels import search_hotels as ag
    from app.services.ixigo.hotels import search_hotels as ix

    city = "Goa" if routes == "domestic" else "Dubai"
    return [
        ("stays:booking",   lambda: bk(city, CI, CO, adults=2)),
        ("stays:easemytrip", lambda: emt(city, CI, CO, adults=2)),
        ("stays:agoda",     lambda: ag(city, CI, CO, adults=2)),
        ("stays:ixigo",     lambda: ix(city, CI, CO, adults=2)),
    ]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--routes", choices=["domestic", "intl", "both"], default="both")
    args = ap.parse_args()

    from app.core.metrics import METRICS
    METRICS.reset()

    route_modes = ["domestic", "intl"] if args.routes == "both" else [args.routes]
    sections: dict[str, list[dict]] = {}
    for rm in route_modes:
        probes = _flight_probes(rm) + _hotel_probes(rm)
        print(f"\n▶ {rm} probes (flights {('DEL→BOM' if rm=='domestic' else 'DEL→DXB')}, "
              f"stays {'Goa' if rm=='domestic' else 'Dubai'})")
        results = await asyncio.gather(*(_probe(n, f) for n, f in probes))
        for r in results:
            mark = "✓" if r["ok"] else "✗"
            extra = f" err={r.get('error')}" if not r["ok"] else f" attempt={r['attempt']}"
            print(f"   {mark} {r['provider']:22s} results={r['results']:3d} {r['latency_sec']}s{extra}")
        sections[rm] = results

    flat = [r for rs in sections.values() for r in rs]
    passed = sum(1 for r in flat if r["ok"])
    out = {
        "generated_at": int(time.time()),
        "summary": {
            "probes": len(flat),
            "passed": passed,
            "failed": len(flat) - passed,
            "pass_pct": round(100 * passed / (len(flat) or 1), 1),
            "failing": [r["provider"] for r in flat if not r["ok"]],
        },
        "by_route": sections,
        "scraper_metrics": METRICS.snapshot(),
    }
    with open("scraper_health.json", "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n── {passed}/{len(flat)} probes passed ── failing: {out['summary']['failing'] or 'none'}")
    print("✔ wrote scraper_health.json")


if __name__ == "__main__":
    asyncio.run(main())
