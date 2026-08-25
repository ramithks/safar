"""
A rate limit is not a model failure, and a report that cannot tell them apart
will eventually blame a model for somebody else's quota.

── the failure this exists for ─────────────────────────────────────────────

A 560-turn sweep and an eleven-message journey ran against the same Vertex
project at the same time. The transport did what it is built to do — rotated
regions, backed off, retried — and two turns took 131 and 95 seconds, blew the
turn budget, and escalated. Every layer above the transport saw a slow turn
that gave up.

One of those is a capacity problem belonging to whoever else is on the quota.
The other is a model-quality signal. They looked identical, and the report was
about to record the second.

── the thirteen classes ────────────────────────────────────────────────────

Every attempted turn is exactly one of these. Ordered most-specific-first,
because a turn can be several things at once and the *cause* is what a reader
needs — a guardrail block on a turn that also fell back is a guardrail block.

  the model answered
    MODEL_SUCCESS               it spoke, or called a tool, and the turn stood
    FALLBACK_SUCCESS            it answered, but not on the configured model
    GUARDRAIL_BLOCK             it answered and the reply was refused

  the model did not answer
    MODEL_EMPTY                 succeeded and returned nothing
    MODEL_MALFORMED_TOOL_CALL   tried to call a tool and could not
    MODEL_TIMEOUT               ran out of the turn's budget
    FALLBACK_FAILURE            every model on the ladder failed

  the provider did not answer
    PROVIDER_429                quota. Never a model-quality failure.
    PROVIDER_5XX                the service, not the model
    NETWORK_FAILURE            the call never arrived

  us
    TOOL_FAILURE                a tool raised or refused
    RUNTIME_FAILURE             the turn itself broke
    EVALUATOR_FAILURE           the harness broke, and nothing was measured

── the three denominators ──────────────────────────────────────────────────

    ALL ATTEMPTS         everything tried, including what never left the house
    PROVIDER_SUCCESS     attempts the provider actually served
    MODEL_EVALUABLE      attempts that say something about a model

**Model-quality percentages use MODEL_EVALUABLE.** Anything else silently
prices a rate limit into a model's accuracy — and because contention is
correlated with load rather than with the model, the model under heaviest test
takes the worst of it.
"""
from collections import Counter

MODEL_SUCCESS = "MODEL_SUCCESS"
MODEL_EMPTY = "MODEL_EMPTY"
MODEL_MALFORMED_TOOL_CALL = "MODEL_MALFORMED_TOOL_CALL"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
PROVIDER_429 = "PROVIDER_429"
PROVIDER_5XX = "PROVIDER_5XX"
NETWORK_FAILURE = "NETWORK_FAILURE"
RUNTIME_FAILURE = "RUNTIME_FAILURE"
TOOL_FAILURE = "TOOL_FAILURE"
GUARDRAIL_BLOCK = "GUARDRAIL_BLOCK"
FALLBACK_SUCCESS = "FALLBACK_SUCCESS"
FALLBACK_FAILURE = "FALLBACK_FAILURE"
EVALUATOR_FAILURE = "EVALUATOR_FAILURE"

CLASSES = (
    MODEL_SUCCESS, MODEL_EMPTY, MODEL_MALFORMED_TOOL_CALL, MODEL_TIMEOUT,
    PROVIDER_429, PROVIDER_5XX, NETWORK_FAILURE, RUNTIME_FAILURE,
    TOOL_FAILURE, GUARDRAIL_BLOCK, FALLBACK_SUCCESS, FALLBACK_FAILURE,
    EVALUATOR_FAILURE,
)

# The provider never served these, so nothing about them is about a model.
NOT_PROVIDER_SUCCESS = frozenset({
    PROVIDER_429, PROVIDER_5XX, NETWORK_FAILURE, EVALUATOR_FAILURE,
})

# **What a model-quality percentage may be computed over.**
#
# A guardrail block is in, deliberately: the model wrote a reply the product
# refused, which is exactly a model-quality event and the largest failure class
# on real traffic. A tool failure is in for the same reason from the other side
# — a model that calls a tool wrongly is a model that did the wrong thing.
#
# Out: everything the provider or the harness caused, and RUNTIME_FAILURE,
# which is our bug and not the model's. Counting our own crash against a model
# is how a build teaches itself that a model is unreliable.
MODEL_EVALUABLE = frozenset({
    MODEL_SUCCESS, MODEL_EMPTY, MODEL_MALFORMED_TOOL_CALL, MODEL_TIMEOUT,
    GUARDRAIL_BLOCK, FALLBACK_SUCCESS, FALLBACK_FAILURE, TOOL_FAILURE,
})

