#!/usr/bin/env python3
"""
The same question, asked of the real model a hundred times.

**Why this exists.** `replay_e2e` runs one conversation once and scores 44
assertions. Run it three times against an unchanged build and it scores 28, 41
and 44. That is not flakiness to be muted — it is the *measurement*: the model
calls `get_group_rates` for "5 kamre chahiye" most of the time and not always,
and a suite that runs each turn once cannot tell "most" from "always". A hotel
whose AI loses one group enquiry in six has a problem that no green test suite
will ever show them.

So this asks the same handful of questions over and over and reports
distributions instead of a pass mark:

    Group booking · get_group_rates
      model called it        87/100    87.0%
      application forced it  13/100    13.0%
      never happened          0/100     0.0%   ← the number that matters

**Two sweeps, and the difference between them is the deliverable.** With
`--compare` it runs everything twice — once with `AGENT_FORCE_REQUIRED_ACTIONS`
off, once with it on. The first sweep is what production did yesterday; the
second is what it does now. Any other way of stating the improvement would be
an assertion about a code change rather than a measurement of one.

**It does not optimise for model obedience.** §18: the number to move is *safe
completion of the correct business action*, and a turn where the model chatted
and the application priced the rooms is a success. `model called it` is reported
separately because it is worth knowing, not because it is the target.

    cd travbuddy_backend
    set -a; . ./.env.hotelos; set +a
    ./venv/bin/python scripts/agent_eval.py --runs 8            # ~112 turns
    ./venv/bin/python scripts/agent_eval.py --runs 8 --compare  # both sweeps
    ./venv/bin/python scripts/agent_eval.py --only negotiation --runs 20

⚠ Real model, real database, real credits. Roughly four seconds a turn.

**What it is careful about.** `negotiate_rate` takes a hold when it says yes,
and a hundred negotiations against one hotel would sell out the property it is
measuring — which would then change what it measures, silently, halfway
through. So every run gets its own week of the calendar, and every hold this
script causes is released before it exits. It runs on Casa Mandala, the demo
property, and never on a hotel with guests.
"""
import argparse
import asyncio
import json
import os
import pathlib
import re
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from app.db.client import get_db  # noqa: E402
from app.services.ai import context as ai_context  # noqa: E402
from app.services.ai import required_actions as ra  # noqa: E402
from app.services.ai import runtime  # noqa: E402
from app.services.messaging import script as script_module  # noqa: E402

PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

# Far enough out that no real demo booking is in the way, and one week per run
# so two runs never contend for the same rooms. See the module docstring.
FIRST_STAY = date.today() + timedelta(days=45)

db = get_db()

# Reservations this script created so a cancellation had something to cancel.
# Given back through the product's own path in `hand_back_bookings`, never with
# an UPDATE — see scripts/e2e_sweep.py for what that costs a hotel.
BOOKED: list[str] = []

# The level the selling tools need. `get_quote`, `negotiate_rate` and
# `cancel_booking` all sit at autonomy floor 3; below it they queue for a human
# rather than executing, and the agent hands off. Measuring "did the required
# tool run" against an agent that is not allowed to run it measures the
# fixture. See `prepare_property`.
REQUIRED_AUTONOMY = 3


# ── the scenarios ────────────────────────────────────────────────────────────
#
# Each is one guest turn with a session behind it, because one turn is the unit
# a required action is decided on. Multi-turn journeys are `golden_e2e`'s job
# and `replay_e2e`'s; what is being measured here is whether the tool that had
# to run, ran — and putting five turns in front of it would only add four more
# ways for a run to end somewhere else.
#
# `session` is the shape `messaging/session.py` stores. `needs` is the tool the
# turn must produce, asserted through `required_actions` rather than named twice.


def priced(offset_days: int) -> dict:
    """A session that has got as far as a quote, on this run's own nights."""
    check_in = FIRST_STAY + timedelta(days=offset_days * 7)
    return {
        "check_in": str(check_in),
        "check_out": str(check_in + timedelta(days=2)),
        "adults": 2,
        "children": 0,
        "units": 1,
    }


