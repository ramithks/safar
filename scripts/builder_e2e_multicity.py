#!/usr/bin/env python3
"""
Live end-to-end drive of the multi-city trip BUILDER — the "make me a trip leg by
leg" flow the copilot now supports:

  1. Plan a seed trip (Delhi → Manali) and wait until it's ready.
  2. Grow it into a multi-city loop entirely through the copilot:
       - "add a flight from Delhi to Manali"      ← leg
       - "add a 2 day itinerary for Manali"       ← per-stop days
       - "now from Manali to Leh"                  ← next leg
       - "add a 3 day itinerary for Leh"          ← per-stop days
       - "come back from Leh to Delhi"            ← return leg (gateway fallback
                                                     kicks in if no direct route)
  3. Print each copilot reply + intent, and the resulting graph breadcrumb,
     stops, and live cost after every turn.

Hits the REAL API (live scrapers + Vertex), so it needs a running server and a
valid auth token. Not a pytest — run it by hand:

    export TRAVBUDDY_BASE_URL=http://localhost:8000     # or prod
    export TRAVBUDDY_TOKEN=<supabase access token>
    python scripts/builder_e2e_multicity.py

The deterministic, offline version lives in tests/test_builder.py (run `pytest`).
"""
import os
import sys
import time
from datetime import date, timedelta

import httpx

BASE_URL = os.environ.get("TRAVBUDDY_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("TRAVBUDDY_TOKEN", "")
API = f"{BASE_URL}/api/v1"

DEPARTURE = (date.today() + timedelta(days=30)).isoformat()

PLAN = {
    "origin": "Delhi",
    "destination": "Manali",
    "departure_date": DEPARTURE,
    "days": 3,
    "travelers": 2,
    "budget": "mid",
    "interests": ["mountains", "adventure"],
}

# Ordered builder conversation — each is one /edit turn. The last leg (Leh →
# Delhi) is where the gateway fallback shows up when there's no direct route.
EDITS = [
    "add a flight from Delhi to Manali",
    "add a 2 day itinerary for Manali",
    "now from Manali to Leh",
    "add a 3 day itinerary for Leh",
    "come back from Leh to Delhi",
]


def _headers() -> dict:
    if not TOKEN:
        sys.exit("Set TRAVBUDDY_TOKEN to a valid Supabase access token first.")
    return {"Authorization": f"Bearer {TOKEN}"}


def create_trip(client: httpx.Client) -> str:
    r = client.post(f"{API}/trip-state", json=PLAN, headers=_headers())
    r.raise_for_status()
    trip_id = r.json()["trip_id"]
    print(f"▶ created trip {trip_id}  ({PLAN['origin']} → {PLAN['destination']}, dep {DEPARTURE})")
    return trip_id


def wait_until_ready(client: httpx.Client, trip_id: str, timeout: int = 300) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"{API}/trip-state/{trip_id}", headers=_headers())
        r.raise_for_status()
        row = r.json()
        status = row.get("status")
        if status != last:
            print(f"   … status: {status}")
            last = status
        if status in ("ready", "error"):
            return row
        time.sleep(3)
    sys.exit("Timed out waiting for the plan to become ready.")


def summarize_graph(state: dict) -> None:
    """Print the canonical graph breadcrumb, per-stop day counts, and live cost."""
    graph = state.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    costs = state.get("costs") or {}
    if not nodes:
        print("   (no graph yet)")
        return
    print(f"   route  : {' → '.join(n.get('city') for n in nodes)}")
    for n in nodes:
        print(f"     • {n.get('city')}: {n.get('nights')} nights, "
              f"{len(n.get('days') or [])} day(s) planned")
    for e in edges:
        route = (e.get("routes") or [{}])[0]
        via = f" via {route.get('gateway_city')}" if route.get("gateway_city") else ""
        print(f"     ⇢ {e.get('from_city')} → {e.get('to_city')}{via} "
              f"[{route.get('kind')}]")
    if costs:
        print(f"   cost   : ₹{costs.get('total')} (₹{costs.get('per_person')}/person)")


def run_edit(client: httpx.Client, trip_id: str, message: str) -> None:
    print(f"\n💬 “{message}”")
    r = client.post(
        f"{API}/trip-state/{trip_id}/edit",
        json={"message": message}, headers=_headers(), timeout=180.0,
    )
    if r.status_code != 200:
        print(f"   ✗ HTTP {r.status_code}: {r.text[:200]}")
        return
    out = r.json()
    print(f"   intent : {out.get('intent')}  (applied={out.get('applied')})")
    print(f"   copilot: {out.get('reply')}")
    if out.get("clarify_options"):
        print(f"   chips  : {out['clarify_options']}")

    # Re-read the snapshot so the graph summary reflects the derived projection.
    snap = client.get(f"{API}/trip-state/{trip_id}", headers=_headers())
    if snap.status_code == 200:
        summarize_graph(snap.json().get("state") or {})


def main() -> None:
    with httpx.Client(timeout=60.0) as client:
        trip_id = create_trip(client)
        row = wait_until_ready(client, trip_id)
        if row.get("status") == "error":
            sys.exit("Generation failed — check server logs.")

        for message in EDITS:
            run_edit(client, trip_id, message)

        print("\n✓ done. Final graph above is the canonical multi-city trip.")


if __name__ == "__main__":
    main()