# Errors whose class names mean "the call never got there".
_NETWORK = ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
            "PoolTimeout", "RemoteProtocolError", "ReadError", "WriteError",
            "SSLError", "ProxyError")


def _provider_trouble(attempts: list[dict]) -> str | None:
    """
    Which provider-level problem this turn hit, if any.

    Reads the transport's own account — `CallTrace.retries` and the upstream
    exception class — rather than inferring from latency. A turn is slow for
    many reasons and only one of them is a quota someone else is using.
    """
    for attempt in attempts or []:
        status = (attempt.get("provider_status") or "").lower()
        error = attempt.get("error_class") or ""
        if "quota" in status or "429" in status or "VertexQuotaExhausted" in error:
            return PROVIDER_429
        if "VertexUpstreamError" in error or "503" in status or "500" in status:
            return PROVIDER_5XX
        if any(name in error for name in _NETWORK):
            return NETWORK_FAILURE
    return None


def classify(*, run: dict | None, actions: list[dict] | None = None,
             harness_error: str | None = None,
             budget_exceeded: bool = False) -> str:
    """
    Which of the thirteen this attempted turn was.

    `run` is the `agent_runs` row (or a `RunResult`'s requirements plus status).
    `actions` are its `agent_actions`. Both are what the runtime recorded, so
    this and any dashboard reach the same verdict from the same fields.
    """
    if harness_error:
        return EVALUATOR_FAILURE
    if not run:
        return EVALUATOR_FAILURE

    requirements = run.get("required_actions") or {}
    model_record = requirements.get("model") or {}
    attempts = model_record.get("attempts") or []
    recovery_log = requirements.get("recovery") or []
    status = (run.get("status") or "").lower()
    error = run.get("error") or ""

    # ── the provider, first ─────────────────────────────────────────────────
    #
    # Ahead of everything, because a turn the provider never served cannot be
    # evidence about a model however it ended. This is the whole point of the
    # module and the one ordering decision that is not negotiable.
    trouble = _provider_trouble(attempts)
    if trouble and status != "succeeded":
        return trouble

    # ── the guardrail ───────────────────────────────────────────────────────
    if requirements.get("guardrail") or error.startswith("guardrail:"):
        return GUARDRAIL_BLOCK

    # ── the model, or the ladder ────────────────────────────────────────────
    failures = {a.get("failure") for a in recovery_log}
    fell_back = bool(model_record.get("fallback_count")) or any(
        a.get("method") == "fallback" for a in recovery_log)

    if status == "failed":
        if "empty_completion" in failures:
            return FALLBACK_FAILURE if fell_back else MODEL_EMPTY
        if "malformed_tool_call" in failures:
            return MODEL_MALFORMED_TOOL_CALL
        if trouble:
            return trouble
        return RUNTIME_FAILURE

    if budget_exceeded or "out of budget" in {a.get("method") for a in recovery_log}:
        # A turn that ran out of time. Kept distinct from PROVIDER_429 above:
        # if the transport was rotating regions this never gets reached, and if
        # it was not then the time went somewhere else.
        return MODEL_TIMEOUT

    if any((a.get("status") or "") == "failed" for a in actions or []):
        return TOOL_FAILURE

    if fell_back:
        return FALLBACK_SUCCESS
    if "empty_completion" in failures:
        return MODEL_EMPTY
    if "malformed_tool_call" in failures:
        return MODEL_MALFORMED_TOOL_CALL

    return MODEL_SUCCESS


def summarise(classified: list[str]) -> dict:
    """
    The three denominators and what sits inside each.

    No accuracy figure is produced here. A caller that wants one takes
    `model_evaluable` and divides by it, which forces the denominator to be
    named rather than assumed.
    """
    counts = Counter(classified)
    total = len(classified)
    provider_success = sum(n for cls, n in counts.items()
                           if cls not in NOT_PROVIDER_SUCCESS)
    evaluable = sum(n for cls, n in counts.items() if cls in MODEL_EVALUABLE)
    return {
        "all_attempts": total,
        "provider_success_attempts": provider_success,
        "model_evaluable_attempts": evaluable,
        "counts": {cls: counts.get(cls, 0) for cls in CLASSES if counts.get(cls)},
        # What was thrown away and why, so a shrinking denominator is visible
        # rather than merely smaller.
        "excluded": {cls: counts[cls] for cls in counts
                     if cls not in MODEL_EVALUABLE},
    }