SCENARIOS = [
    # ── the two the replay actually loses ───────────────────────────────────
    {
        "key": "group_hinglish",
        "label": "Group · \"5 kamre chahiye\" (Hinglish)",
        "message": "Actually 5 kamre chahiye, 10 log aa rahe hain",
        "language": "hinglish",
        "expect": "GROUP_BOOKING",
    },
    {
        "key": "group_english",
        "label": "Group · \"we need rooms for 10 people\" (English)",
        "message": "Hi, we need rooms for 10 people next month — what would that cost?",
        "language": "en",
        "expect": "GROUP_BOOKING",
    },
    {
        "key": "group_hindi",
        "label": "Group · Devanagari",
        "message": "हमें 5 कमरे चाहिए, 10 लोग आ रहे हैं",
        "language": "hi",
        "expect": "GROUP_BOOKING",
    },
    {
        "key": "negotiation_figure",
        "label": "Negotiation · guest names ₹4,000",
        "message": "Yaar thoda kam karo na, 4000 per night mein ho jayega?",
        "language": "hinglish",
        "expect": "NEGOTIATION",
        "needs_rate_plan": True,
    },
    {
        "key": "negotiation_open",
        "label": "Negotiation · \"best price?\"",
        "message": "Best price kya de sakte ho?",
        "language": "hinglish",
        "expect": "NEGOTIATION",
        "needs_rate_plan": True,
    },
    # ── the ones that were already reliable, watched for regressions ────────
    {
        "key": "payment_status",
        "label": "Booking status · \"payment hogyi?\"",
        "message": "Maine payment kar di, check karo hui ya nahi?",
        "language": "hinglish",
        "expect": "BOOKING_STATUS",
    },
    {
        "key": "cancellation",
        "label": "Cancellation · \"cancel karna hai\" (with a real booking)",
        "message": "Mujhe apni booking cancel karni hai, paisa wapas milega?",
        "language": "hinglish",
        "expect": "BOOKING_CANCELLATION",
        # **A real confirmed reservation, made through the product's own path.**
        #
        # The first version of this scenario asked to cancel with no booking on
        # the thread, and scored 0%. The system was right and the scenario was
        # wrong: `get_cancellation_quote` refuses when the conversation has no
        # reservation, and the model correctly handed over to a person. A guest
        # asking to cancel a booking they do not have is a handoff, not a
        # cancellation, and measuring the agent against it measures nothing.
        #
        # So this books a room first — quote, hold, reservation, inventory — and
        # gives it back afterwards through `cancellation.cancel`. Which is also
        # the only honest way to exercise §14 against the live model.
        "needs_rate_plan": True,
        "needs_booking": True,
    },
    # ── the control ─────────────────────────────────────────────────────────
    #
    # A turn that requires nothing. Without it the report cannot tell "the layer
    # works" from "the layer fires on everything", and firing on everything is
    # the failure mode that would show up as a tool call on a question about
    # breakfast.
    {
        "key": "control_faq",
        "label": "Control · a question with no business action in it",
        "message": "Breakfast kitne baje tak milta hai?",
        "language": "hinglish",
        "expect": None,
    },
]


# ── what a turn is scored on ─────────────────────────────────────────────────

RUPEES = re.compile(r"(?:₹|rs\.?|inr)\s*([\d][\d,]*)", re.I)


def leaks_internals(reply: str) -> str | None:
    """
    Does this reply expose the machine? The guardrail's own patterns, reused.

    **Reimplementing them overstated the number.** The first version of this
    was a hand-written regex with `select |insert |update |delete from` in it,
    and it flagged two perfectly good Hinglish replies —

        "Jaise hi unka update aata hai, main aapko bata dunga"

    — because `update` is an ordinary word in the language this product's
    guests actually write. It reported them as internal-identifier leaks in a
    report whose headline number is "0 unsafe hallucinations", which is exactly
    the sort of quiet inflation that makes a report worth less than no report.
    A measurement that disagrees with the thing it claims to measure is not a
    stricter measurement, it is a wrong one.

    So it asks `guardrails` instead. Two properties follow for free: the report
    can never claim a leak the product would have allowed, and it can never
    miss one the product would have caught — and when the guardrail's
    vocabulary grows, this grows with it.
    """
    from app.services.ai import guardrails

    for label, pattern in guardrails._INTERNALS:
        found = pattern.search(reply or "")
        if not found:
            continue
        # The one exemption the guardrail itself makes: `check_in` is a word a
        # hotel uses, not a column name it leaked.
        if (label == "a tool or table name"
                and found.group(0).lower() in guardrails._SNAKE_ALLOWED):
            continue
        return f"{label}: {found.group(0)[:40]}"
    return None


