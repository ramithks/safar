#!/usr/bin/env python3
"""
Does the suite actually kill the mutants that matter?

§11: *"Do not trust the test suite blindly."* A green suite proves the tests
pass, not that they would fail — and the previous pass found one clause in the
negotiation engine whose deletion broke nothing, because the number it guarded
never became a price. That mutant survived a suite of three thousand tests.

**Targeted rather than exhaustive.** A full mutation run over this codebase would
take hours and drown the signal in equivalent mutants. What is listed below is
the set of single edits that would each reintroduce a specific defect this
product has actually had, or remove a guard something depends on. Each one is a
sentence somebody could plausibly write while refactoring.

A survivor is not automatically a bug — it may be an equivalent mutation — but it
is always a question worth answering, and the answer belongs in a test.

    venv/bin/python scripts/mutation_probe.py
    venv/bin/python scripts/mutation_probe.py --only idempotency
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Mutant:
    """One edit, and what it would break if nothing caught it."""

    def __init__(self, group, name, path, before, after, harm, tests):
        self.group, self.name, self.harm, self.tests = group, name, harm, tests
        self.path = ROOT / path
        self.before, self.after = before, after


MUTANTS = [
    # ── the recovery ladder ─────────────────────────────────────────────────
    Mutant("recovery", "a filtered completion becomes retryable",
           "app/services/ai/recovery.py",
           'RETRYABLE = frozenset({EMPTY, MALFORMED, TRUNCATED, PROVIDER_ERROR})',
           'RETRYABLE = frozenset({EMPTY, MALFORMED, TRUNCATED, PROVIDER_ERROR, FILTERED})',
           "the same prompt is filtered twice and the guest waits double for the "
           "same refusal",
           "tests/test_silent_recovery.py"),
    Mutant("recovery", "a failed tool is described as done",
           "app/services/ai/recovery.py",
           'if getattr(o, "ok", False) and getattr(o, "output", None) is not None',
           'if getattr(o, "output", None) is not None',
           "a guest is told a booking exists because the tool that failed to "
           "make it returned a payload",
           "tests/test_silent_recovery.py"),
    Mutant("recovery", "malformed output stops being classified",
           "app/services/ai/recovery.py",
           '"MALFORMED_FUNCTION_CALL": MALFORMED,',
           '',
           "three of the nine real silent turns become unclassified again",
           "tests/test_silent_recovery.py"),

    # ── idempotency ─────────────────────────────────────────────────────────
    Mutant("idempotency", "a refusal is replayed as a success",
           "app/services/ai/registry.py",
           '.eq("status", "executed")',
           '',
           "a permission fix stays invisible until the guest writes again, and "
           "a refused call is reported to the model as done",
           "tests/test_turn_idempotency.py"),
    Mutant("idempotency", "the guard moves above the autonomy gate",
           "app/services/ai/registry.py",
           '    if spec.mutating and ctx.answering:\n        key = idempotency_key(',
           '    if False:\n        key = idempotency_key(',
           "every mutating call runs again on a redelivered message — two "
           "bookings, two payment links, two charges",
           "tests/test_turn_idempotency.py"),
    Mutant("idempotency", "the approval claim stops being conditional",
           "app/services/ai/approvals.py",
           '.eq("status", "awaiting_approval")\n        .execute().data',
           '.execute().data',
           "two managers pressing Approve both execute the tool",
           "tests/test_approval_is_claimed_once.py"),
    Mutant("idempotency", "a repeated write is re-run inside the turn",
           "app/services/ai/runtime.py",
           'repeated = (fingerprint in called_writes',
           'repeated = (False and fingerprint in called_writes',
           "a model that stutters bills the guest twice",
           "tests/test_silent_recovery.py tests/test_ai_runtime.py"),

    # ── guardrails ──────────────────────────────────────────────────────────
    Mutant("guardrails", "a denial no longer needs a lookup",
           "app/services/ai/guardrails.py",
           'if mood == DENIED and not licensed_denial:',
           'if mood == DENIED and False:',
           "a guest who has paid is told they have not, on a turn where nobody "
           "looked",
           "tests/test_confirmation_mood.py"),
    Mutant("guardrails", "the perfective is read as hypothetical",
           "app/services/ai/guardrails.py",
           '| hog [aeio] \\b',
           '| hog [aeio]? \\w* \\b',
           "'booking confirm ho gaya hai' stops being a claim, and a stay "
           "nobody made is announced",
           "tests/test_confirmation_mood.py"),
    Mutant("guardrails", "an unbacked figure is allowed through",
           "app/services/ai/guardrails.py",
           'unbacked = amounts - allowed',
           'unbacked = set()',
           "the model quotes a price nothing computed",
           "tests/test_ai_guardrails.py tests/test_outbound_guardrail.py"),

    # ── required actions ────────────────────────────────────────────────────
    Mutant("required", "a forced call overwrites the turn's fresh facts",
           "app/services/messaging/session.py",
           '            facts.set(field, args.get(field), argument_source)',
           '            facts.values[field] = args.get(field) or facts.values.get(field)',
           "a guest changes their dates and the session changes them back",
           "tests/test_conversation_corpus.py tests/test_fact_precedence.py"),
    Mutant("required", "a year is read as a budget again",
           "app/services/ai/required_actions.py",
           'if 2020 <= value <= 2035 and pattern in (_OFFERED_PRICE, _OFFERED_PRICE_AFTER):',
           'if False:',
           "'2026 mein aa rahe hain' opens a negotiation at ₹2,026",
           "tests/test_haggle_vocabulary.py"),
    Mutant("required", "a room number is read as money",
           "app/services/ai/required_actions.py",
           'if _NOT_MONEY_BEFORE.search(before):',
           'if False:',
           "'room 204 mein hain' offers the hotel ₹204",
           "tests/test_haggle_vocabulary.py"),
    Mutant("required", "the escalation vocabulary loses its person nouns",
           "app/services/ai/required_actions.py",
           'rf"(?:{_PERSON})\\s+(?:se|ko|si)\\s+(?:baat|bat|baate|connect|milwa|milva|bula)|"',
           '',
           "'human se baat karni hai' is answered by a machine",
           "tests/test_haggle_vocabulary.py"),

    # ── the catalogue ───────────────────────────────────────────────────────
    Mutant("catalogue", "the redundant-call instruction comes back",
           "app/services/ai/tools/read.py",
           '"rate plans that apply and an indicative total. This already includes "\n'
           '        "availability — do NOT call search_availability first. For a firm "',
           '"rate plans that apply and an indicative total. Use after "\n'
           '        "search_availability when the guest asks what the options are. For a firm "',
           "56% of the turns that reach get_stay_options pay for a preceding "
           "call — ~5,000 tokens and 3.5s each",
           "tests/test_tool_catalogue_audit.py"),
    Mutant("catalogue", "a tool stops handing the model its display strings",
           "app/services/ai/registry.py",
           '    result = money.with_display(result)',
           '    pass',
           "the model formats paise itself again — the largest class of the 30 "
           "guardrail-blocked production replies",
           "tests/test_money_is_never_formatted_by_the_model.py"),

    # ── conversation state ──────────────────────────────────────────────────
    Mutant("state", "a correction stops invalidating what it derived",
           "app/services/messaging/session.py",
           '            changed = corrections(profile, patch)',
           '            changed = set()',
           "a quote priced for two rooms survives the guest saying three, and "
           "goes into the next prompt",
           "tests/test_corrections_invalidate.py"),
    Mutant("state", "the guest's own words stop outranking tool arguments",
           "app/services/messaging/session.py",
           '        facts.update(required_actions.stated_facts(message), GUEST_SAID)',
           '        pass',
           "\"actually 4 log hain\" lands only if some tool happened to carry it",
           "tests/test_corrections_invalidate.py tests/test_conversation_corpus.py"),

    # ── entity resolution ───────────────────────────────────────────────────
    Mutant("entities", "the resolver guesses between two matching rooms",
           "app/services/pms/room_names.py",
           '    if len(contenders) > 1:',
           '    if False:',
           "a hotel with a Deluxe Balcony and a Deluxe Garden has 'the deluxe "
           "one' silently resolved to whichever sorted first",
           "tests/test_room_name_resolution.py"),
    Mutant("entities", "a partial word becomes a room selection",
           "app/services/pms/room_names.py",
           '        if any(h == 0.0 for h in hits):\n            # **Every significant token has to be there.**',
           '        if False:\n            # **Every significant token has to be there.**',
           "'is there a pool?' selects the Pool Villa",
           "tests/test_room_name_resolution.py"),
    Mutant("entities", "an ambiguous slash date is parsed anyway",
           "app/services/pms/date_facts.py",
           '    if slashed and int(slashed.group(1)) <= 12 and int(slashed.group(2)) <= 12:',
           '    if False:',
           "5/9 is read as one of its two meanings and the guest arrives four "
           "weeks late",
           "tests/test_date_corrections.py"),
    Mutant("entities", "each date stops being resolved from the one before it",
           "app/services/pms/date_facts.py",
           '        resolved = _next_with_day(day, anchor)',
           '        resolved = _next_with_day(day, today)',
           "'25th to 28th' said on the 26th comes out backwards and is refused, "
           "when the guest was perfectly clear",
           "tests/test_date_corrections.py"),

    Mutant("state", "the source table stops outranking anything",
           "app/services/messaging/session.py",
           '        if held is not None and SOURCE_RANK[source] < SOURCE_RANK[held]:',
           '        if False:',
           "a forced call built from the stale session overwrites the dates the "
           "guest just gave",
           "tests/test_fact_precedence.py"),
    Mutant("state", "a forced call is ranked as fresh evidence",
           "app/services/messaging/session.py",
           '        argument_source = CARRIED if forced else TOOL_ARGUMENT',
           '        argument_source = TOOL_ARGUMENT',
           "the session repeating itself outranks the guest correcting it",
           "tests/test_fact_precedence.py tests/test_conversation_corpus.py"),

    Mutant("state", "a guest statement can claim a business fact",
           "app/services/messaging/session.py",
           '        if field in BUSINESS_AUTHORITY and source not in AUTHORITATIVE_SOURCES:',
           '        if False:',
           "\"my booking is confirmed\" overwrites the payment state the "
           "provider owns",
           "tests/test_business_authority.py"),
    Mutant("state", "a tool argument counts as the system of record",
           "app/services/messaging/session.py",
           'AUTHORITATIVE_SOURCES = frozenset({TOOL_RESULT})',
           'AUTHORITATIVE_SOURCES = frozenset({TOOL_RESULT, TOOL_ARGUMENT})',
           "the model's reading of the guest sets a booking state, one remove "
           "from the guest saying it themselves",
           "tests/test_business_authority.py"),

    # ── the handoff invariant ───────────────────────────────────────────────
    Mutant("handoff", "a promised person is never actually fetched",
           "app/services/ai/runtime.py",
           '    if reply and not handoff and recovery.promises_a_person(reply):',
           '    if False:',
           "the model says somebody will get back to the guest and nobody is "
           "asked — the turn that failed the promotion gate, as a class",
           "tests/test_promised_a_person.py"),
    Mutant("handoff", "a promise that cannot be kept is sent anyway",
           "app/services/ai/runtime.py",
           '        if not marked and reply and recovery.promises_a_person(reply):',
           '        if False:',
           "a failed UPDATE leaves the guest promised a person on a thread the "
           "sweep never looks at",
           "tests/test_promised_a_person.py"),
    Mutant("handoff", "the handoff failure is swallowed again",
           "app/services/ai/runtime.py",
           '        return True\n    except Exception:  # noqa: BLE001',
           '        pass\n    except Exception:  # noqa: BLE001',
           "_ensure_human stops reporting success, so the withdrawal never fires",
           "tests/test_promised_a_person.py"),

    Mutant("entities", "a room number is counted as a room quantity",
           "app/services/ai/required_actions.py",
           '            if door and value == int(door):',
           '            if False:',
           "\"room 204 mein AC nahi chal raha\" forces group pricing for 204 "
           "rooms on a guest reporting a fault",
           "tests/test_room_numbers.py"),

    # ── business-rule provenance ────────────────────────────────────────────
    Mutant("provenance", "a failed rule read stops being distinguishable",
           "app/services/pms/negotiation.py",
           '        return rules_from_settings(fallback, EMERGENCY_DEFAULT)',
           '        return rules_from_settings(fallback)',
           "a hotel whose configured rules stopped applying is invisible again "
           "— the exact bug that started this pass",
           "tests/test_rule_provenance.py"),
    Mutant("provenance", "provenance stops reaching the run row",
           "app/services/ai/runtime.py",
           '        summary_of_requirements["provenance"] = ctx.turn.telemetry',
           '        pass',
           "the fallback is named and nobody can query it",
           "tests/test_rule_provenance.py"),
    Mutant("provenance", "the rule scope leaks into the model's context",
           "app/services/pms/negotiation.py",
           "        return out",
           '        out["ruleSource"] = self.rule_source\n        return out',
           "the name of our configuration scope goes into every negotiation "
           "result the model sees — the class of thing the leak guardrail "
           "refuses in a reply",
           "tests/test_rule_provenance.py"),

    Mutant("provenance", "a quote stops supplying the rupees behind its percentage",
           "app/services/ai/tools/pricing.py",
           '        payload["depositPaise"] = deposit_paise\n'
           '        payload["deposit"] = money.display(deposit_paise)',
           '        pass',
           "the model is handed a deposit percentage beside a total and works "
           "the rupees out itself — the ₹19,784 fabrication, exactly",
           "tests/test_business_truth_provenance.py"),

    # ── the negotiation floor ───────────────────────────────────────────────
    Mutant("negotiation", "the approval ceiling stops being clamped",
           "app/services/pms/negotiation.py",
           '        approved_total = max(approved_total, floor)',
           '        pass',
           "a price below the floor is sent to a manager who cannot grant it",
           "tests/test_invariants.py"),
    Mutant("negotiation", "the automatic ceiling stops being clamped",
           "app/services/pms/negotiation.py",
           '        auto_total = max(auto_total, floor)',
           '        pass',
           "the agent gives away more than the hotel's floor, unattended",
           "tests/test_invariants.py"),
]


def run(mutant: Mutant, timeout: int) -> tuple[str, float]:
    original = mutant.path.read_text()
    if mutant.before not in original:
        return "STALE", 0.0
    started = time.monotonic()
    mutant.path.write_text(original.replace(mutant.before, mutant.after, 1))
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *mutant.tests.split(), "-x", "-q",
             "--no-header", "-p", "no:cacheprovider"],
            cwd=ROOT, capture_output=True, timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        killed = proc.returncode != 0
    except subprocess.TimeoutExpired:
        killed = True  # a hang is a failure, and a failure is a kill
    finally:
        mutant.path.write_text(original)
    return ("KILLED" if killed else "SURVIVED"), time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="one group: recovery, idempotency, "
                                       "guardrails, required, negotiation")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    chosen = [m for m in MUTANTS if not args.only or m.group == args.only]
    print(f"{BOLD}{len(chosen)} mutants{OFF}  {DIM}each one a defect this product "
          f"has had, or a guard something depends on{OFF}\n")

    survivors, stale = [], []
    group = None
    for mutant in chosen:
        if mutant.group != group:
            group = mutant.group
            print(f"{DIM}── {group} ──{OFF}")
        verdict, seconds = run(mutant, args.timeout)
        if verdict == "KILLED":
            print(f"  {GREEN}killed  {OFF} {mutant.name}  {DIM}{seconds:.1f}s{OFF}")
        elif verdict == "STALE":
            stale.append(mutant)
            print(f"  {DIM}stale   {mutant.name} — the code it edits has moved{OFF}")
        else:
            survivors.append(mutant)
            print(f"  {RED}SURVIVED{OFF} {mutant.name}  {DIM}{seconds:.1f}s{OFF}")
            print(f"           {DIM}would mean: {mutant.harm}{OFF}")

    print(f"\n══ {len(chosen) - len(survivors) - len(stale)} killed, "
          f"{len(survivors)} survived, {len(stale)} stale ══")
    for mutant in survivors:
        print(f"  {RED}·{OFF} {mutant.group}/{mutant.name} — {mutant.harm}")
    for mutant in stale:
        print(f"  {DIM}·{OFF} {mutant.group}/{mutant.name} needs rewriting "
              f"against the current source")
    return 1 if survivors or stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
