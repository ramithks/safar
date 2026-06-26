"""
Gold-standard route + persona evaluation harness for the journey/plan agent.

Runs a battery of *hard* routes — small Indian towns, landmarks, micro-states,
and remote regions whose gateway airport is non-obvious — through the real
pipeline (Gemini route planner + live scrapers + the new scoring/reasoning),
auto-scores each result against the evaluation rubric, and writes everything
(full results + per-case scorecard + live scraper metrics) to ``result_test.json``.

Run (from repo root, in the project venv):

    venv/bin/python tests/eval_plan_routes.py                 # journey mode (default)
    venv/bin/python tests/eval_plan_routes.py --mode plan     # full /plan (adds itinerary)
    venv/bin/python tests/eval_plan_routes.py --limit 5       # first 5 cases only
    venv/bin/python tests/eval_plan_routes.py --only gold     # one category
    venv/bin/python tests/eval_plan_routes.py --concurrency 2

Modes
-----
- ``journey`` (default): calls ``run_plan_journey`` directly with an empty
  profile. No DB writes, faster, and still returns the complete journey
  (legs + gateway resolution + flights + hotels + journey_options + reasoning +
  cost/visa). This is exactly what the route benchmark evaluates.
- ``plan``: calls ``supervisor_run`` — the full ``/plan`` (journey + itinerary +
  budget agent + return journey). DB access is stubbed so nothing is persisted
  and no real user is needed.

Each case is scored on the rubric: input understanding, destination
normalization, airport selection, route planning, scraper success, fallback
behaviour, response quality.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from collections import Counter
from datetime import date, timedelta

# Allow `python tests/eval_plan_routes.py` from repo root (not just -m).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Departure date: a few weeks out so the not-past validator passes ───────────
DEPARTURE_DATE = (date.today() + timedelta(days=24)).isoformat()
TEST_USER_ID = os.environ.get("TEST_USER_ID", "00000000-0000-0000-0000-000000000000")
OUTPUT_FILE = os.environ.get("RESULT_FILE", "result_test.json")


# ── Case matrix ────────────────────────────────────────────────────────────────
# expected: IATA codes OR city-name tokens we hope the agent routes through.
# flags:  multi  = a hub leg is expected (no sensible direct service)
#         intl   = an international air leg is expected
def _case(cid, origin, dest, expected, *, budget="mid", travelers=2,
          category="gold", multi=True, intl=True, vibe=None):
    return {
        "id": cid, "origin": origin, "destination": dest,
        "expected_gateways": [e.lower() for e in expected],
        "budget": budget, "travelers": travelers, "category": category,
        "expect_multi_leg": multi, "expect_flight": intl, "vibe": vibe,
    }


GOLD_CASES = [
    _case("gold-bhiwani-ireland",   "Bhiwani",   "Ireland",        ["del", "dub", "delhi", "dublin"]),
    _case("gold-kasol-iceland",     "Kasol",     "Iceland",        ["kuu", "del", "ded", "kef", "bhuntar", "reykjavik", "keflavik"]),
    _case("gold-tawang-vatican",    "Tawang",    "Vatican City",   ["gau", "guwahati", "rom", "fco", "rome"]),
    _case("gold-hampi-borabora",    "Hampi",     "Bora Bora",      ["hbx", "blr", "hubli", "bengaluru", "ppt", "papeete", "bob"]),
    _case("gold-triund-hallstatt",  "Triund",    "Hallstatt",      ["dharamshala", "dhm", "szg", "vie", "salzburg", "vienna"]),
    _case("gold-delhi-liechtenstein","Delhi",    "Liechtenstein",  ["zrh", "zurich", "del"]),
    _case("gold-delhi-monaco",      "Delhi",     "Monaco",         ["nce", "nice", "del"]),
    _case("gold-delhi-andorra",     "Delhi",     "Andorra",        ["bcn", "barcelona", "del"]),
    _case("gold-delhi-yellowstone", "Delhi",     "Yellowstone",    ["boz", "jac", "bozeman", "jackson", "del"]),
    _case("gold-delhi-serengeti",   "Delhi",     "Serengeti",      ["jro", "arusha", "kilimanjaro", "del"]),
    _case("gold-abu-luxembourg",    "Mount Abu", "Luxembourg",     ["udr", "amd", "lux", "udaipur", "luxembourg"]),
    _case("gold-diu-greenland",     "Diu",       "Greenland",      ["diu", "raj", "rajkot", "nuuk", "goh", "kangerlussuaq"]),
    _case("gold-leh-faroe",         "Leh",       "Faroe Islands",  ["ixl", "del", "fae", "vagar", "leh"]),
    _case("gold-shillong-easter",   "Shillong",  "Easter Island",  ["shl", "gau", "ipc", "mataveri", "guwahati"]),
    _case("gold-portblair-svalbard","Port Blair","Svalbard",       ["ixz", "lyr", "longyearbyen", "port blair"]),
]

SMALL_TOWN_CASES = [
    _case("st-bhiwani-nyc",      "Bhiwani",     "New York City", ["del", "jfk", "ewr", "delhi"],            category="small-town"),
    _case("st-kasol-paris",      "Kasol",       "Paris",         ["kuu", "ded", "del", "cdg", "bhuntar"],   category="small-town"),
    _case("st-triund-tokyo",     "Triund",      "Tokyo",         ["dharamshala", "dhm", "del", "hnd", "nrt"],category="small-town"),
    _case("st-tawang-sydney",    "Tawang",      "Sydney",        ["gau", "guwahati", "del", "syd"],         category="small-town"),
    _case("st-kaza-rome",        "Kaza",        "Rome",          ["kullu", "shimla", "del", "fco", "rome"], category="small-town"),
    _case("st-chikmagalur-dubai","Chikmagalur", "Dubai",         ["ixe", "mangalore", "blr", "dxb"],        category="small-town"),
    _case("st-abu-singapore",    "Mount Abu",   "Singapore",     ["udr", "amd", "udaipur", "ahmedabad", "sin"], category="small-town"),
    _case("st-diu-toronto",      "Diu",         "Toronto",       ["diu", "raj", "rajkot", "yyz", "yto"],    category="small-town"),
    _case("st-hampi-amsterdam",  "Hampi",       "Amsterdam",     ["hbx", "blr", "hubli", "bengaluru", "ams"], category="small-town"),
    _case("st-auli-london",      "Auli",        "London",        ["ded", "dehradun", "del", "lhr", "lon"],  category="small-town"),
]

# Persona matrix — same hotel-rich destinations across budget × group size, to
# check property-type relevancy (hostel vs hotel vs resort) and price banding.
# Domestic short hops so the signal is the *hotels*, not the routing.
PERSONA_CASES = [
    _case("p-manali-solo-low",    "Delhi", "Manali", ["del", "kuu", "manali"], budget="low",  travelers=1, category="persona", multi=True,  intl=False, vibe="backpacker / solo"),
    _case("p-manali-couple-mid",  "Delhi", "Manali", ["del", "kuu", "manali"], budget="mid",  travelers=2, category="persona", multi=True,  intl=False, vibe="couple getaway"),
    _case("p-goa-friends-low",    "Delhi", "Goa",    ["del", "goi", "goa"],    budget="low",  travelers=4, category="persona", multi=False, intl=False, vibe="friends party"),
    _case("p-goa-family-high",    "Delhi", "Goa",    ["del", "goi", "goa"],    budget="high", travelers=4, category="persona", multi=False, intl=False, vibe="family resort"),
    _case("p-dubai-couple-high",  "Delhi", "Dubai",  ["del", "dxb", "dubai"],  budget="high", travelers=2, category="persona", multi=False, intl=True,  vibe="luxury couple"),
]

ALL_CASES = GOLD_CASES + SMALL_TOWN_CASES + PERSONA_CASES


# ── DB stub (plan mode only) — keeps the harness from touching Supabase ────────
class _FakeResp:
    def __init__(self, data=None):
        self.data = data if data is not None else []


class _FakeQuery:
    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return _FakeResp([])


class _FakeTable:
    def __getattr__(self, _name):
        return lambda *a, **k: _FakeQuery()


class _FakeDB:
    def table(self, _name):
        return _FakeTable()


def _stub_db():
    """Patch get_db everywhere it was imported so plan mode persists nothing."""
    import app.db.client as client_mod
    fake = _FakeDB()
    client_mod.get_db = lambda: fake  # type: ignore
    for modname in (
        "app.agents.supervisor",
        "app.api.v1.plan_trip.journey",
        "app.api.v1.plan_trip.orchestrator",
    ):
        try:
            mod = __import__(modname, fromlist=["get_db"])
            if hasattr(mod, "get_db"):
                mod.get_db = lambda: fake  # type: ignore
        except Exception:
            pass


# ── Run one case ───────────────────────────────────────────────────────────────
async def _run_case(case: dict, mode: str) -> dict:
    from app.api.v1.plan_trip.models import PlanJourneyRequest, PlanRequest

    started = time.perf_counter()
    try:
        if mode == "plan":
            from app.agents.supervisor import supervisor_run
            body = PlanRequest(
                origin=case["origin"], destination=case["destination"],
                departure_date=DEPARTURE_DATE, days=case["travelers"] and 4 or 4,
                travelers=case["travelers"], budget=case["budget"],
            )
            result = await supervisor_run(body, TEST_USER_ID)
        else:
            from app.api.v1.plan_trip.journey import run_plan_journey
            req = PlanJourneyRequest(
                origin=case["origin"], destination=case["destination"],
                travelers=case["travelers"], budget=case["budget"],
                departure_date=DEPARTURE_DATE, stay_nights=3,
            )
            result = await run_plan_journey(req, TEST_USER_ID, profile={})
        elapsed = round(time.perf_counter() - started, 1)
        return {"ok": True, "elapsed_sec": elapsed, "result": result}
    except Exception as exc:  # noqa: BLE001
        elapsed = round(time.perf_counter() - started, 1)
        return {"ok": False, "elapsed_sec": elapsed, "error": f"{type(exc).__name__}: {exc}"}


# ── Extract a normalized view from either mode's result ────────────────────────
def _extract(result: dict, mode: str) -> dict:
    if mode == "plan":
        going = result.get("going") or {}
        legs = going.get("legs") or []
        hotels = (going.get("hotels") or {}).get("items") or []
        flights_block = (going.get("flights") or {}).get("items") or []
        reasoning = going.get("reasoning")
        journey_options = going.get("journey_options") or {}
        summary = result.get("journey_summary")
        is_intl = result.get("is_international")
        cost = result.get("cost_breakdown") or {}
    else:
        legs = result.get("legs") or []
        hotels = (result.get("destination_hotels") or {}).get("hotels") or []
        flights_block = []
        reasoning = result.get("journey_reasoning")
        journey_options = result.get("journey_options") or {}
        summary = result.get("journey_summary")
        is_intl = result.get("is_international")
        cost = result.get("cost_breakdown") or {}

    # Flights live on flight-mode legs; also fold in any shaped flight items.
    flights: list[dict] = list(flights_block)
    resolved_iatas: set[str] = set()
    leg_cities: set[str] = set()
    flight_legs = 0
    for leg in legs:
        sp = leg.get("search_params") or {}
        for k in ("from_iata", "to_iata"):
            if sp.get(k):
                resolved_iatas.add(str(sp[k]).lower())
        for k in ("from", "to"):
            if leg.get(k):
                leg_cities.add(str(leg[k]).lower())
        if leg.get("mode", "").lower() == "flight":
            flight_legs += 1
            flights.extend((leg.get("live_data") or {}).get("flights") or [])

    return {
        "legs": legs, "hotels": hotels, "flights": flights,
        "resolved_iatas": resolved_iatas, "leg_cities": leg_cities,
        "flight_legs": flight_legs, "reasoning": reasoning,
        "journey_options": journey_options, "summary": summary,
        "is_international": is_intl, "cost_breakdown": cost,
    }


def _hotel_prices(hotels: list[dict]) -> list[float]:
    out = []
    for h in hotels:
        p = h.get("price_per_night") or h.get("estimated_price_per_night")
        try:
            if p:
                out.append(float(p))
        except (TypeError, ValueError):
            pass
    return out


# ── Score one result against the rubric ────────────────────────────────────────
def _evaluate(case: dict, run: dict, mode: str) -> dict:
    if not run["ok"]:
        return {
            "passed": 0, "total": 7, "scorecard": {k: False for k in _RUBRIC},
            "notes": [f"pipeline error: {run['error']}"],
        }

    e = _extract(run["result"], mode)
    legs = e["legs"]
    notes: list[str] = []

    # Gateway hit: an expected IATA/token shows up in resolved IATAs or leg cities.
    haystack = e["resolved_iatas"] | e["leg_cities"]
    hay_join = " ".join(haystack)
    gateway_hit = any(
        tok in e["resolved_iatas"] or tok in hay_join
        for tok in case["expected_gateways"]
    )

    num_legs = len(legs)
    flights = e["flights"]
    hotels = e["hotels"]
    prices = _hotel_prices(hotels)
    ptypes = Counter((h.get("property_type") or "unknown") for h in hotels)

    has_resolved_air = bool(e["resolved_iatas"]) and e["flight_legs"] > 0
    reached = any(
        case["destination"].split(",")[0].strip().lower() in c
        or c in case["destination"].lower()
        for c in e["leg_cities"]
    )

    sc = {
        # Did we get a structured multi-leg plan at all?
        "input_understanding": num_legs >= 1,
        # Did aliases/landmarks/countries map onto a routable target?
        "destination_normalization": gateway_hit or reached,
        # Was a sensible gateway airport chosen (for air routes)?
        "airport_selection": has_resolved_air if case["expect_flight"] else True,
        # Realistic multi-leg journey where direct travel isn't possible?
        "route_planning": num_legs >= (2 if case["expect_multi_leg"] else 1),
        # Did scrapers return anything usable?
        "scraper_success": bool(flights) or bool(hotels),
        # Did obscure origins still resolve (no silent empty)?
        "fallback_behavior": bool(e["resolved_iatas"]) or num_legs >= 2,
        # Did we explain the journey / trade-offs rather than fail silently?
        "response_quality": bool(e["reasoning"]) or bool(e["summary"]),
    }

    if not gateway_hit:
        notes.append(
            f"expected gateway none of {case['expected_gateways']} — "
            f"resolved IATAs={sorted(e['resolved_iatas'])} cities={sorted(e['leg_cities'])}"
        )
    if not flights and case["expect_flight"]:
        notes.append("no live flights returned for an international route")
    if not hotels:
        notes.append("no hotels returned")

    return {
        "passed": sum(1 for v in sc.values() if v),
        "total": len(sc),
        "scorecard": sc,
        "notes": notes,
        "observed": {
            "num_legs": num_legs,
            "modes": [leg.get("mode") for leg in legs],
            "resolved_iatas": sorted(e["resolved_iatas"]),
            "flight_legs": e["flight_legs"],
            "flights_count": len(flights),
            "cheapest_flight": min((f.get("price_total") for f in flights if f.get("price_total")), default=None),
            "hotels_count": len(hotels),
            "hotel_property_types": dict(ptypes),
            "hotel_price_min": min(prices) if prices else None,
            "hotel_price_median": round(statistics.median(prices), 0) if prices else None,
            "hotel_price_max": max(prices) if prices else None,
            "top_hotel": (hotels[0].get("name") if hotels else None),
            "top_hotel_scores": (
                {"rank_score": hotels[0].get("rank_score"), "quality_score": hotels[0].get("quality_score")}
                if hotels else None
            ),
            "is_international": e["is_international"],
            "has_reasoning": bool(e["reasoning"]),
            "reasoning_headline": (e["reasoning"] or {}).get("headline") if e["reasoning"] else None,
            "total_cost_per_person": (e["cost_breakdown"] or {}).get("total_per_person"),
        },
    }


_RUBRIC = [
    "input_understanding", "destination_normalization", "airport_selection",
    "route_planning", "scraper_success", "fallback_behavior", "response_quality",
]


# ── Driver ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["journey", "plan"], default="journey")
    ap.add_argument("--limit", type=int, default=0, help="run only first N cases")
    ap.add_argument("--only", default="", help="category filter: gold|small-town|persona")
    ap.add_argument("--concurrency", type=int, default=2)
    args = ap.parse_args()

    if args.mode == "plan":
        _stub_db()

    from app.core.metrics import METRICS
    METRICS.reset()

    cases = ALL_CASES
    if args.only:
        cases = [c for c in cases if c["category"] == args.only]
    if args.limit:
        cases = cases[: args.limit]

    print(f"▶ {len(cases)} cases | mode={args.mode} | departure={DEPARTURE_DATE} "
          f"| concurrency={args.concurrency}")

    sem = asyncio.Semaphore(args.concurrency)
    records: list[dict] = [None] * len(cases)  # type: ignore

    async def _worker(idx: int, case: dict):
        async with sem:
            print(f"  … [{idx+1}/{len(cases)}] {case['id']}: {case['origin']} → {case['destination']}")
            run = await _run_case(case, args.mode)
            ev = _evaluate(case, run, args.mode)
            status = "✓" if run["ok"] else "✗"
            print(f"    {status} {case['id']}: {ev['passed']}/{ev['total']} "
                  f"({run['elapsed_sec']}s)" + (f" — {ev['notes'][0]}" if ev["notes"] else ""))
            records[idx] = {
                "case": case,
                "elapsed_sec": run["elapsed_sec"],
                "ok": run["ok"],
                "error": run.get("error"),
                "evaluation": ev,
                "result": run.get("result"),
            }

    await asyncio.gather(*(_worker(i, c) for i, c in enumerate(cases)))

    # ── Aggregate scorecard ────────────────────────────────────────────────────
    crit_pass = {k: 0 for k in _RUBRIC}
    total_pass = 0
    total_pts = 0
    by_cat: dict[str, dict] = {}
    for r in records:
        ev = r["evaluation"]
        total_pass += ev["passed"]
        total_pts += ev["total"]
        for k, v in ev["scorecard"].items():
            crit_pass[k] += 1 if v else 0
        cat = r["case"]["category"]
        by_cat.setdefault(cat, {"cases": 0, "passed": 0, "points": 0})
        by_cat[cat]["cases"] += 1
        by_cat[cat]["passed"] += ev["passed"]
        by_cat[cat]["points"] += ev["total"]

    n = len(records) or 1
    summary = {
        "mode": args.mode,
        "departure_date": DEPARTURE_DATE,
        "cases_run": len(records),
        "overall_score_pct": round(100 * total_pass / (total_pts or 1), 1),
        "criteria_pass_rate_pct": {k: round(100 * v / n, 1) for k, v in crit_pass.items()},
        "by_category": {
            c: {**v, "score_pct": round(100 * v["passed"] / (v["points"] or 1), 1)}
            for c, v in by_cat.items()
        },
    }

    out = {
        "generated_at": int(time.time()),
        "summary": summary,
        "scraper_metrics": METRICS.snapshot(),
        "cases": records,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2, default=str)

    print("\n── SUMMARY ─────────────────────────────────────────")
    print(f"  overall: {summary['overall_score_pct']}%  ({total_pass}/{total_pts} rubric points)")
    for k, v in summary["criteria_pass_rate_pct"].items():
        print(f"  {k:28s} {v:5.1f}%")
    print("  by category:")
    for c, v in summary["by_category"].items():
        print(f"    {c:12s} {v['score_pct']:5.1f}%  ({v['cases']} cases)")
    print(f"\n✔ wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