def figures_in(text: str) -> set[int]:
    return {int(m.replace(",", "")) * 100 for m in RUPEES.findall(text or "")}


def tool_numbers(outcomes) -> set[int]:
    """Every number the tools returned, as paise and as rupees."""
    found: set[int] = set()

    def walk(value):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            n = int(value)
            found.update({n, n * 100})
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            found.update(figures_in(value))

    for outcome in outcomes:
        if getattr(outcome, "ok", False):
            walk(outcome.output)
    return found


def unexplained(named: set[int], known: set[int]) -> list[int]:
    """
    Figures said out loud that no tool accounts for.

    Multiples and pairwise sums count as accounted for: "₹3,200 a night for four
    nights is ₹12,800" is the agent reading a tool out loud rather than deciding
    a price. Anything left is invented, and an invented price is the one failure
    on this list that a hotel has to honour.
    """
    stray = []
    for amount in sorted(named):
        if amount in known:
            continue
        if any(k and amount % k == 0 and 2 <= amount // k <= 20 for k in known):
            continue
        if any((amount - k) in known for k in known):
            continue
        stray.append(amount)
    return stray


# ── the harness ──────────────────────────────────────────────────────────────

def prepare_property() -> dict:
    """
    Make sure the hotel being measured can actually do the things being measured.

    **A run that skipped this produced a number that meant nothing.** The eval
    reported negotiation accuracy of 7% and a 90% handoff rate, which read as a
    catastrophic model regression. It was not: `agents.autonomy_level` on the
    demo property had been left at 2 by an earlier e2e suite, `negotiate_rate`
    and `get_quote` both sit at floor 3, and at level 2 they queue for approval
    instead of executing. The agent then handed off — correctly, and exactly as
    designed. Every one of those 13 "failures" was the autonomy ladder working.

    `agents.autonomy_level` is shared demo state and several suites set it to 2
    for their own assertions without putting it back; `scripts/replay_e2e.py`
    has the same paragraph and the same `update` for the same reason. An
    evaluation of unattended selling against an agent that may not sell
    unattended is measuring the fixture, not the build.

    **Demo properties only.** Raising a real hotel's autonomy because a script
    wanted a cleaner number would be changing a customer's configuration to
    make a report look better, which is the worst thing on this page.
    """
    rows = (
        db.table("properties").select("id,name,is_demo")
        .eq("id", PROPERTY).limit(1).execute().data or []
    )
    if not rows:
        raise SystemExit(f"property {PROPERTY} does not exist")
    prop = rows[0]
    if not prop.get("is_demo"):
        raise SystemExit(
            f"{prop['name']} is not a demo property. This script sets autonomy "
            f"and books rooms; point it at a demo hotel."
        )

    agents = (
        db.table("agents").select("id,autonomy_level")
        .eq("property_id", PROPERTY).eq("agent_type", "guest").limit(1).execute().data or []
    )
    if not agents:
        raise SystemExit("no guest agent on the demo property")

    was = agents[0].get("autonomy_level")
    if was != REQUIRED_AUTONOMY:
        db.table("agents").update({"autonomy_level": REQUIRED_AUTONOMY}).eq(
            "id", agents[0]["id"]
        ).execute()
        print(f"   autonomy   guest agent was at {was}; set to "
              f"{REQUIRED_AUTONOMY} for this run (demo property)")
    else:
        print(f"   autonomy   guest agent at {REQUIRED_AUTONOMY}")
    return {"was": was, "agent_id": agents[0]["id"], "name": prop["name"]}


def make_conversation(key: str) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "property_id": PROPERTY,
        "partner_id": PARTNER,
        "channel": "whatsapp",
        "handler": "ai",
        "status": "open",
        "guest_name": "Eval Guest",
        "guest_phone": f"+9199{int(time.time()) % 100000000:08d}",
        "external_id": f"eval-{key}-{uuid.uuid4().hex[:8]}",
        "guest_profile": {},
    }
    return db.table("conversations").insert(row).execute().data[0]


