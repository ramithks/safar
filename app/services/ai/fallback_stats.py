"""
Three populations, never one.

── the failure this exists for ──────────────────────────────────────────────

The model matrix ran eleven scenarios four times against two model families and
reported a column per family. Eleven of the forty-four turns in the 3.x column
finished on the fallback tier after two consecutive empty completions each —
the recovery ladder working exactly as designed — and every one of them was
counted as a 3.x turn, because the only model on the result was the one the
router had chosen.

A quarter of the column was the other model's work. The comparison read as a
verdict on Gemini 3.x and was in part a verdict on Gemini 2.5 doing 3.x's
recovery, and no number in it could be trusted to belong to the model whose
name was above it.

── the three populations ────────────────────────────────────────────────────

  PRIMARY_ONLY    the configured model answered, start to finish. The only
                  population that says anything about that model.

  FALLBACK_USED   the configured model was tried, failed, and something else
                  finished the turn. Says something about *the ladder* —
                  whether recovery works — and nothing clean about either
                  model on its own.

  FALLBACK_ONLY   the configured model never produced anything usable at all.
                  The turn is entirely the fallback's, and attributing it to
                  the primary is the specific error above.

They are never summed into one accuracy figure. A blended number is the thing
that hid the problem: 3.x's apparent 87.1% unprompted-tool rate was three
quarters 3.x and one quarter 2.5, and no amount of significant figures makes
that a measurement of anything.

`assign` reads the record `runtime` writes on every turn — see
`summary_of_requirements["model"]` — so the evaluator and any dashboard get the
same answer from the same field, rather than each deciding for itself what
counts as a fallback.
"""
from collections import defaultdict

PRIMARY_ONLY = "PRIMARY_ONLY"
FALLBACK_USED = "FALLBACK_USED"
FALLBACK_ONLY = "FALLBACK_ONLY"
UNKNOWN = "UNKNOWN"

POPULATIONS = (PRIMARY_ONLY, FALLBACK_USED, FALLBACK_ONLY)


def assign(record: dict | None) -> str:
    """
    Which population one turn belongs to.

    `record` is `agent_runs.required_actions["model"]`, or the same dict off a
    `RunResult`'s requirements. A turn with no record is UNKNOWN and is never
    silently folded into PRIMARY_ONLY — turns written before this field existed
    are not evidence that no fallback happened, and treating absence as
    "primary" would recreate the original error in the reporting layer.
    """
    if not record:
        return UNKNOWN

    configured = record.get("configured")
    served = list(record.get("served_by") or [])
    finished = record.get("finished_on")
    fallbacks = record.get("fallback_count") or 0

    if not configured or not served:
        return UNKNOWN

    if not fallbacks and served == [configured]:
        return PRIMARY_ONLY

    # The configured model never contributed anything usable. `served_by` is
    # the models that produced tokens, in order; if the configured one is not
    # among them, it was tried and gave nothing.
    if configured not in served:
        return FALLBACK_ONLY

    # It contributed and something else finished. Also the case where the turn
    # climbed down and back — `fallback_count` is the authority, not the
    # endpoint, because a turn that fell back and recovered still cost two
    # models' work and is not a clean reading of either.
    if fallbacks or (finished and finished != configured):
        return FALLBACK_USED

    return PRIMARY_ONLY


def split(turns: list[dict], *, record_key: str = "model") -> dict[str, list[dict]]:
    """
    The turns, grouped. Every population is a key even when empty, so a report
    that iterates this cannot silently omit the one that had no members —
    "FALLBACK_ONLY: 0" and no line at all read very differently to a person.
    """
    grouped: dict[str, list[dict]] = {p: [] for p in (*POPULATIONS, UNKNOWN)}
    for turn in turns:
        grouped[assign(turn.get(record_key))].append(turn)
    return grouped


def summarise(turns: list[dict], *, record_key: str = "model") -> dict:
    """
    Counts, rates and the error classes behind them — per population.

    No total accuracy figure is produced here, deliberately. A caller that
    wants one has to pick a population and say which, which is the whole
    argument of this module.
    """
    grouped = split(turns, record_key=record_key)
    n = len(turns)
    reasons: dict[str, int] = defaultdict(int)
    classes: dict[str, int] = defaultdict(int)
    for turn in turns:
        record = turn.get(record_key) or {}
        for reason in record.get("fallback_reasons") or []:
            reasons[reason] += 1
        for attempt in record.get("attempts") or []:
            if attempt.get("error_class"):
                classes[attempt["error_class"]] += 1

    return {
        "n": n,
        "counts": {p: len(rows) for p, rows in grouped.items()},
        "rates": {p: (len(rows) / n if n else 0.0) for p, rows in grouped.items()},
        # How often the ladder fired at all, which is the number that says
        # whether the primary is healthy. Distinct from the populations: a turn
        # can fall back and still be finished by the primary on a later step.
        "turns_that_fell_back": sum(
            1 for t in turns if (t.get(record_key) or {}).get("fallback_count")),
        "fallback_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "provider_error_classes": dict(sorted(classes.items(), key=lambda kv: -kv[1])),
    }
