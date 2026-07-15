#!/usr/bin/env python3
"""
Live end-to-end drive of the trip planner + copilot, exactly as a user would:

  1. Plan a real trip: Bhiwani → Meghalaya.
  2. Stream generation until the plan is ready.
  3. Talk to the copilot the way the user asked:
       - "give me the cheapest hotel"
       - "add more spots to the itinerary"
       - "find me a flight with more luggage allowance"
       - "a hotel with a swimming pool"
       - "a hotel under 1000"
       - "add Nainital in between"          ← stopover / waypoint
       - "and a pet-friendly stay near the lake"  ← follow-up filter
  4. Print the copilot's reply + intent for each turn.

This hits the REAL API (live scrapers + Vertex), so it needs a running server
and a valid auth token. It is intentionally NOT a pytest — run it by hand:

    export TRAVBUDDY_BASE_URL=http://localhost:8000     # or prod
    export TRAVBUDDY_TOKEN=<supabase access token>
    python scripts/copilot_e2e_bhiwani_meghalaya.py

The deterministic, offline version of these edits lives in
tests/test_copilot_edits.py (run with `pytest`).
"""
import json
import os
import sys
import time
from datetime import date, timedelta

import httpx

BASE_URL = os.environ.get("TRAVBUDDY_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("TRAVBUDDY_TOKEN", "")
API = f"{BASE_URL}/api/v1"

# A month out so departure_date validation passes and live inventory exists.
DEPARTURE = (date.today() + timedelta(days=30)).isoformat()

PLAN = {
    "origin": "Bhiwani",
    "destination": "Meghalaya",
    "departure_date": DEPARTURE,
    "days": 4,
    "travelers": 2,
    "budget": "mid",
    "interests": ["nature", "food", "adventure"],
}

# Ordered copilot conversation — each is one /edit turn.
EDITS = [
    "give me the cheapest hotel",
    "add more spots to the itinerary",
    "find me a flight with more luggage allowance",
    "show me a hotel with a swimming pool",
    "a hotel under 1000 per night",
    "add Nainital in between on the way",
    "now a pet-friendly stay near the lake under 3000",
]


def _headers() -> dict:
    if not TOKEN:
        sys.exit("Set TRAVBUDDY_TOKEN to a valid Supabase access token first.")
    return {"Authorization": f"Bearer {TOKEN}"}


def create_trip(client: httpx.Client) -> str:
    r = client.post(f"{API}/trip-state", json=PLAN, headers=_headers())
    r.raise_for_status()
    trip_id = r.json()["trip_id"]
    print(f"▶ created trip {trip_id}  ({PLAN['origin']} → {PLAN['destination']}, "
          f"{PLAN['days']}d, dep {DEPARTURE})")
    return trip_id


def wait_until_ready(client: httpx.Client, trip_id: str, timeout: int = 300) -> dict:
    """Poll the snapshot until status is ready/error (simpler than SSE here)."""
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


def summarize_plan(state: dict) -> None:
    days = (state.get("itinerary") or {}).get("days") or []
    stay = (state.get("stay") or {}).get("selected") or {}
    costs = state.get("costs") or {}
    print(f"\n✓ plan ready — {len(days)} days, "
          f"{sum(len(d.get('activities') or []) for d in days)} activities")
    for d in days:
        acts = ", ".join(a.get("title") for a in (d.get("activities") or []))
        print(f"   Day {d.get('day')}: {acts}")
    if stay:
        print(f"   stay: {stay.get('name')} "
              f"(₹{stay.get('price_per_night') or stay.get('price')}/night)")
    if costs:
        print(f"   total: ₹{costs.get('total')} (₹{costs.get('per_person')}/person)")


def run_edit(client: httpx.Client, trip_id: str, message: str) -> None:
    print(f"\n💬 “{message}”")
    r = client.post(
        f"{API}/trip-state/{trip_id}/edit",
        json={"message": message}, headers=_headers(), timeout=120.0,
    )
    if r.status_code != 200:
        print(f"   ✗ HTTP {r.status_code}: {r.text[:200]}")
        return
    out = r.json()
    print(f"   intent : {out.get('intent')}  (applied={out.get('applied')})")
    print(f"   copilot: {out.get('reply')}")


def main() -> None:
    with httpx.Client(timeout=60.0) as client:
        trip_id = create_trip(client)
        row = wait_until_ready(client, trip_id)
        if row.get("status") == "error":
            sys.exit("Generation failed — check server logs.")
        summarize_plan(row.get("state") or {})

        for message in EDITS:
            run_edit(client, trip_id, message)

        # Final snapshot after all edits.
        r = client.get(f"{API}/trip-state/{trip_id}", headers=_headers())
        r.raise_for_status()
        print("\n──── final plan ────")
        summarize_plan(r.json().get("state") or {})
        print(f"\nfull state: {API}/trip-state/{trip_id}")


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        sys.exit(f"HTTP error: {exc.response.status_code} {exc.response.text[:300]}")
    except json.JSONDecodeError as exc:
        sys.exit(f"Bad JSON from server: {exc}")
