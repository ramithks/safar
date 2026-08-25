"""
What to do when the model does not answer.

**The number this exists for, and the correction that came with it.**
`agent_runs.error = 'the model produced no reply'` was 39% of the recorded
failure taxonomy and the largest single reliability signal in the system. Traced
run by run, it is four different things wearing one error string, and most of it
was not the product:

    94 runs recorded          the whole class, 90 days
    −80 provider = 'echo'     e2e scripts whose scripted provider ran out
    ─────
    14 real turns
    −5  send_message executed the guest *was* answered; the model declined to
                              repeat itself in prose (already handled)
    ─────
     9 turns where a guest, or a hotelier, got silence

Nine is a real number and it is the one this module is written against. It is
also not 39% of anything, and saying so is the point rather than a way out: the
metric was counting our own test scripts, and every hour spent on the wrong 80
was an hour not spent on the nine. `EchoProvider` exhausting its script returns
`ModelStep(reply="", tokens_in=1, tokens_out=1)`, which is indistinguishable in
the telemetry from a model that answered nothing — so a *test bug* was being
recorded as a production failure, at a rate that buried the production failure.

**The nine, classified from the rows.** Each has a distinct cause and a distinct
right answer, which is why "no reply → handoff" was the wrong shape:

    MALFORMED   3   2.5-pro, complex_negotiation, finishReason=
                    MALFORMED_FUNCTION_CALL, 4 output tokens, 31–83s.
                    The model tried to emit a tool call and produced something
                    unparseable. A sampling failure — the same prompt works on
                    the next attempt. → retry, then a different model.

    EMPTY       3   2.5-flash, booking, ~6.5s, ~2,730 in, 1 out, no tools.
                    A blank candidate, back too fast to have been generated.
                    → retry, then a different model.

    NO_PROSE    3   2.5-pro, revenue_analysis. Tools did real work —
                    compare_with_previous_period, create_revenue_recommendation —
                    and the model then said nothing. The hotelier sees no answer
                    to a question the system has already answered.
                    → say what the tools established, deterministically.

The ladder is retry → fallback → deterministic → handoff, and handoff is the
last rung rather than the first. What it must never become is a way to answer a
guest with something nobody computed: the deterministic rung builds its sentence
only from display strings a tool returned, and is then put through
`guardrails.check_reply` like any other reply — so it is incapable of inventing a
figure by construction, not by review.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ── what came back ───────────────────────────────────────────────────────────

USABLE = "usable"                  # it spoke, or it asked for tools
EMPTY = "empty_completion"         # succeeded and returned nothing
MALFORMED = "malformed_tool_call"  # tried to call a tool and could not
TRUNCATED = "truncated"            # ran out of output budget mid-sentence
FILTERED = "safety_filtered"       # the provider's own filter stopped it
PROVIDER_ERROR = "provider_error"  # the call itself failed — raised, not returned

# Which of those are worth trying again *at all*. A filtered completion is not:
# the same prompt filtered once will filter again, and the honest end of it is a
# person. Everything else here is a bad sample rather than a bad request.
RETRYABLE = frozenset({EMPTY, MALFORMED, TRUNCATED, PROVIDER_ERROR})

# Vertex's `finishReason` values that mean the content is unusable. `STOP` is
# absent deliberately — a model that stops cleanly and says nothing is EMPTY,
# which is a different thing from one that was cut off, and the two want
# different recoveries.
_FINISH = {
    "MALFORMED_FUNCTION_CALL": MALFORMED,
    "MAX_TOKENS": TRUNCATED,
    "SAFETY": FILTERED,
    "RECITATION": FILTERED,
    "BLOCKLIST": FILTERED,
    "PROHIBITED_CONTENT": FILTERED,
    "SPII": FILTERED,
}


def classify(step) -> str:
    """
    What kind of non-answer this is, or `USABLE`.

    Read from the step rather than from the exception, because the failures that
    matter here did not raise: Vertex returned 200, a candidate, a finish reason
    and no content. `getattr` throughout, because a diagnostic must not be the
    thing that breaks the turn it is describing — any provider may hand back a
    step object without these fields.
    """
    if step is None:
        return PROVIDER_ERROR
    if getattr(step, "wants_tools", False):
        return USABLE
    if (getattr(step, "reply", None) or "").strip():
        return USABLE

    finish = (getattr(step, "finish_reason", None) or "").upper()
    named = _FINISH.get(finish)
    if named:
        return named
    return EMPTY


@dataclass
class Attempt:
    """One rung of the ladder, for the record."""

    failure: str
    model: str
    method: str          # 'retry' · 'fallback' · 'deterministic' · 'handoff'

    def as_dict(self) -> dict:
        return {"failure": self.failure, "model": self.model, "method": self.method}


# ── saying what the tools established ────────────────────────────────────────

# Result types whose output carries something a person can be told, and the
# display field to tell them from. Display strings only: every one of these is
# already formatted by `money.display` or by the tool, so nothing here converts,
# rounds or computes.
#
# Deliberately short. A result type absent from this table produces no
# deterministic reply at all, which falls through to a handoff — the right
# outcome for anything we cannot describe without guessing.
_SAYABLE = {
    "quote": ("total", "I've got the price for those dates: {}."),
    "reservation": ("ref", "That's done — the reference is {}."),
    "payment_link": ("url", "Here's the payment link: {}"),
    "folio_entry": ("total", "That's added to the bill: {}."),
}


def deterministic_reply(outcomes, *, staff: bool) -> str | None:
    """
    A reply built only from what a tool returned, or None.

    **The third rung, and the narrowest.** It runs when the model has already
    been retried and fallen back and still said nothing, and the turn has real
    tool results sitting unreported. Silence is the alternative, and for the
    hotelier asking a revenue question it is the whole of what they got.

    Two rules make it safe rather than merely useful. It only ever *quotes*
    display strings a tool produced — no arithmetic, no rounding, no assembling
    a figure from parts. And the caller puts it through `guardrails.check_reply`
    exactly as it would a model's reply, so a sentence carrying a number no tool
    result contains is refused by the same rule that refuses one the model
    invented. It is incapable of inventing business truth by construction.

    `staff` widens it. A hotelier asking "how did last month go" and getting
    silence after two tools ran is the case with the clearest right answer, and
    the audience is a person who works here rather than a guest being sold to.
    """
    verified = [
        o for o in (outcomes or [])
        if getattr(o, "ok", False) and getattr(o, "output", None) is not None
    ]
    if not verified:
        return None

    said: list[str] = []
    for outcome in verified:
        result_type = getattr(outcome, "result_type", "none")
        entry = _SAYABLE.get(result_type)
        if not entry:
            continue
        field, template = entry
        value = (outcome.output or {}).get(field) if isinstance(outcome.output, dict) else None
        if value:
            said.append(template.format(value))

    if said:
        # Deduplicated in order: two quotes on one turn is the model comparing
        # options, and reading the same sentence twice reads as a stutter.
        seen, unique = set(), []
        for sentence in said:
            if sentence not in seen:
                seen.add(sentence)
                unique.append(sentence)
        return " ".join(unique[:3])

    if staff:
        # Nothing quotable, but a person who works here is owed the fact that
        # the work happened.
        #
        # **In human nouns, not tool names.** The first version of this said
        # "That's done — get_property", and `guardrails.check_reply` refused it
        # for leaking an internal identifier — on its first outing, before any
        # of it reached a person. A tool name is not a thing a hotelier asked
        # about, and §33 asks for zero identifier leakage from *any* path, not
        # only the model's.
        made = [
            _NOUNS[getattr(o, "result_type", "none")]
            for o in verified
            if getattr(o, "result_type", "none") in _NOUNS
        ]
        if made:
            unique = list(dict.fromkeys(made))
            return ("That's done — I've put together " + _join(unique)
                    + ". It's on this conversation; ask me to write it out and I "
                      "will.")
    return None


# Result types a person can be told about in their own words. A pure read has no
# entry, because "I read something" is not an answer to anything — a turn whose
# tools only read and whose model then said nothing has genuinely produced no
# answer, and a handoff is the honest end of it.
_NOUNS = {
    "recommendation": "a recommendation",
    "task": "a job for the team",
    "reservation": "a booking",
    "quote": "a quote",
    "folio_entry": "a charge on the bill",
    "payment_link": "a payment link",
    "campaign": "a campaign",
    "escalation": "a note for the team",
}


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