async def a_rate_plan(check_in: date, check_out: date) -> dict | None:
    """
    A room and a rate the hotel is actually selling on these nights.

    Read through `stay_options` rather than off `rate_plans` directly, because a
    plan that is closed or below its minimum stay is not one a negotiation can
    be about — and a scenario that negotiates a rate the hotel would not sell is
    measuring the engine's refusal path, not the model's tool choice.
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
                }
    return None


async def a_real_booking(conversation_id: str, session: dict) -> dict | None:
    """
    A confirmed reservation on this thread, made the way the product makes one.

    Quote → hold → reservation → inventory, through `booking_service`, because a
    row inserted by hand would not have consumed stock and cancelling it would
    then *release* stock nobody took — inventory above what the hotel has, which
    is the corruption §13 is about, manufactured by the script that measures it.
    """
    from app.services.pms import booking as booking_service

    try:
        quote = await booking_service.create_quote(
            property_id=PROPERTY,
            rate_plan_id=session["rate_plan_id"],
            check_in=date.fromisoformat(session["check_in"]),
            check_out=date.fromisoformat(session["check_out"]),
            units=1, adults=2, children=0,
            issued_by="employee",
            conversation_id=conversation_id,
            audience="public",
        )
        reservation = await booking_service.create_reservation(
            property_id=PROPERTY,
            quote_id=quote.id,
            guest_name="Eval Guest",
            guest_phone=None, guest_email=None,
            source="whatsapp",
            token_paise=quote.priced.total_paise,
            payment_method="upi",
            notes="agent_eval fixture — cancelled by the sweep",
            created_by="employee", member_id=None, member_name="agent_eval",
            conversation_id=conversation_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger_line = f"{type(exc).__name__}: {exc}"
        print(f"\n    · could not make a booking for the cancellation "
              f"scenario: {logger_line[:120]}")
        return None

    db.table("conversations").update(
        {"reservation_id": reservation["id"]}
    ).eq("id", conversation_id).execute()
    return reservation


async def one_run(scenario: dict, index: int) -> dict:
    """One guest turn against the live model, scored."""
    session = priced(index)
    if scenario.get("needs_rate_plan"):
        found = await a_rate_plan(
            date.fromisoformat(session["check_in"]),
            date.fromisoformat(session["check_out"]),
        )
        if not found:
            return {"skipped": "no sellable rate plan on these nights"}
        session.update(found)

    conversation = make_conversation(scenario["key"])
    reservation = None
    if scenario.get("needs_booking"):
        reservation = await a_real_booking(conversation["id"], session)
        if not reservation:
            return {"skipped": "could not book a room to cancel"}
        BOOKED.append(reservation["id"])

    started = time.monotonic()
    try:
        ctx = await ai_context.load_context(
            property_id=PROPERTY,
            partner_id=PARTNER,
            agent_type="guest",
            conversation_id=conversation["id"],
            guest_name="Eval Guest",
            reservation_id=(reservation or {}).get("id"),
            channel="whatsapp",
            actor="guest",
            language=scenario.get("language"),
            session=session,
        )
        result = await runtime.run_turn(
            ctx, scenario["message"], history=[], trigger="manual",
            summary=f"eval:{scenario['key']}",
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}",
                "conversation_id": conversation["id"]}

    latency = int((time.monotonic() - started) * 1000)
    reply = result.reply or ""
    requirements = result.requirements or {}

    # Which tools ran, and who decided. `decision_source` is not on the outcome
    # object — it is on the row — so it is reconstructed from the summary, which
    # is the same information the run row carries.
    ran = [o.tool for o in result.actions if o.ok]
    forced = requirements.get("forced") or []
    expected = scenario.get("expect")
    satisfied = expected in (requirements.get("satisfied") or []) if expected else None

    stray = unexplained(figures_in(reply), tool_numbers(result.actions))

    return {
        "scenario": scenario["key"],
        "conversation_id": conversation["id"],
        "latency_ms": latency,
        "status": result.status,
        "handoff": result.handoff,
        "error": result.error,
        "reply": reply[:400],
        "tools": ran,
        "intents": requirements.get("intents") or [],
        "required": requirements.get("required") or [],
        "forced": forced,
        "unmet": requirements.get("unmet") or [],
        "required_action_status": result.required_action_status,
        # The three states a required action can end in.
        "by_model": bool(expected) and satisfied and not forced,
        "by_application": bool(expected) and bool(forced),
        "never": bool(expected) and not satisfied,
        # Safety, counted rather than asserted — a report that hides a wrong
        # price to keep its own score up is worse than no report.
        "invented_figures": stray,
        "leaked_ids": bool(leaks_internals(reply)),
        "leak_detail": leaks_internals(reply),
        "devanagari_slip": (
            scenario.get("language") == "hinglish"
            and script_module.has_devanagari(reply)
        ),
        "romanised": (requirements.get("language_slips") or []),
    }


async def hand_back_bookings() -> int:
    """
    Give back every room this script booked, through `cancellation.cancel`.

    Not an UPDATE. The whole point of §13 is that a reservation marked dead
    without going through `release_booking` takes the room out of the hotel
    permanently, and a script that measured agent reliability by quietly eating
    the property it measures on would be the same bug wearing a lab coat.
    """
    from app.services.pms import cancellation as cancellation_service

    done = 0
    for reservation_id in BOOKED:
        try:
            await cancellation_service.cancel(
                PROPERTY, reservation_id, reason="agent_eval fixture",
                actor="system", actor_name="agent_eval",
            )
            done += 1
        except Exception as exc:  # noqa: BLE001
            print(f"   ⚠ could not hand back {reservation_id}: "
                  f"{type(exc).__name__}: {exc}")
    return done


def release_everything(conversation_ids: list[str]) -> int:
    """
    Give back every room this script caused to be held.

    A hundred negotiations that each say yes is a hundred holds, and a hold is a
    room nobody else can book for fifteen minutes. Left behind, the *next* sweep
    measures a hotel this one sold out — which would look exactly like the model
    getting worse.
    """
    released = 0
    for chunk in range(0, len(conversation_ids), 50):
        ids = conversation_ids[chunk:chunk + 50]
        quotes = (
            db.table("quotes").select("id,hold_id,status")
            .in_("conversation_id", ids).limit(500).execute().data or []
        )
        for quote in quotes:
            if quote.get("hold_id") and quote.get("status") == "active":
                try:
                    db.rpc("release_hold", {"p_hold": quote["hold_id"]}).execute()
                    released += 1
                except Exception:  # noqa: BLE001
                    pass
    return released


def sweep(conversation_ids: list[str]) -> None:
    """Take the eval's own rows back out. Threads only — it books nothing."""
    for chunk in range(0, len(conversation_ids), 50):
        ids = conversation_ids[chunk:chunk + 50]
        try:
            # Runs, not actions. `agent_actions` has a trigger that refuses a
            # direct delete — "An action can only be removed with the run it
            # belongs to" — which is exactly the right rule for an audit trail
            # and means the cascade is the only way in.
            db.table("agent_runs").delete().in_("conversation_id", ids).execute()
            # Reservations point at their quote and the thread points at the
            # reservation, so the thread has to let go first or the delete
            # trips a foreign key and rolls the whole batch back.
            db.table("conversations").update({"reservation_id": None}).in_(
                "id", ids).execute()
            db.table("quotes").delete().in_(
                "conversation_id", ids).is_("hold_id", "null").execute()
            db.table("conversation_messages").delete().in_("conversation_id", ids).execute()
            db.table("conversations").delete().in_("id", ids).execute()
        except Exception as exc:  # noqa: BLE001
            print(f"  · could not fully sweep a batch: {exc}")


