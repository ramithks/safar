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
import re
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
    """
    One rung of the ladder, for the record.

    **`error_class` and `latency_ms` were not here, and §17 needs both.** The
    row said a fallback happened and which model it landed on; it did not say
    what the provider had actually done or how long the failed attempt cost.
    Those are the two questions an operator asks about a fallback — *is this
    the provider or is this us*, and *what is this costing the guest in
    latency* — and neither could be answered without a log search on a run id
    nobody has.

    `error_class` is the provider's own exception type, not our classification
    of it: `classify` already collapses a hundred provider errors into a
    handful of behaviours, which is right for deciding what to do next and
    lossy for deciding whether to raise a ticket with Google.
    """

    failure: str
    model: str
    method: str          # 'retry' · 'fallback' · 'deterministic' · 'handoff'
    error_class: str | None = None
    latency_ms: int | None = None

    def as_dict(self) -> dict:
        record = {"failure": self.failure, "model": self.model,
                  "method": self.method}
        if self.error_class:
            record["error_class"] = self.error_class
        if self.latency_ms is not None:
            record["latency_ms"] = self.latency_ms
        return record


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


# ── a promise of a person has to be true ────────────────────────────────────
#
# **The invariant §12 asks for**: do not tell a guest a human will contact them
# unless a real handoff was actually created.
#
# Two ways it was breakable. The runtime's own handoff sentence — *"I've passed
# this to the hotel team and someone will be with you shortly"* — is composed
# before `_ensure_human` runs, and `_ensure_human` swallowed its own failure. A
# database blip on that one UPDATE left the conversation on `ai`, the sweep
# looks at the handler, and the guest waited for somebody nobody had told.
#
# And the model can write the promise itself. A live evaluation turn produced
# *"one of our team members will get back to you shortly"* with no tool called
# and `handoff` false — the words of a handoff without the handoff.
#
# The fix is to make the promise true rather than to refuse the reply. A guest
# who has been told a person is coming should get one; taking the sentence away
# and leaving them with nothing is the worse of the two repairs.
_PROMISED_A_PERSON = re.compile(
    r"(?xi)"
    r"("
    # English
    r"  (?:someone|somebody|a\s+(?:member|colleague)|the\s+(?:team|hotel|staff)|"
    r"     our\s+team|a\s+human|a\s+person|the\s+manager)"
    r"  [^.!?]{0,40}?"
    r"  (?:will|going\s+to|shall)\s+(?:be\s+)?"
    r"  (?:with|contact|call|reach|get\s+back|follow\s+up|assist|help)"
    r"| (?:passed|passing|handed|handing|transferr?(?:ed|ing)|escalat(?:ed|ing))"
    r"  \s+(?:this|it|you|your\s+\w+)?\s*(?:on\s+)?to\s+"
    r"  (?:the\s+)?(?:team|hotel|staff|manager|someone|a\s+colleague)"
    r"| (?:connecting|putting)\s+you\s+(?:through|in\s+touch)"
    # "let me get somebody from the hotel to look at it with you" — the
    # sentence several tools already hand back when they refuse. An offer to
    # fetch a person is a promise of one.
    r"| let\s+me\s+(?:get|bring|ask|find)\s+"
    r"  (?:somebody|someone|a\s+colleague|the\s+team|the\s+manager|"
    r"     the\s+hotel|hotel\s+team)"
    # Hinglish
    r"| team\s+(?:ko|se)\s+(?:bhej|bata|bula|forward|connect)"
    r"| (?:koi|kisi|hamara|humara)\s+\w{0,12}\s*(?:aapse|aap\s*se)\s+"
    r"  (?:baat|contact|sampark)"
    r"| team\s+(?:aapse|aap\s*se)\s+(?:baat|contact|sampark)"
    r"| main\s+team\s+ko\s+(?:bula|bhej|bata)"
    r")"
)


# **"I will share it with you as soon as it is ready", and then a handoff.**
#
# `promises_a_person` covers a reply that says a human is coming. This is the
# same failure one step over: a reply that promises a *thing* is coming — a
# link, a price, a confirmation — on a turn that then hands the conversation to
# somebody. The guest is left waiting for something nobody has undertaken to
# send, and unlike the person case there is not even a queue entry that names
# what they are waiting for.
#
# Found on turn 8 of the first-guest journey, on the second of three runs:
# "I am checking on the payment link for you right now. I will share it with
# you as soon as it is ready." The turn escalated, the promise stood, and the
# guest's next three messages got the handoff sentence.
#
# Deliberately narrow — a *future-tense first-person promise*. "Here is the
# link" is not one; "I'll send the link" is. The check is only ever consulted
# on a turn that is already handing off, so a promise the agent goes on to keep
# in the same turn never reaches it.
_PROMISED_AN_ACTION = re.compile(
    r"(?xi)"
    r"(?:"
    r"  (?:i|we)\s*(?:'ll|\s+will|\s+shall|\s+am\s+going\s+to|\s+are\s+going\s+to)"
    r"  [^.!?]{0,60}?"
    r"  (?:send|share|get|give|provide|create|generate|confirm|check|arrange|"
    r"     update|revert|come\s+back|let\s+you\s+know|update\s+you)"
    r"| (?:main|hum)\s+[^.!?]{0,40}?"
    r"  (?:bhej|bhejta|bhejti|bhejunga|bhej\s+dunga|de\s+dunga|kar\s+dunga|"
    r"     bata\s+dunga|bata\s+dungi|check\s+kar)"
    r"| (?:sharing|sending|generating|creating|checking)\s+(?:it|this|that|the)"
    r"  [^.!?]{0,30}?(?:shortly|soon|right\s+away|in\s+a\s+moment|now)"
    # "Let me check and get back to you" — an undertaking without
    # a first-person future, and one of the commonest ways to make one.
    r"| let\s+me\s+[^.!?]{0,40}?(?:check|find\s+out|confirm|look|see)"
    r"| (?:shortly|soon|in\s+a\s+moment|right\s+away)\s*[.!]?\s*$"
    r")"
)


def promises_an_action(reply: str | None) -> bool:
    """Does this reply undertake to do something later?"""
    return bool(_PROMISED_AN_ACTION.search(reply or ""))


def promises_a_person(reply: str | None) -> bool:
    """Does this reply tell the guest that somebody will come?"""
    return bool(_PROMISED_A_PERSON.search(reply or ""))
