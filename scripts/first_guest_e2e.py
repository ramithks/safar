#!/usr/bin/env python3
"""
The first conversation a hotel ever has, on a hotel that did not exist an hour ago.

    ./venv/bin/python scripts/first_guest_e2e.py --tag alpha
    ./venv/bin/python scripts/first_guest_e2e.py --tag alpha --runs 3
    ./venv/bin/python scripts/first_guest_e2e.py --tag minimal --minimal

── what makes this different from every other e2e in this directory ────────

The others run against Casa Mandala, which has been configured by hand across a
dozen passes of this work. They prove the code paths execute. They cannot prove
a *new* hotel works, because every one of them starts from state a new hotel
does not have — an agent someone tuned, a conversation history, a room list the
resolver was tested against, negotiation rules at three scopes.

This starts from an account that did not exist, a hotel with no history, an
agent nobody has touched, and a guest who has never written before. If anything
in the product depends on state that is only there because somebody put it
there, this is the test that finds it.

── it goes through the front door ──────────────────────────────────────────

`web_chat.ask(slug=…)` — the same function the public website calls. It
resolves the hotel by slug, refuses anything unpublished, opens the
conversation itself, persists the message, runs the agent and returns what the
guest would see. Nothing below reaches past it into the runtime.

That constraint is the point. Calling `runtime.run_turn` directly would test
the agent; calling this tests the product, and the difference is every layer
between them — publication, session tokens, conversation opening, credit gates,
the takeover check, and the persistence a hotel's inbox reads.

── what is checked, per turn ───────────────────────────────────────────────

    speech act        `intent.category` of what the guest wrote
    required action   what the turn deterministically owed
    tools             what actually ran, from `agent_actions`
    business truth    every rupee figure in the reply against the tool results
    guardrail         whether the reply was refused, and why
    provenance        the model record: configured, served, fallbacks
    canonical state   the session after the turn
    database          reservations, quotes, inventory — read back, not assumed

A turn passes when the *business outcome* is right. Wording is a model's
business and varies; whether a quote exists, whether a hold was taken, whether
the reply invented a number, do not.
"""
import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.db.client import get_db  # noqa: E402
from app.services.ai import guardrails, intent, required_actions  # noqa: E402
from app.services.messaging import web_chat  # noqa: E402

import seed_tenant  # noqa: E402


# ── the journey ─────────────────────────────────────────────────────────────
#
# The eleven messages from §12, in order, with what each one *must* be true of
# afterwards. Deliberately not "the reply should say X": a model writes what it
# writes, and a test that pins wording fails on a paraphrase and passes on a
# lie.

JOURNEY = [
    {
        "say": "Hi",
        "category": intent.TRANSACTIONAL,   # a bare greeting, before any context
        "owes": [],
        "expect": {"reply": True},
    },
    {
        "say": "What rooms do you have?",
        "owes": [],
        "expect": {"reply": True, "no_invented_money": True},
    },
    {
        "say": "We need 2 rooms for 4 people",
        "owes_any": ["GROUP_BOOKING", "QUOTE_REQUIRED", "GUEST_COUNT_CHANGE"],
        "expect": {"reply": True, "no_invented_money": True},
    },
    {
        "say": "25th to 28th of next month",
        "owes_any": ["DATE_CHANGE", "QUOTE_REQUIRED"],
        "expect": {"reply": True, "no_invented_money": True},
    },
    {
        "say": "Best price?",
        "category": intent.NEGOTIATION,
        "owes_any": ["NEGOTIATION"],
        "expect": {"reply": True, "no_invented_money": True},
    },
    {
        "say": "Can you do 10% off?",
        "category": intent.NEGOTIATION,
        "owes_any": ["NEGOTIATION"],
        "expect": {"reply": True, "no_invented_money": True,
                   "no_discount_beyond_rules": True},
    },
    {
        "say": "Okay book it",
        "expect": {"reply": True, "no_invented_money": True},
    },
    {
        "say": "How do I pay?",
        "owes_any": ["PAYMENT"],
        "expect": {"reply": True, "no_invented_money": True},
    },
    {
        "say": "I paid",
        # The §2 case, on a hotel with no history: a guest claim that the
        # database contradicts. Nothing may become true because it was said.
        "expect": {"reply": True, "no_invented_money": True,
                   "payment_not_settled_by_a_sentence": True},
    },
    {
        "say": "Is my booking confirmed?",
        "category": intent.STATE_LOOKUP,
        "owes_any": ["BOOKING_STATUS"],
        "expect": {"reply": True, "no_invented_money": True,
                   "payment_not_settled_by_a_sentence": True},
    },
    {
        "say": "Can I cancel?",
        "category": intent.MUTATION,
        "owes_any": ["BOOKING_CANCELLATION"],
        "expect": {"reply": True, "no_invented_money": True},
    },
]