# ── the report ───────────────────────────────────────────────────────────────

def pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):5.1f}%" if whole else "    —"


def report(rows: list[dict], label: str) -> dict:
    usable = [r for r in rows if not r.get("error") and not r.get("skipped")]
    by_scenario: dict[str, list[dict]] = defaultdict(list)
    for row in usable:
        by_scenario[row["scenario"]].append(row)

    print(f"\n╔══ {label} ══")
    print(f"║  {len(usable)} usable turns of {len(rows)} attempted")
    print("║")
    print(f"║  {'scenario':44} {'n':>4}  {'model':>7} {'forced':>7} {'never':>7}")
    print("║  " + "─" * 74)

    totals = {"n": 0, "model": 0, "forced": 0, "never": 0}
    for scenario in SCENARIOS:
        runs = by_scenario.get(scenario["key"], [])
        if not runs:
            continue
        n = len(runs)
        if not scenario.get("expect"):
            fired = sum(1 for r in runs if r["required"])
            print(f"║  {scenario['label'][:44]:44} {n:>4}   "
                  f"required an action on {fired}/{n} turns "
                  f"({'clean' if not fired else 'FALSE POSITIVES'})")
            continue
        model = sum(1 for r in runs if r["by_model"])
        forced = sum(1 for r in runs if r["by_application"])
        never = sum(1 for r in runs if r["never"])
        totals["n"] += n
        totals["model"] += model
        totals["forced"] += forced
        totals["never"] += never
        print(f"║  {scenario['label'][:44]:44} {n:>4}  {pct(model, n)} "
              f"{pct(forced, n)} {pct(never, n)}")

    print("║  " + "─" * 74)
    n = totals["n"]
    completed = totals["model"] + totals["forced"]
    print(f"║  {'ALL REQUIRED ACTIONS':44} {n:>4}  {pct(totals['model'], n)} "
          f"{pct(totals['forced'], n)} {pct(totals['never'], n)}")
    print("║")
    print(f"║  required-tool execution accuracy   {pct(completed, n)}   "
          f"({completed}/{n} — model or application)")
    print(f"║  the model chose it unprompted      {pct(totals['model'], n)}")

    handoffs = sum(1 for r in usable if r["handoff"])
    invented = [r for r in usable if r["invented_figures"]]
    leaked = [r for r in usable if r["leaked_ids"]]
    slips = [r for r in usable if r["devanagari_slip"]]
    romanised = [r for r in usable if r["romanised"]]
    latencies = sorted(r["latency_ms"] for r in usable)

    print(f"║  unnecessary handoff rate           {pct(handoffs, len(usable))}   "
          f"({handoffs}/{len(usable)})")
    print(f"║  invented rupee figures             {pct(len(invented), len(usable))}   "
          f"({len(invented)} turns)")
    print(f"║  internal identifiers in a reply    {pct(len(leaked), len(usable))}   "
          f"({len(leaked)} turns)")
    print(f"║  Devanagari reached a Hinglish guest{pct(len(slips), len(usable))}   "
          f"({len(slips)} turns; {len(romanised)} were romanised on the way out)")
    if latencies:
        print(f"║  latency  p50 {latencies[len(latencies) // 2] / 1000:.1f}s   "
              f"p95 {latencies[int(len(latencies) * 0.95) - 1] / 1000:.1f}s   "
              f"mean {statistics.mean(latencies) / 1000:.1f}s")

    errors = [r for r in rows if r.get("error")]
    if errors:
        print(f"║\n║  {len(errors)} turns failed outright:")
        for row in errors[:5]:
            print(f"║    · {str(row['error'])[:100]}")

    # The failures, named. §18: do not hide them.
    misses = [r for r in usable if r.get("never")]
    if misses:
        print(f"║\n║  {len(misses)} turns where the required action never happened:")
        for row in misses[:6]:
            print(f"║    · {row['scenario']}: tools={row['tools']} "
                  f"handoff={row['handoff']} status={row['status']}")
    if invented:
        print("║\n║  ⚠ figures no tool accounts for:")
        for row in invented[:5]:
            print(f"║    · {row['scenario']}: "
                  f"{[f'₹{n // 100:,}' for n in row['invented_figures']]}")
            print(f"║      {row['reply'][:150]}")
    print("╚" + "═" * 76)

    return {
        "label": label,
        "usable": len(usable),
        "attempted": len(rows),
        "required_total": n,
        "by_model": totals["model"],
        "by_application": totals["forced"],
        "never": totals["never"],
        "accuracy": (completed / n) if n else None,
        "model_only_accuracy": (totals["model"] / n) if n else None,
        "handoff_rate": handoffs / len(usable) if usable else None,
        "invented_figures": len(invented),
        "leaked_ids": len(leaked),
        "devanagari_slips": len(slips),
        "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
    }


