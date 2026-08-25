"""
What went wrong, in sixteen words the whole system agrees on.

§20 of the reliability brief: *"Every failed evaluation should map to one. This
will make future debugging dramatically faster."*

**Why a classifier and not an enum.** A vocabulary nothing produces is a
vocabulary nobody queries. The failures this has to name already exist and are
already recorded — `agent_runs.error`, `agent_actions.status` and
`refusal_reason`, the guardrail's verdict, the arbiter's judgement — spread
across three columns in two tables and written by a dozen call sites in prose.
What is missing is not the data; it is one answer to "what *kind* of failure was
that", derivable from the rows anybody already has.

So this reads a run and its actions and returns a category. Nothing has to be
instrumented, no call site changes, and it works on rows written before it
existed — which matters, because the first question it gets asked is about the
last thirty days.

**The order is the point.** A turn can be several of these at once: a tool
refused by autonomy, no reply, and a handoff. Classifying it as `HANDOFF_ERROR`
would be true and useless — the handoff is the *consequence*. The rules below run
most-specific first and stop, so a turn is filed under the thing that started it.

**Not a severity ladder.** `GUARDRAIL_BLOCK` is the system working; `MODEL_FAILURE`
is the model not answering; `INVENTORY_CONFLICT` may be an ordinary sold-out
Saturday. What the category says is *where to look*, not how bad it is.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── the vocabulary ───────────────────────────────────────────────────────────

INTENT_ERROR = "INTENT_ERROR"                    # the turn's meaning was misread
ENTITY_ERROR = "ENTITY_ERROR"                    # a date, a count, a figure read wrong
STATE_ERROR = "STATE_ERROR"                      # answered from a stale or absent session
TOOL_SELECTION_ERROR = "TOOL_SELECTION_ERROR"    # a required tool was never called
POLICY_ERROR = "POLICY_ERROR"                    # the property's own rules refused it
AUTONOMY_ERROR = "AUTONOMY_ERROR"                # the rung was too low to act
HANDOFF_ERROR = "HANDOFF_ERROR"                  # a handoff that should not have happened
TOOL_FAILURE = "TOOL_FAILURE"                    # a tool raised
BUSINESS_RULE_FAILURE = "BUSINESS_RULE_FAILURE"  # a precheck or validation said no
INVENTORY_CONFLICT = "INVENTORY_CONFLICT"        # the room was not there to sell
PAYMENT_FAILURE = "PAYMENT_FAILURE"              # the money path
MESSAGING_FAILURE = "MESSAGING_FAILURE"          # the reply could not be delivered
MODEL_FAILURE = "MODEL_FAILURE"                  # the model returned nothing, or nothing usable
GUARDRAIL_BLOCK = "GUARDRAIL_BLOCK"              # the reply was refused before sending
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"      # the hotel is not set up for this
INTEGRATION_ERROR = "INTEGRATION_ERROR"          # an upstream we do not own
NONE = "NONE"                                    # nothing went wrong

ALL = (
    INTENT_ERROR, ENTITY_ERROR, STATE_ERROR, TOOL_SELECTION_ERROR, POLICY_ERROR,
    AUTONOMY_ERROR, HANDOFF_ERROR, TOOL_FAILURE, BUSINESS_RULE_FAILURE,
    INVENTORY_CONFLICT, PAYMENT_FAILURE, MESSAGING_FAILURE, MODEL_FAILURE,
    GUARDRAIL_BLOCK, CONFIGURATION_ERROR, INTEGRATION_ERROR, NONE,
)

# Which layer owns the fix. The second question after "what kind" is always
# "whose problem is it", and answering it here stops every triage conversation
# rediscovering the same mapping.
OWNER = {
    INTENT_ERROR: "required_actions — the vocabulary",
    ENTITY_ERROR: "required_actions — the extractors",
    STATE_ERROR: "messaging/session",
    TOOL_SELECTION_ERROR: "required_actions + toolsets",
    POLICY_ERROR: "the property's configuration",
    AUTONOMY_ERROR: "the property's autonomy level",
    HANDOFF_ERROR: "services/ai/arbiter",
    TOOL_FAILURE: "the tool",
    BUSINESS_RULE_FAILURE: "the tool's precheck or input model",
    INVENTORY_CONFLICT: "pms/booking — or nothing, the room was sold",
    PAYMENT_FAILURE: "pms/payments",
    MESSAGING_FAILURE: "messaging/channels",
    MODEL_FAILURE: "the provider, or the model",
    GUARDRAIL_BLOCK: "services/ai/guardrails — or the model, which lied",
    CONFIGURATION_ERROR: "onboarding",
    INTEGRATION_ERROR: "the upstream",
    NONE: "",
}


@dataclass(frozen=True)
class Diagnosis:
    category: str
    why: str
    """The evidence, quoted. A category without the sentence it came from is
    an assertion; with it, somebody can disagree."""

    @property
    def owner(self) -> str:
        return OWNER.get(self.category, "")

    def __bool__(self) -> bool:
        return self.category != NONE


# ── the evidence, matched ────────────────────────────────────────────────────
#
# Patterns rather than equality, because these strings are written by a dozen
# call sites and several of them interpolate a tool name or an exception class.

_UPSTREAM = re.compile(
    r"readtimeout|timeout|502|503|504|429|unavailable|vertex call failed|"
    r"connection|bad_record_mac|ssl", re.I)
_NO_REPLY = re.compile(r"produced no reply|empty completion|finishreason", re.I)
# `units` alone was here and matched *"get_group_rates arguments rejected —
# units: Field required"*, filing an argument-validation bug as a sold-out
# Saturday. A word that appears in both a business outcome and a schema error is
# not evidence of either.
_INVENTORY = re.compile(r"no_stock|sold out|no longer available|not available|"
                        r"no availability|units_(?:held|booked)|inventory|no rooms",
                        re.I)

# Argument validation, which is the model getting a schema wrong and is nothing
# to do with the hotel. Checked before everything else a refusal could be,
# because its message quotes the field name and field names collide with
# business vocabulary.
_ARGUMENTS = re.compile(r"arguments rejected|field required|validation", re.I)

# The arbiter turning down a handoff the model asked for. This is the whole
# subject of the reliability pass, and it arrives as a tool refusal — so without
# naming it here it files as a generic business-rule failure, which is the one
# categorisation that would hide the number the work exists to move.
_ARBITER = re.compile(r"handoff_not_supported", re.I)
_PAYMENT = re.compile(r"razorpay|payment|refund|order_|charge", re.I)
_MESSAGING = re.compile(r"message_window_closed|template|not deliverable|"
                        r"send failed|messaging", re.I)
_CONFIG = re.compile(r"not configured|no agent|no rate plan|no group|"
                     r"missing polic|no messaging polic|not set up", re.I)


def _actions(actions) -> list:
    return list(actions or [])


def _field(row, name, default=None):
    """Works on a dict from PostgREST and on a `ToolOutcome` alike."""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def diagnose(run, actions=None) -> Diagnosis:
    """
    Read a run and its actions and name the failure.

    `run` is an `agent_runs` row or a `RunResult`; `actions` are `agent_actions`
    rows or `ToolOutcome`s. Both shapes appear in this codebase — the evaluation
    holds objects, a dashboard query holds dicts — and neither should have to
    convert to ask this question.
    """
    error = str(_field(run, "error") or "")
    status = str(_field(run, "status") or "")
    acts = _actions(actions if actions is not None else _field(run, "actions"))

    # 1. The reply was stopped. Most specific thing that can happen to a turn,
    #    and the only one that is the system working rather than failing.
    if error.startswith("guardrail:"):
        return Diagnosis(GUARDRAIL_BLOCK, error[:200])

    # 2. An upstream we do not own, wherever it surfaced. Checked before the
    #    tool and model rules because both of those wrap it: a `get_quote` that
    #    failed on a Vertex timeout is not a bug in get_quote.
    if _UPSTREAM.search(error):
        return Diagnosis(INTEGRATION_ERROR, error[:200])

    # 3. The hotel, or the deployment, is not set up for this. Before the model
    #    rule because it surfaces as a failed run with a prose error, and
    #    *"OPENAI_API_KEY is not configured"* filed as MODEL_FAILURE until this
    #    classifier was first run against thirty days of real rows.
    if _CONFIG.search(error):
        return Diagnosis(CONFIGURATION_ERROR, error[:200])

    # 4. The model said nothing usable.
    if _NO_REPLY.search(error):
        return Diagnosis(MODEL_FAILURE, error[:200])

    # 4. What the tools themselves reported, most specific first. A refusal
    #    carries its own reason and that reason is better evidence than
    #    anything on the run row.
    for action in acts:
        state = str(_field(action, "status") or "")
        reason = str(_field(action, "refusal_reason") or _field(action, "error") or "")
        if state not in ("failed", "refused", "rejected"):
            continue
        if _ARBITER.search(reason):
            return Diagnosis(HANDOFF_ERROR, reason[:200])
        if _UPSTREAM.search(reason):
            return Diagnosis(INTEGRATION_ERROR, reason[:200])
        if _ARGUMENTS.search(reason):
            return Diagnosis(BUSINESS_RULE_FAILURE,
                             f"{_field(action, 'tool')}: {reason[:180]}")
        if _INVENTORY.search(reason):
            return Diagnosis(INVENTORY_CONFLICT, reason[:200])
        if _PAYMENT.search(reason):
            return Diagnosis(PAYMENT_FAILURE, reason[:200])
        if _MESSAGING.search(reason):
            return Diagnosis(MESSAGING_FAILURE, reason[:200])
        if _CONFIG.search(reason):
            return Diagnosis(CONFIGURATION_ERROR, reason[:200])
        if "autonomy" in reason.lower() or state == "rejected":
            return Diagnosis(AUTONOMY_ERROR, reason[:200])
        if "permission" in reason.lower() or "not enabled" in reason.lower():
            return Diagnosis(POLICY_ERROR, reason[:200])
        if state == "failed":
            return Diagnosis(TOOL_FAILURE, f"{_field(action, 'tool')}: {reason[:180]}")
        return Diagnosis(BUSINESS_RULE_FAILURE, f"{_field(action, 'tool')}: {reason[:180]}")

    # 5. The arbiter turned a handoff down and the turn ended in one anyway, or
    #    the turn handed off with nothing to justify it. Read from the run's own
    #    summary — see services/ai/runtime, `handoff_judgements`.
    required = _field(run, "required_actions") or _field(run, "requirements") or {}
    if isinstance(required, dict):
        judgements = required.get("handoff_judgements") or []
        if judgements and status == "escalated":
            return Diagnosis(HANDOFF_ERROR, str(judgements[0])[:200])

    # 6. The turn owed a business tool and did not pay it. This is the metric
    #    the whole required-action layer exists to move, so it is named rather
    #    than folded into a generic failure.
    if isinstance(required, dict):
        state = _field(run, "required_action_status") or required.get("status")
        if state == "unmet":
            unmet = required.get("unmet") or required.get("observed_unmet") or []
            return Diagnosis(TOOL_SELECTION_ERROR,
                             f"required but never called: {unmet}"[:200])

    if status == "failed" and error:
        return Diagnosis(MODEL_FAILURE, error[:200])

    # **An escalation is not a failure, and calling it one buries the number
    # that matters.**
    #
    # The first run of this classifier against thirty days of production filed
    # 41% of everything under HANDOFF_ERROR on this line — 93 runs whose only
    # evidence was `status = 'escalated'` and no error at all. But that is
    # exactly what a *correct* handoff looks like: the guest asked for a person,
    # `request_human_handoff` succeeded, and the run closed as escalated with
    # nothing wrong. Counting those as errors makes the arbiter look like it is
    # failing 93 times in the same report that is meant to measure it.
    #
    # A handoff is an error when something says it should not have happened:
    # the arbiter turned it down (matched above, on the tool refusal), or the
    # run escalated carrying a rejected judgement (matched at step 5). With
    # neither, the honest answer is that the turn ended the way it was supposed
    # to — and `why` says so rather than leaving a reader to guess what an empty
    # category means.
    if status == "escalated":
        return Diagnosis(NONE, "handed to a person, with nothing recorded against it")
    if status == "failed":
        return Diagnosis(MODEL_FAILURE, "failed with no error recorded")
    return Diagnosis(NONE, "")


def tally(rows) -> dict[str, int]:
    """
    Counts by category, in a fixed order.

    Ordered rather than sorted by frequency: a report whose rows move between
    runs cannot be read side by side, and comparing two runs is the entire
    reason anybody counts these.
    """
    counts = {name: 0 for name in ALL}
    for run, actions in rows:
        counts[diagnose(run, actions).category] += 1
    return {name: n for name, n in counts.items() if n}