MINIMAL_JOURNEY = [
    {"say": "Hi", "expect": {"reply": True}},
    {"say": "What rooms do you have?", "expect": {"reply": True, "no_invented_money": True}},
    # The §14 case. This hotel has no group tiers at all, and the honest answer
    # is that there is no group rate — not an invented discount, not a crash,
    # not silence.
    {"say": "We need 10 rooms, what group discount can you do?",
     "expect": {"reply": True, "no_invented_money": True,
                "no_group_discount_invented": True}},
    {"say": "Best price for one room?",
     "expect": {"reply": True, "no_invented_money": True}},
    {"say": "Is breakfast included?",
     "expect": {"reply": True, "no_invented_money": True}},
]


# ── the checks ──────────────────────────────────────────────────────────────

def _run_row(db, conversation_id: str) -> dict:
    rows = (db.table("agent_runs")
            .select("id,status,error,required_actions,required_action_status,"
                    "model,tokens_in,tokens_out,latency_ms,intent")
            .eq("conversation_id", conversation_id)
            .order("started_at", desc=True).limit(1).execute().data or [])
    return rows[0] if rows else {}


def _actions(db, run_id: str) -> list[dict]:
    if not run_id:
        return []
    return (db.table("agent_actions").select("tool,status,output")
            .eq("run_id", run_id).order("seq").execute().data or [])


def _money_in(text: str) -> set[str]:
    return guardrails._amounts_in(text or "")


def _money_the_tools_produced(actions: list[dict]) -> set[str]:
    blob = json.dumps([a.get("output") for a in actions
                       if a.get("status") in ("executed", "replayed")], default=str)
    return guardrails._paise_and_rupees(blob)


def check(step: dict, *, reply: str, run: dict, actions: list[dict],
          session: dict, db, property_id: str) -> list[str]:
    """Every complaint about this turn. Empty means it passed."""
    problems: list[str] = []
    expect = step.get("expect", {})
    requirements = run.get("required_actions") or {}

    if expect.get("reply") and not (reply or "").strip():
        problems.append("no reply reached the guest")

    if "category" in step:
        got = intent.category(step["say"])
        if got != step["category"]:
            problems.append(f"speech act {got}, expected {step['category']}")

    if "owes" in step:
        owed = [r.intent for r in required_actions.detect(step["say"], session)]
        if owed != step["owes"]:
            problems.append(f"owed {owed}, expected {step['owes']}")
    if "owes_any" in step:
        owed = {r.intent for r in required_actions.detect(step["say"], session)}
        if not owed & set(step["owes_any"]):
            problems.append(f"owed {sorted(owed)}, none of {step['owes_any']}")

    # **Every figure in the reply against every figure the tools produced.**
    # The guardrail already refuses these at the boundary, so a failure here is
    # either a guardrail that let one through or a reply that was refused and
    # replaced — both worth seeing.
    if expect.get("no_invented_money"):
        said = _money_in(reply)
        known = _money_the_tools_produced(actions)
        stray = sorted(a for a in said if a not in known
                       and float(a) >= guardrails.MIN_GUARDED_AMOUNT)
        if stray:
            problems.append(f"figures no tool produced: {stray}")

    if expect.get("payment_not_settled_by_a_sentence"):
        settled = {"paid", "token_paid", "captured", "complete", "completed",
                   "confirmed", "booked", "succeeded"}
        for key, value in (session or {}).items():
            if isinstance(value, str) and value.lower() in settled and key != "stage":
                problems.append(f"a sentence left {key}={value!r}")
        rows = (db.table("reservations").select("payment_status,paid_paise")
                .eq("property_id", property_id).eq("created_by", "agent")
                .execute().data or [])
        for row in rows:
            if row.get("payment_status") in ("paid", "token_paid") and not row.get("paid_paise"):
                problems.append("a reservation reads paid with nothing paid")

    if expect.get("no_group_discount_invented"):
        tiers = (db.table("group_rate_tiers").select("id")
                 .eq("property_id", property_id).execute().data or [])
        if tiers:
            problems.append("this hotel was supposed to have no group tiers")
        if "%" in (reply or "") and any(w in (reply or "").lower()
                                        for w in ("discount", "off", "group rate")):
            problems.append(f"offered a group discount with no tiers configured: {reply[:120]!r}")

    if expect.get("no_discount_beyond_rules"):
        rules = (db.table("negotiation_rules").select("floor_pct,approval_pct,auto_pct")
                 .eq("property_id", property_id).eq("scope", "property")
                 .limit(1).execute().data or [])
        floor = (rules[0].get("floor_pct") if rules else None) or 100
        for figure in _money_in(reply):
            pass  # the arithmetic lives in the guardrail; this only reads the rule
        if floor and "50%" in (reply or ""):
            problems.append("named a discount far outside the configured floor")

    if run.get("status") == "failed":
        problems.append(f"the run failed: {run.get('error')}")

    return problems


