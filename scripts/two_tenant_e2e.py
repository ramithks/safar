#!/usr/bin/env python3
"""
Two hotels that have never heard of each other, and every way one could reach
the other.

    ./venv/bin/python scripts/two_tenant_e2e.py

── why this is not tests/test_tenant_isolation.py ──────────────────────────

That file has seventeen tests and they run against `FakeDb`. They prove the
tool layer refuses a foreign id — which is the important half, and is the half
a test double can prove.

What a double cannot prove is that the refusal survives everything underneath
it: PostgREST's own filters, the row-level policies, the property resolution
that happens before a tool is reached, and the caches that sit in front of all
of it. `room_names._CACHE` is keyed by property and lives sixty seconds;
`agent_configs` are read per run; the session is a jsonb column on a
conversation that belongs to one property. Any of those could hold a row from
the wrong hotel and no test against a fake database would notice.

So: two accounts created minutes ago, with deliberately different rooms, prices
and policies, and every id from each offered to the other through the services
the product actually calls.

── the asymmetry that makes this worth running ─────────────────────────────

A refusal is only evidence if the same call *succeeds* with the tenant's own
id. Otherwise "no rooms found" proves the fixture is broken rather than that
the boundary holds — which is exactly how the first version of the tenant
isolation tests passed while proving nothing. Every attempt below is run twice:
once with the foreign id, once with the tenant's own.
"""
import argparse
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.db.client import get_db  # noqa: E402
from app.services.ai import context as ai_context  # noqa: E402
from app.services.ai import registry  # noqa: E402

import quota_lock  # noqa: E402
import seed_tenant  # noqa: E402

A_TAG, B_TAG = "isoa", "isob"


async def _context(record: dict, conversation_id: str):
    return await ai_context.load_context(
        property_id=record["property_id"],
        partner_id=record["partner_id"],
        agent_type="guest",
        conversation_id=conversation_id,
        guest_name="Isolation Guest",
        channel="whatsapp",
        actor="guest",
    )


async def attempt(ctx, tool: str, arguments: dict) -> dict:
    """One tool call through `registry.execute`, the single door."""
    try:
        outcome = await registry.execute(ctx, tool, arguments, seq=1)
        return {"status": outcome.status, "output": outcome.output,
                "refusal": outcome.refusal_reason}
    except Exception as exc:  # noqa: BLE001
        return {"status": "raised", "output": None, "refusal": f"{type(exc).__name__}: {exc}"}


def leaked(result: dict, foreign: dict) -> str | None:
    """
    Did anything belonging to the other hotel come back?

    Checked against the *values* rather than by trusting a status: a tool that
    returns `ok` with an empty list has refused correctly, and a tool that
    returns `ok` with the other hotel's room name has not.
    """
    blob = json.dumps(result.get("output"), default=str)
    for label, value in foreign.items():
        if value and str(value) in blob:
            return f"{label} ({value}) came back"
    return None


