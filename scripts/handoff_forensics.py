"""
Why does Gemini 3.x hand a negotiation to a human?

Not a scorecard. A scorecard already told us *that* 3.x hands off — 87.3% where
2.5 hands off 15.4% — and a scorecard cannot say why, which is the only thing
worth knowing before changing anything. §2 of the brief is explicit about the
order: reproduce, capture, and do not assume the cause is the model.

So this captures the whole decision surface for one turn and prints it beside
itself for both model families:

    system prompt          length, and every line mentioning handoff
    tools exposed          names, in the order the provider receives them
    tool descriptions      handoff and negotiation, verbatim
    tool schema            what `reason` says it accepts
    session state          what the turn already knows
    detected intent        what required_actions decided, before the model ran
    required action        the tool the turn owes
    model tool calls       every one, with its arguments
    handoff reason         the string the model chose, and its note
    autonomy               the level the tools were gated at
    final response         what the guest would have read

Everything above the horizontal rule is static — it is identical on every run
and is printed once — and everything below it varies. That split is the point:
if the static surface is the same for both models and only the behaviour
differs, the cause is in how the two models read an identical prompt, and the
fix has to be a layer that does not depend on how a model reads anything.

    python scripts/handoff_forensics.py --runs 30
    python scripts/handoff_forensics.py --runs 30 --message "Best price kya de sakte ho?"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db.client import get_db  # noqa: E402
from app.services.ai import context as ai_context  # noqa: E402
from app.services.ai import model_router, prompts, registry, required_actions, runtime  # noqa: E402

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

db = get_db()

# The two sentences the model comparison disagreed most violently about.
DEFAULT_MESSAGE = "Best price kya de sakte ho?"

CONVERSATIONS: list[str] = []


async def session_for(index: int, priced: bool) -> dict:
    """
    The state the conversation is in when the guest asks.

    Two of them, because they are two different questions and the first
    forensics run conflated them:

      **fresh** — the guest has said what they want but nothing has priced it.
      No rate plan on the session. This is the normal first turn and, as it
      turns out, the one where the deterministic layer has no opinion.

      **priced** — a real sellable rate plan on real nights, which is what
      `agent_eval` measures against and the only state where the negotiation
      requirement is `must_execute`.
    """
    check_in = date.today() + timedelta(days=45 + index * 7)
    check_out = check_in + timedelta(days=2)
    session = {
        "check_in": str(check_in),
        "check_out": str(check_out),
        "adults": 2,
        "children": 0,
        "units": 1,
    }
    if priced:
        found = await a_rate_plan(check_in, check_out)
        if found:
            session.update(found)
    return session


async def a_rate_plan(check_in: date, check_out: date) -> dict | None:
    """
    A room and a rate the hotel is actually selling on these nights.

    Read through `stay_options` for the same reason `agent_eval` does: a plan
    that is closed or under its minimum stay is not one a negotiation can be
    about, and negotiating it would measure the engine's refusal path rather
    than the model's tool choice.
    """
    from app.services.pms import availability as availability_service

    options = await availability_service.stay_options(
        PROPERTY, check_in, check_out, units=1, audience="public",
    )
    for option in options:
        for plan in option.rate_plans:
            if not plan.closed and plan.min_stay_violation is None:
                return {
                    "room_type_id": option.room_type_id,
                    "room_name": option.room_type_name,
                    "rate_plan_id": plan.rate_plan_id,
                    "stage": "quoted",
                }
    return None


def make_conversation() -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "property_id": PROPERTY,
        "partner_id": PARTNER,
        "channel": "whatsapp",
        "handler": "ai",
        "status": "open",
        "guest_name": "Forensics Guest",
        "guest_phone": f"+9198{int(time.time() * 1000) % 100000000:08d}",
        "external_id": f"forensics-{uuid.uuid4().hex[:10]}",
        "guest_profile": {},
        # **Without this the measurement is of the harness, not the build.**
        #
        # `window_open` is false when `last_inbound_at` is null, so `send_message`
        # comes back MESSAGE_WINDOW_CLOSED — and the agent's documented answer to
        # a refused action is to hand off. The first forensics run scored 3.x at a
        # 100% handoff rate on exactly that path, which is not the failure being
        # investigated: a real guest turn is triggered *by* an inbound message and
        # the pipeline stamps this column before the agent ever runs.
        "last_inbound_at": datetime.now(timezone.utc).isoformat(),
    }
    created = db.table("conversations").insert(row).execute().data[0]
    CONVERSATIONS.append(created["id"])
    return created


async def capture_static(message: str, label: str, priced: bool) -> dict:
    """The half of the surface that does not vary between runs."""
    conversation = make_conversation()
    session = await session_for(0, priced)
    ctx = await ai_context.load_context(
        property_id=PROPERTY, partner_id=PARTNER, agent_type="guest",
        conversation_id=conversation["id"], guest_name="Forensics Guest",
        channel="whatsapp", actor="guest", session=session,
    )
    system = prompts.build_system_prompt(ctx)
    exposed = registry.describe_for_provider(ctx, task="guest_reply")

    def described(name: str) -> dict:
        for spec in exposed:
            fn = spec.get("function", spec)
            if fn.get("name") == name:
                return fn
        return {}

    handoff_lines = [
        line.strip() for line in system.splitlines()
        if "handoff" in line.lower() or "hand off" in line.lower()
        or "hand this" in line.lower() or "human" in line.lower()
    ]
    negotiate_lines = [
        line.strip() for line in system.splitlines()
        if "negotiat" in line.lower() or "discount" in line.lower()
    ]
    order = [spec.get("function", spec).get("name") for spec in exposed]
    return {
        "label": label,
        "session_state": "priced" if priced else "fresh",
        "session": session,
        "model": model_router.model_for_tier("conversation"),
        "autonomy": ctx.principal.autonomy_level,
        "system_prompt_chars": len(system),
        "system_prompt_handoff_lines": handoff_lines,
        "system_prompt_negotiation_lines": negotiate_lines,
        "tools_exposed": order,
        "tool_count": len(order),
        "handoff_position": order.index("request_human_handoff") if "request_human_handoff" in order else None,
        "negotiate_position": order.index("negotiate_rate") if "negotiate_rate" in order else None,
        "handoff_description": described("request_human_handoff").get("description"),
        "handoff_schema": described("request_human_handoff").get("parameters"),
        "negotiate_description": described("negotiate_rate").get("description"),
        "negotiate_schema": described("negotiate_rate").get("parameters"),
        "detected": [
            {"intent": r.intent, "tool": r.tool, "must_execute": r.must_execute,
             "detail": r.detail}
            for r in required_actions.detect(message, ctx.session)
        ],
    }


async def one_run(message: str, index: int, priced: bool) -> dict:
    conversation = make_conversation()
    started = time.monotonic()
    try:
        ctx = await ai_context.load_context(
            property_id=PROPERTY, partner_id=PARTNER, agent_type="guest",
            conversation_id=conversation["id"], guest_name="Forensics Guest",
            channel="whatsapp", actor="guest",
            session=await session_for(index, priced),
        )
        result = await runtime.run_turn(
            ctx, message, history=[], trigger="manual",
            summary=f"forensics:{index}",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    calls = [
        {"tool": o.tool, "status": o.status, "input": o.input,
         "refusal": o.refusal_reason}
        for o in result.actions
    ]
    handoff_call = next(
        (c for c in calls if c["tool"] == "request_human_handoff"), None
    )
    requirements = result.requirements or {}
    return {
        "index": index,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "status": result.status,
        "handoff": result.handoff,
        "handoff_reason": (handoff_call or {}).get("input", {}).get("reason"),
        "handoff_note": (handoff_call or {}).get("input", {}).get("note"),
        "calls": calls,
        "tools": [c["tool"] for c in calls],
        "intents": requirements.get("intents") or [],
        "required": requirements.get("required") or [],
        "satisfied": requirements.get("satisfied") or [],
        "forced": requirements.get("forced") or [],
        "unmet": requirements.get("unmet") or [],
        "reply": (result.reply or "")[:300],
    }


def summarise(rows: list[dict]) -> dict:
    real = [r for r in rows if "error" not in r]
    refused = Counter(
        c["tool"] for r in real for c in r.get("calls", []) if c["status"] == "refused"
    )
    handoffs = [r for r in real if r["handoff"]]
    reasons = Counter(r["handoff_reason"] for r in handoffs if r["handoff_reason"])
    negotiated = [r for r in real if "negotiate_rate" in r["tools"]]
    first_tool = Counter(r["tools"][0] for r in real if r["tools"])
    return {
        "runs": len(rows),
        "errors": len(rows) - len(real),
        "handoff_rate": len(handoffs) / len(real) if real else 0.0,
        "handoff_reasons": dict(reasons),
        "negotiated_rate": len(negotiated) / len(real) if real else 0.0,
        "first_tool_called": dict(first_tool),
        "no_tool_at_all": sum(1 for r in real if not r["tools"]),
        # A refusal is the thing a handoff most often follows, so it is counted
        # beside the handoff rather than left in the per-run detail.
        "refused_tools": dict(refused),
        "handoff_after_refusal": sum(
            1 for r in real if r["handoff"]
            and any(c["status"] == "refused" for c in r.get("calls", []))
        ),
    }


async def family(message: str, runs: int, v3: bool, label: str, priced: bool) -> dict:
    settings.hotel_models_v3 = v3
    static = await capture_static(message, label, priced)
    print(f"\n{'=' * 78}\n{label}  —  {static['model']}\n{'=' * 78}")
    print(f"  autonomy              {static['autonomy']}")
    print(f"  session state         {static['session_state']} "
          f"(rate_plan_id={'yes' if static['session'].get('rate_plan_id') else 'no'})")
    print(f"  system prompt         {static['system_prompt_chars']} chars")
    print(f"  tools exposed         {static['tool_count']}")
    print(f"  handoff at position   {static['handoff_position']}")
    print(f"  negotiate at position {static['negotiate_position']}")
    print(f"  detected requirement  {[d['intent'] + '->' + d['tool'] for d in static['detected']]}")
    print(f"  handoff description   {static['handoff_description']}")
    print(f"  handoff reason enum   "
          f"{(static['handoff_schema'] or {}).get('properties', {}).get('reason', {})}")

    rows = []
    for index in range(runs):
        row = await one_run(message, index, priced)
        rows.append(row)
        mark = "HANDOFF" if row.get("handoff") else ("ok" if "error" not in row else "ERR")
        print(f"    [{index + 1:>3}/{runs}] {mark:<8} "
              f"tools={','.join(row.get('tools') or []) or '-':<40} "
              f"reason={row.get('handoff_reason') or '-'}")
    return {"static": static, "rows": rows, "summary": summarise(rows)}


def cleanup() -> None:
    for conversation_id in CONVERSATIONS:
        try:
            runs = (db.table("agent_runs").select("id")
                    .eq("conversation_id", conversation_id).execute().data or [])
            for run in runs:
                db.table("agent_runs").delete().eq("id", run["id"]).execute()
            db.table("messages").delete().eq("conversation_id", conversation_id).execute()
            db.table("conversations").delete().eq("id", conversation_id).execute()
        except Exception:  # noqa: BLE001
            pass


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--out", default="/tmp/handoff_forensics.json")
    parser.add_argument("--only", choices=["v3", "stable"], default=None)
    parser.add_argument("--fresh", action="store_true",
                        help="no rate plan on the session — the normal first turn")
    args = parser.parse_args()

    print(f"message: {args.message!r}")
    was = settings.hotel_models_v3
    result = {}
    try:
        priced = not args.fresh
        if args.only != "v3":
            result["stable"] = await family(args.message, args.runs, False,
                                            "Gemini 2.5", priced)
        if args.only != "stable":
            result["v3"] = await family(args.message, args.runs, True,
                                        "Gemini 3.x", priced)
    finally:
        settings.hotel_models_v3 = was
        cleanup()

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    for key, block in result.items():
        s = block["summary"]
        print(f"\n{block['static']['label']} ({block['static']['model']})")
        print(f"  handoff rate      {s['handoff_rate'] * 100:.1f}%")
        print(f"  negotiated        {s['negotiated_rate'] * 100:.1f}%")
        print(f"  handoff reasons   {s['handoff_reasons']}")
        print(f"  first tool        {s['first_tool_called']}")
        print(f"  no tool at all    {s['no_tool_at_all']}")
        print(f"  refused tools     {s['refused_tools']}")
        print(f"  handoff⟵refusal   {s['handoff_after_refusal']}")

    pathlib.Path(args.out).write_text(json.dumps(result, indent=2, default=str))
    print(f"\nfull capture written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
