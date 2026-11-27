#!/usr/bin/env python3
"""
What a guest turn actually costs, measured rather than assumed.

§18 and §19 both ask for numbers before changes, and this codebase has been
wrong about this before: an earlier phase assumed the expensive turns were
expensive because `get_stay_options` returned a fat matrix, measured it, and
found it averages 1,188 characters. The cost was the tool *catalogue*, re-sent
on every model step.

So this measures four things separately, because they are four different
problems with four different fixes:

    system prompt      the fixed cost of existing
    tool schemas       the catalogue, per task
    session state      what obligations and the session add
    tool results       what comes back

It reads `agent_runs` for what production actually spent, and composes prompts
locally for what each task would cost today. Nothing here calls a model.

    cd safar_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/token_audit.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json  # noqa: E402
from collections import defaultdict  # noqa: E402

from app.db.client import get_db  # noqa: E402
from app.services.ai import registry, toolsets  # noqa: E402
from app.services.ai.agents import guest as guest_agent  # noqa: E402
from app.services.ai.agents import sales as sales_agent  # noqa: E402

# Roughly four characters to a token for English, and the ratio holds well
# enough for Hinglish. Used only for relative comparisons — every absolute
# number in the production section below is Vertex's own count.
CHARS_PER_TOKEN = 4


def approx_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


def schema_size(names: set[str]) -> tuple[int, int]:
    """Bytes and approximate tokens of the tool schemas for this set."""
    described = []
    for name in sorted(names):
        spec = registry.REGISTRY.get(name)
        if not spec:
            continue
        described.append({
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_model.model_json_schema(),
        })
    blob = json.dumps(described)
    return len(blob), approx_tokens(blob)


def measure_menus() -> None:
    print("\n══ 1. the tool catalogue, per task ══")
    print("   What every model step re-sends. Measured for the Sales agent,")
    print("   which holds the most tools and therefore pays the most.\n")

    granted = frozenset(sales_agent.DEFINITION.tools)
    everything_bytes, everything_tokens = schema_size(set(granted))
    print(f"   {'task':26} {'tools':>5} {'bytes':>7} {'~tokens':>8}  saving")
    print(f"   {'-' * 26} {'-' * 5} {'-' * 7} {'-' * 8}  ------")
    print(f"   {'(everything granted)':26} {len(granted):>5} {everything_bytes:>7} "
          f"{everything_tokens:>8}       —")

    rows = []
    for task in sorted(toolsets.BY_TASK):
        names = toolsets.for_task(task, granted)
        size, tokens = schema_size(set(names))
        saving = round((1 - size / everything_bytes) * 100) if everything_bytes else 0
        rows.append((task, len(names), size, tokens, saving))

    for task, count, size, tokens, saving in sorted(rows, key=lambda r: r[2]):
        print(f"   {task:26} {count:>5} {size:>7} {tokens:>8}  {saving:>4}%")

    print(f"\n   Guest agent, everything granted: "
          f"{schema_size(set(guest_agent.DEFINITION.tools))[0]} bytes")


def measure_state() -> None:
    """
    What the session and obligations add to a prompt.

    The concern §19 raises directly: structured state is only cheaper than
    history if it stays small. This is the check that it has.
    """
    from app.services.messaging import obligations, session

    print("\n══ 2. what structured state costs ══")

    profile = {
        "check_in": "2026-09-10", "check_out": "2026-09-14",
        "adults": 4, "children": 0, "units": 2, "stage": "quoted",
        "quote_id": "6b1f…", "room_type_id": "c3a…", "rate_plan_id": "9de…",
        "negotiation": "counter", "payment": "PAYMENT_PENDING",
        "awaiting_hotel": {"id": "e1", "kind": "discount_request", "notified": True},
        "obligations": {"meals": 1, "services": 2, "negotiation": 0},
    }
    told = session.describe({"guest_profile": profile}) or ""
    outstanding = obligations.describe(profile) or ""

    print(f"   session line       {len(told):>4} chars  ~{approx_tokens(told):>3} tokens")
    print(f"   obligations line   {len(outstanding):>4} chars  ~{approx_tokens(outstanding):>3} tokens")
    print(f"   stored json        {len(json.dumps(profile)):>4} chars  (never sent to the model)")

    # What the alternative would have cost. Ten turns of history is what
    # `_history` carries, and re-deriving obligations from it every turn is
    # exactly the design this avoids.
    print(f"\n   For comparison, ten message bodies at ~120 chars each: "
          f"~{approx_tokens('x' * 1200)} tokens")
    print("   Structured state replaces the *re-derivation*, not the history —")
    print("   history is still carried, and is still the smaller half.")


def measure_production() -> None:
    """What production actually spent, from `agent_runs`."""
    print("\n══ 3. what production actually spent ══")

    db = get_db()
    rows = (
        db.table("agent_runs")
        .select("task,model_tier,tokens_in,tokens_out,latency_ms,status,model")
        .order("started_at", desc=True).limit(1000).execute().data or []
    )
    if not rows:
        print("   no runs recorded")
        return

    by_task: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_task[row.get("task") or "(none)"].append(row)

    print(f"   {'task':26} {'runs':>5} {'tok_in':>8} {'tok_out':>8} {'p50 ms':>8} {'p95 ms':>8}")
    print(f"   {'-' * 26} {'-' * 5} {'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}")

    def pct(values: list[int], p: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[min(int(len(ordered) * p), len(ordered) - 1)]

    for task, runs in sorted(by_task.items(), key=lambda kv: -len(kv[1])):
        tin = [int(r.get("tokens_in") or 0) for r in runs]
        tout = [int(r.get("tokens_out") or 0) for r in runs]
        lat = [int(r.get("latency_ms") or 0) for r in runs if r.get("latency_ms")]
        print(f"   {task:26} {len(runs):>5} {round(sum(tin)/len(tin)):>8} "
              f"{round(sum(tout)/len(tout)):>8} {pct(lat, 0.5):>8} {pct(lat, 0.95):>8}")

    lat_all = [int(r.get("latency_ms") or 0) for r in rows if r.get("latency_ms")]
    slow = [ms for ms in lat_all if ms > 30_000]
    failed = [r for r in rows if r.get("status") not in ("succeeded", None)]
    print(f"\n   over all {len(rows)} runs:")
    print(f"     p50 {pct(lat_all, 0.5):>6} ms   p95 {pct(lat_all, 0.95):>6} ms   "
          f"p99 {pct(lat_all, 0.99):>6} ms   max {max(lat_all or [0]):>6} ms")
    print(f"     over 30s: {len(slow)} ({round(100 * len(slow) / max(len(lat_all), 1))}%)")
    print(f"     not succeeded: {len(failed)} "
          f"({round(100 * len(failed) / len(rows))}%)")

    if slow:
        print("\n   The slow ones, by task:")
        by_slow: dict[str, int] = defaultdict(int)
        for row in rows:
            if int(row.get("latency_ms") or 0) > 30_000:
                by_slow[row.get("task") or "(none)"] += 1
        for task, count in sorted(by_slow.items(), key=lambda kv: -kv[1]):
            print(f"     {task:26} {count}")


def measure_tool_results() -> None:
    """How big a tool's answer is, from what production recorded."""
    print("\n══ 4. what tools return ══")

    db = get_db()
    rows = (
        db.table("agent_actions").select("tool,output")
        .order("created_at", desc=True).limit(1500).execute().data or []
    )
    sizes: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        output = row.get("output")
        if output is None:
            continue
        sizes[row.get("tool") or "?"].append(len(json.dumps(output)))

    if not sizes:
        print("   no tool outputs recorded")
        return

    print(f"   {'tool':30} {'calls':>6} {'avg':>7} {'max':>7} {'~avg tok':>9}")
    print(f"   {'-' * 30} {'-' * 6} {'-' * 7} {'-' * 7} {'-' * 9}")
    for tool, values in sorted(sizes.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))[:14]:
        avg = round(sum(values) / len(values))
        print(f"   {tool:30} {len(values):>6} {avg:>7} {max(values):>7} "
              f"{approx_tokens('x' * avg):>9}")


def main() -> int:
    registry.load_all()
    measure_menus()
    measure_state()
    try:
        measure_production()
        measure_tool_results()
    except Exception as exc:  # noqa: BLE001
        print(f"\n   (could not read production runs: {type(exc).__name__}: {exc})")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