async def run(force: bool) -> int:
    try:
        quota_lock.acquire("two_tenant_e2e", force=force)
    except quota_lock.Busy as busy:
        print(f"   {busy}")
        return 2

    print("── two fresh tenants ──")
    a = await seed_tenant.build(A_TAG)
    b = await seed_tenant.build(B_TAG)
    db = get_db()
    registry.load_all()

    # Deliberately different, so a leak is legible rather than a coincidence.
    db.table("properties").update({"name": f"{seed_tenant._MARKER} Alpha Isolation"}) \
        .eq("id", a["property_id"]).execute()
    db.table("properties").update({"name": f"{seed_tenant._MARKER} Beta Isolation"}) \
        .eq("id", b["property_id"]).execute()

    print(f"   A {a['property_id']}  {a['counts']}")
    print(f"   B {b['property_id']}  {b['counts']}")

    a_conv = a["conversations"]["ordinary"]
    b_conv = b["conversations"]["ordinary"]
    a_room = next(iter(a["room_types"].values()))
    b_room = next(iter(b["room_types"].values()))
    a_res = next(iter(a["reservations"].values()))
    b_res = next(iter(b["reservations"].values()))
    a_rate = next(iter(a["rate_plans"].values()))
    b_rate = next(iter(b["rate_plans"].values()))

    ctx_a = await _context(a, a_conv)
    ctx_b = await _context(b, b_conv)

    # Each probe names its arguments *by role*, and the roles are filled in
    # per direction below. Written this way after a first version tried to
    # rewrite one direction's dict into the other's inline and produced
    # something nobody could read — the arguments differ per direction and
    # saying so plainly is shorter than deriving it.
    def args_for(room, rate, reservation, tool):
        return {
            "get_room_details": {"room_type_id": room},
            "search_availability": {"check_in": "2026-11-10",
                                    "check_out": "2026-11-12",
                                    "room_type_id": room},
            "get_quote": {"check_in": "2026-11-10", "check_out": "2026-11-12",
                          "units": 1, "adults": 2, "room_type_id": room,
                          "rate_plan_id": rate},
            "get_reservation": {"reservation_id": reservation},
            "get_booking_status": {},
            "get_property_policies": {},
            "get_cancellation_terms": {},
        }[tool]

    TOOLS = ["get_room_details", "search_availability", "get_quote",
             "get_reservation", "get_booking_status", "get_property_policies",
             "get_cancellation_terms"]

    findings = []
    for tool in TOOLS:
        if tool not in registry.REGISTRY:
            findings.append({"tool": tool, "skipped": "no such tool"})
            print(f"  · {tool:24} not in the registry")
            continue

        a_args = args_for(a_room, a_rate, a_res, tool)
        b_args = args_for(b_room, b_rate, b_res, tool)

        # A reaching for B's ids, and the reverse.
        a_to_b = await attempt(ctx_a, tool, b_args)
        b_to_a = await attempt(ctx_b, tool, a_args)

        # The controls, without which a refusal proves nothing.
        a_own = await attempt(ctx_a, tool, a_args)
        b_own = await attempt(ctx_b, tool, b_args)

        leak_ab = leaked(a_to_b, {"B room": b_room, "B reservation": b_res,
                                  "B rate plan": b_rate, "B name": "Beta Isolation"})
        leak_ba = leaked(b_to_a, {"A room": a_room, "A reservation": a_res,
                                  "A rate plan": a_rate, "A name": "Alpha Isolation"})
        controls_worked = all(r["status"] in ("executed", "replayed")
                              for r in (a_own, b_own))

        findings.append({
            "tool": tool,
            "a_to_b": a_to_b["status"], "a_to_b_leak": leak_ab,
            "b_to_a": b_to_a["status"], "b_to_a_leak": leak_ba,
            "a_own": a_own["status"], "b_own": b_own["status"],
            "controls_worked": controls_worked,
            "control_refusal": a_own["refusal"] or b_own["refusal"],
        })
        mark = "✓" if not leak_ab and not leak_ba and controls_worked else "✗"
        print(f"  {mark} {tool:24} A→B {a_to_b['status']:<10} "
              f"B→A {b_to_a['status']:<10} own {a_own['status']}/{b_own['status']}")
        for problem in (leak_ab, leak_ba):
            if problem:
                print(f"       ✗ LEAK: {problem}")
        if not controls_worked:
            print(f"       ✗ a control failed ({a_own['refusal'] or b_own['refusal']}) "
                  f"— a refusal here proves nothing")

    breaches = [f for f in findings
                if f.get("a_to_b_leak") or f.get("b_to_a_leak")]
    blind = [f for f in findings if f.get("controls_worked") is False]

    print(f"\n   cross-tenant leaks: {len(breaches)}")
    print(f"   probes whose control failed (and so prove nothing): {len(blind)}")

    pathlib.Path("/tmp/two_tenant.json").write_text(
        json.dumps({"a": a["property_id"], "b": b["property_id"],
                    "findings": findings}, indent=1, default=str))

    quota_lock.release("two_tenant_e2e")
    print(f"   {seed_tenant.destroy(A_TAG)}")
    print(f"   {seed_tenant.destroy(B_TAG)}")
    return 1 if breaches else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-quota", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.force_quota))


if __name__ == "__main__":
    sys.exit(main())
