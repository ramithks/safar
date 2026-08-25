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
           'if forced and field in established:\n                continue',
           'if False:\n                continue',
           "a guest changes their dates and the session changes them back",
           "tests/test_conversation_corpus.py"),
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