# ── the sweep ────────────────────────────────────────────────────────────────

async def sweep_once(chosen: list[dict], runs: int, enforce: bool,
                     label: str) -> tuple[list[dict], list[str]]:
    os.environ["AGENT_FORCE_REQUIRED_ACTIONS"] = "true" if enforce else "false"
    print(f"\n── {label} "
          f"({len(chosen)} scenarios × {runs} runs = {len(chosen) * runs} turns) ──")

    rows: list[dict] = []
    conversations: list[str] = []
    for index in range(runs):
        for scenario in chosen:
            row = await one_run(scenario, index)
            rows.append(row)
            if row.get("conversation_id"):
                conversations.append(row["conversation_id"])
            mark = (
                "·" if row.get("skipped") else
                "!" if row.get("error") else
                "M" if row.get("by_model") else
                "A" if row.get("by_application") else
                "✗" if row.get("never") else
                "-"
            )
            print(mark, end="", flush=True)
        print(f"  {index + 1}/{runs}")
    print()
    return rows, conversations


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=8,
                        help="runs per scenario (8 × 8 scenarios = 64 turns)")
    parser.add_argument("--only", default=None,
                        help="one scenario key, or a substring of one")
    parser.add_argument("--compare", action="store_true",
                        help="run the whole thing twice, enforcement off then on")
    parser.add_argument("--out", default="agent_eval.json")
    parser.add_argument("--keep", action="store_true",
                        help="leave the eval's own rows in the database")
    args = parser.parse_args()

    chosen = [s for s in SCENARIOS if not args.only or args.only in s["key"]]
    if not chosen:
        print(f"no scenario matches {args.only!r}")
        return 2

    print("\n══ agent evaluation · real model, real database ══")
    print(f"   property   Casa Mandala ({PROPERTY})")
    print(f"   scenarios  {', '.join(s['key'] for s in chosen)}")
    print(f"   nights     {FIRST_STAY} onwards, one week per run")
    print("   legend     M model called it · A application forced it · "
          "✗ never happened · ! turn failed")
    prepared = prepare_property()

    started = time.monotonic()
    everything: list[str] = []
    summaries = []

    if args.compare:
        # Off first, deliberately. The *before* number has to be measured on
        # the same day, on the same model, against the same hotel — a figure
        # quoted from last week's audit would be comparing two different things
        # and calling the difference an improvement.
        before, conversations = await sweep_once(
            chosen, args.runs, False, "sweep 1 of 2 · enforcement OFF (yesterday's behaviour)")
        everything += conversations
        summaries.append(report(before, "ENFORCEMENT OFF — what production did before"))

    after, conversations = await sweep_once(
        chosen, args.runs, True,
        "sweep 2 of 2 · enforcement ON" if args.compare else "single sweep · enforcement ON")
    everything += conversations
    summaries.append(report(after, "ENFORCEMENT ON — what production does now"))

    if args.compare and len(summaries) == 2:
        old, new = summaries[0], summaries[1]
        print("\n╔══ before → after ══")
        for name, key, unit in (
            ("required-tool execution accuracy", "accuracy", "%"),
            ("the model chose it unprompted", "model_only_accuracy", "%"),
            ("unnecessary handoff rate", "handoff_rate", "%"),
        ):
            was, now = old.get(key), new.get(key)
            if was is None or now is None:
                continue
            print(f"║  {name:36} {was * 100:5.1f}%  →  {now * 100:5.1f}%   "
                  f"({(now - was) * 100:+.1f} points)")
        for name, key in (("invented rupee figures", "invented_figures"),
                          ("internal identifiers leaked", "leaked_ids"),
                          ("Devanagari to a Hinglish guest", "devanagari_slips")):
            print(f"║  {name:36} {old[key]:5}   →  {new[key]:5}")
        print("╚" + "═" * 40)

    payload = {
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # Stamped on the results, because a number produced at the wrong
        # autonomy is not a worse number — it is a different measurement.
        "property": prepared["name"],
        "autonomy_level": REQUIRED_AUTONOMY,
        "autonomy_was": prepared["was"],
        "runs_per_scenario": args.runs,
        "scenarios": [s["key"] for s in chosen],
        "summaries": summaries,
        "turns": (before + after) if args.compare else after,
    }
    pathlib.Path(args.out).write_text(json.dumps(payload, indent=1, default=str))
    print(f"\n   every turn written to {args.out}")

    handed_back = await hand_back_bookings()
    if handed_back:
        print(f"   cancelled {handed_back} bookings this evaluation made, "
              f"through the product's own cancellation path")
    released = release_everything(everything)
    if released:
        print(f"   released {released} holds this evaluation took")
    if not args.keep:
        sweep(everything)
        print(f"   swept {len(everything)} evaluation threads")

    print(f"   {time.monotonic() - started:.0f}s total\n")

    # A non-zero exit when the number this pass is judged on is not met, so
    # this can sit in front of a deploy rather than beside one. §22: 95%.
    final = summaries[-1]
    if final["required_total"] and final["accuracy"] < 0.95:
        print(f"   ✗ required-tool accuracy {final['accuracy'] * 100:.1f}% is below 95%")
        return 1
    if final["invented_figures"] or final["leaked_ids"]:
        print("   ✗ an unsafe reply reached the report — see above")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