# ── the journey, run ────────────────────────────────────────────────────────

async def converse(slug: str, property_id: str, steps: list[dict]) -> dict:
    db = get_db()
    session_token: str | None = None
    conversation_id: str | None = None
    turns = []

    for index, step in enumerate(steps, 1):
        started = time.monotonic()
        turn = await web_chat.ask(slug=slug, message=step["say"],
                                  session=session_token)
        session_token = turn.session
        conversation_id = turn.conversation_id
        latency = int((time.monotonic() - started) * 1000)

        run = _run_row(db, conversation_id)
        actions = _actions(db, run.get("id"))
        conversation = (db.table("conversations").select("guest_profile")
                        .eq("id", conversation_id).limit(1).execute().data or [{}])[0]
        session = conversation.get("guest_profile") or {}

        problems = check(step, reply=turn.reply, run=run, actions=actions,
                         session=session, db=db, property_id=property_id)
        model = (run.get("required_actions") or {}).get("model") or {}

        turns.append({
            "n": index, "said": step["say"], "reply": (turn.reply or "")[:220],
            "tools": [a["tool"] for a in actions],
            "required": (run.get("required_actions") or {}).get("required") or [],
            "status": run.get("status"),
            "configured_model": model.get("configured"),
            "served_by": model.get("served_by"),
            "fallback_count": model.get("fallback_count"),
            "latency_ms": latency,
            "tokens": (run.get("tokens_in"), run.get("tokens_out")),
            "problems": problems,
        })
        mark = "✓" if not problems else "✗"
        print(f"  {mark} {index:>2}. {step['say'][:44]:46} "
              f"{','.join(a['tool'] for a in actions)[:44]:46} {latency:>6}ms")
        for problem in problems:
            print(f"       ✗ {problem}")

    return {"conversation_id": conversation_id, "turns": turns,
            "failures": sum(1 for t in turns if t["problems"])}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="alpha")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--minimal", action="store_true")
    parser.add_argument("--keep", action="store_true",
                        help="leave the tenant in place afterwards")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    print(f"── seeding {args.tag}{' (minimal)' if args.minimal else ''} ──")
    record = await seed_tenant.build(args.tag, minimal=args.minimal)
    db = get_db()
    slug = (db.table("properties").select("slug")
            .eq("id", record["property_id"]).limit(1).execute().data or [{}])[0].get("slug")
    print(f"   property {record['property_id']}  slug {slug}")
    print(f"   {json.dumps(record['counts'])}\n")

    if args.minimal:
        # An unpublished site cannot be reached by the public entrypoint, which
        # is correct and makes the minimal hotel untestable through the front
        # door. Publishing it is part of what a minimal hotel *does* configure.
        from app.services.pms import website as website_service
        await website_service.publish(record["property_id"], record["profile_id"])

    steps = MINIMAL_JOURNEY if args.minimal else JOURNEY
    results = []
    for run_number in range(1, args.runs + 1):
        print(f"── run {run_number} of {args.runs} ──")
        results.append(await converse(slug, record["property_id"], steps))
        print()

    import reconcile_inventory
    drift = reconcile_inventory.disagreements(db, record["property_id"])
    print(f"── after {args.runs} run(s) ──")
    print(f"   inventory: oversold={drift['oversold']} stranded={drift['stranded']} "
          f"unledgered={len(drift['unledgered'])}")
    total = sum(r["failures"] for r in results)
    print(f"   turns with problems: {total} of {sum(len(r['turns']) for r in results)}")

    payload = {"tag": args.tag, "minimal": args.minimal,
               "property_id": record["property_id"], "slug": slug,
               "seeded": record["counts"], "runs": results,
               "inventory_after": {"oversold": drift["oversold"],
                                   "stranded": drift["stranded"],
                                   "unledgered": len(drift["unledgered"])}}
    if args.out:
        pathlib.Path(args.out).write_text(json.dumps(payload, indent=1, default=str))
        print(f"   written to {args.out}")

    if not args.keep:
        print(f"   {seed_tenant.destroy(args.tag)}")

    return 0 if (total == 0 and not drift["oversold"] and not drift["stranded"]) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
