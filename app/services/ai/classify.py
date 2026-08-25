"""
What is this message about?

The runtime has always routed by *capability* — `model_router.select(task=…)` —
and until now the capability was a constant. `pipeline.process_conversation`
never passed one at all, so every guest turn in production was routed as
`guest_conversation` and every reasoning-tier task in the router's table was
unreachable except by a caller that hardcoded it. The escalation ladder existed
and nothing ever climbed it.

This module is the step that decides. One cheap model call, before the run:

    message ──► classify ──► task ──► model_router ──► the model that answers

**It cannot do anything.** No tools are passed, so there is no tool call it
could make; the property id is never sent; the return value is checked against a
closed list and anything unrecognised becomes the agent's default task. The
worst outcome of a fully compromised classifier is that a turn runs on the wrong
tier — it cannot read a booking, cannot spend money, and cannot widen a
permission, because it is not on the path where any of those are decided. The
server-side gates are all downstream and all unchanged.

**The candidate list is per agent, not global.** A guest agent must not be able
to route itself to `revenue_analysis`; a front desk agent asking for the day's
board must not be routed to `guest_faq`. So each agent type gets the subset of
capabilities it could actually serve, and the model chooses within it. That is
also what keeps the prompt short enough to be cheap.

**Length is never an input.** The classifier is shown the last few turns and the
message, and asked what the guest is trying to do. A four-paragraph message
about a honeymoon is still a breakfast question; a one-line "why am I being
charged ₹4,000 to cancel" is a rate dispute. The router's own docstring has said
this since slice 3 and this is the first thing that could have violated it.

**Failure is not an error.** Vertex being slow, a malformed answer, a task name
from a future version — every one of them resolves to the agent's default task
and the turn proceeds. A classifier that can fail a guest conversation is a
worse feature than no classifier, and this one is an optimisation dressed as a
routing decision: getting it wrong costs tokens, not correctness.
"""
import logging
from dataclasses import dataclass

from app.services.ai import model_router
from app.services.ai.providers import Provider, for_name

logger = logging.getLogger(__name__)


# ── what each agent may be routed to ─────────────────────────────────────────
#
# Drawn from `model_router._TASKS`, narrowed to what the agent has tools for.
# Offering a capability the agent cannot serve teaches the classifier to pick it
# and then leaves the model with a tier it does not need and a job it cannot do.
#
# The first entry of each tuple is the default: what an unclassifiable message
# becomes. Every default is a conversation-tier task on purpose — the fallback
# must never be the expensive one, or a broken classifier becomes an invoice.

_CANDIDATES: dict[str, tuple[str, ...]] = {
    "guest": (
        "guest_conversation",
        "guest_faq",
        "guest_request",
        "reservation_lookup",
        "availability",
        "simple_quote",
        "booking",
        "payment_status",
        "cancellation_dispute",
        "complex_negotiation",
        "policy_conflict",
        "multi_reservation",
    ),
    "sales": (
        "availability",
        "guest_faq",
        "simple_quote",
        "booking",
        "payment_status",
        "complex_negotiation",
        "policy_conflict",
        "multi_reservation",
    ),
    "frontdesk": (
        "frontdesk_operations",
        "frontdesk_summary",
        "multi_reservation",
        "operational_analysis",
    ),
    "owner": (
        # Routed for the audit trail rather than for the tier: `_ALWAYS_REASONING`
        # puts the owner agent on Pro whatever this returns.
        "revenue_analysis",
        "operational_analysis",
    ),
    "marketing": (
        # Campaign planning is a staff conversation about numbers. The reasoning
        # tier is reached by naming an analysis, not by a long message about a
        # festival weekend.
        "marketing_planning",
        "operational_analysis",
        "revenue_analysis",
    ),
}


# How long a reply may be and still be read as a label wearing punctuation.
# `intent_classification` is the longest real label at 23 characters; anything
# past this is prose, and prose that happens to contain a label is not a choice.
_MAX_LABEL_CHARS = 32


def candidates(agent_type: str | None) -> tuple[str, ...]:
    return _CANDIDATES.get(agent_type or "", _CANDIDATES["guest"])


def default_task(agent_type: str | None) -> str:
    return candidates(agent_type)[0]


# What "hello" is, per agent.
#
# **`default_task` is the wrong answer for a greeting on the Sales agent.** Its
# candidate list starts with `availability`, so a guest who opened a DM with
# "Hello" was routed as an availability query — and answered
#
#     Hello! To help you find a room, could you please tell me your desired
#     arrival and departure dates, and how many adults and children will be
#     staying?
#
# to somebody who had said one word. That is a real transcript from the first
# guest message a real hotel ever received on Instagram. It is not wrong so
# much as deaf: nobody opens with "hi" and expects an intake form.
_OPENING_TASK = {
    "guest": "guest_conversation",
    "sales": "guest_faq",
}

# A message carrying no routing signal at all.
#
# Deliberately a small closed list rather than a length cut-off or a fuzzy
# match. "No" is two characters and means something; "when?" is five and is a
# real question. What is safe to skip the classifier for is the set of things
# that are *only* an opening — and if one is missed, the cost is the classifier
# running as it does today, which is the behaviour we already have.
_OPENINGS = frozenset({
    "hi", "hii", "hiii", "hey", "heyy", "hello", "helo", "hlo", "yo",
    "hi there", "hey there", "hello there",
    "good morning", "good afternoon", "good evening", "gm", "ge",
    "namaste", "namaskar", "hola", "salaam", "assalamualaikum",
    "hi!", "hello!", "hey!", "hi.", "hello.", "hey.",
})


# A message that closes rather than asks.
#
# **"thanks" cost the full catalogue** — 38 tool schemas, about 7,600 input
# tokens, on a word that asks for nothing. It is not a greeting, so the opening
# shortcut never saw it, and it classifies as `guest_conversation`, which
# narrows nothing on purpose because it is also the fallback for anything the
# classifier could not read. It is among the commonest turns a hotel receives.
#
# **Gratitude and farewell only, and the narrowness is doing real work.** A bare
# "ok", "haan" or "theek hai" looks like the same kind of turn and is not: after
# *"shall I send you the payment link?"* it means yes, and a menu narrowed to
# CORE would have taken the payment tool off it. Nobody has ever said "thanks"
# to mean "book it", which is the whole test for what belongs on this list.
_CLOSINGS = frozenset({
    "thanks", "thank you", "thankyou", "thanks a lot", "thanks!", "thank you!",
    "thx", "tysm", "ty", "thanku", "thank u",
    "dhanyawad", "dhanyavaad", "shukriya", "shukriyaa",
    "bye", "goodbye", "bye bye", "alvida", "tata",
    "ok thanks", "okay thanks", "thanks bhai", "thank you so much",
    "धन्यवाद", "शुक्रिया",
})


def is_opening(message: str | None) -> bool:
    """Is this only a greeting — nothing to route on?"""
    cleaned = " ".join((message or "").strip().lower().split())
    return cleaned in _OPENINGS


def is_closing(message: str | None) -> bool:
    """
    Is this only thanks or goodbye — a turn that asks for nothing?

    Unlike an opening this has history behind it by definition, so it cannot
    lean on "the thread is empty" for safety. What makes it safe is the list:
    every entry is a word that cannot be an instruction to act.
    """
    cleaned = " ".join((message or "").strip().lower().split())
    return cleaned in _CLOSINGS


def opening_task(agent_type: str | None) -> str:
    """The conversational task, for a message that carries no other signal."""
    task = _OPENING_TASK.get(agent_type or "")
    if task and task in candidates(agent_type):
        return task
    return default_task(agent_type)


@dataclass(frozen=True)
class Classification:
    """What was decided, what it cost, and whether a model was involved at all."""

    task: str
    tier: str
    # 'model' when the classifier ran, 'default' when it was skipped, 'fallback'
    # when it ran and the answer was unusable. Recorded so a hotel asking why a
    # turn ran on Pro gets "the classifier said complex_negotiation" rather than
    # a shrug.
    source: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""

    # Was this only a greeting, on a thread with nothing before it?
    #
    # Carried rather than recomputed. `is_opening` is already consulted here,
    # under a condition — `not history` — that the runtime would have to
    # duplicate to ask the same question, and a predicate written twice is a
    # predicate that drifts. See services/ai/toolsets.for_task.
    opening: bool = False


class StaticClassifier:
    """
    Answers without calling anything.

    Not a stub: it is what a caller that already knows the capability should
    use, and what the deterministic E2E suites use so a scripted provider is
    never asked to be a classifier as well as a model.
    """

    def __init__(self, task: str | None = None):
        self.task = task

    async def classify(self, *, agent_type: str, message: str,
                       history: list[dict] | None = None) -> Classification:
        task = self.task or default_task(agent_type)
        return Classification(task=task, tier=model_router.tier_of(task), source="default")


class ModelClassifier:
    """
    One call on the classify tier, whichever model `model_router` resolves
    that to. Deliberately not named here: `app/config.py` and
    `model_router.py` are the only two files in this system allowed to know
    a model id, and `tests/test_ai_vertex.py` enforces it.

    Deliberately not given the run's provider. A caller scripting the model for
    a test is scripting the *answer*, not the routing, and consuming a script
    entry here would make every deterministic suite depend on the order of two
    unrelated decisions.
    """

    def __init__(self, provider: Provider | None = None):
        self._provider = provider

    def _llm(self) -> Provider:
        return self._provider or for_name("vertex_ai")

    async def classify(self, *, agent_type: str, message: str,
                       history: list[dict] | None = None) -> Classification:
        allowed = candidates(agent_type)
        fallback = default_task(agent_type)

        # "Hello" does not need a model to route it, and paid 2.3 seconds for
        # the privilege — measured in production, on the slowest and most
        # visible turn there is: the first thing a guest ever says. Skipping it
        # is worth doing for the latency alone and fixes the routing as well,
        # since the answer the model gave for a greeting was `availability`.
        #
        # Only for the *opening* message. Once a thread has history a bare "hi"
        # may well be an answer to something, and that is the classifier's job.
        if not history and is_opening(message):
            task = opening_task(agent_type)
            return Classification(task=task, tier=model_router.tier_of(task),
                                  source="default", opening=True)

        # A closing needs no model to route it either, and needs no tools to
        # answer. Reported as an `opening` because that is what the flag means
        # downstream — "this turn is asking for nothing" — and giving it a
        # second name would mean two ways to say one thing to `toolsets`.
        if is_closing(message):
            task = opening_task(agent_type)
            return Classification(task=task, tier=model_router.tier_of(task),
                                  source="default", opening=True)

        choice = model_router.select(
            agent_type=agent_type, task="intent_classification"
        )

        try:
            step = await self._llm().step(
                model=choice.model,
                messages=[
                    {"role": "system", "content": _prompt(allowed)},
                    {"role": "user", "content": _subject(message, history)},
                ],
                # Empty, and this is the security property: a classifier with no
                # tools cannot act, whatever a guest writes into it.
                tools=[],
                params={
                    "temperature": choice.temperature,
                    "max_output_tokens": choice.max_output_tokens,
                    "thinking_budget": choice.thinking_budget,
                },
            )
        except Exception:  # noqa: BLE001 — every failure is the same failure here
            logger.info("intent classification unavailable; routing %s as %s",
                        agent_type, fallback, exc_info=True)
            return Classification(task=fallback, tier=model_router.tier_of(fallback),
                                  source="fallback")

        task = _read(step.reply, allowed)
        if task is None:
            logger.info("classifier returned %r for %s; routing as %s",
                        (step.reply or "")[:60], agent_type, fallback)
            return Classification(
                task=fallback, tier=model_router.tier_of(fallback), source="fallback",
                tokens_in=step.tokens_in, tokens_out=step.tokens_out, model=choice.model,
            )

        return Classification(
            task=task, tier=model_router.tier_of(task), source="model",
            tokens_in=step.tokens_in, tokens_out=step.tokens_out, model=choice.model,
        )


# ── the prompt, and reading the answer ───────────────────────────────────────

def _prompt(allowed: tuple[str, ...]) -> str:
    """
    The capability list, with the router's own one-line reasons.

    Taken from `model_router._TASKS` rather than restated, so a capability whose
    meaning changes changes here too. Two descriptions of the same task is how a
    classifier ends up confidently picking the wrong one.
    """
    lines = []
    for task in allowed:
        _, why = model_router._TASKS.get(task, ("", ""))
        lines.append(f"{task} — {why}" if why else task)
    listed = "\n".join(lines)
    return (
        "You label hotel messages so the right model answers them. Read the "
        "message and reply with exactly one label from this list and nothing "
        "else — no punctuation, no explanation, no quotes.\n\n"
        f"{listed}\n\n"
        "Judge what the person is trying to achieve, not how much they wrote. A "
        "long message asking about breakfast is still a simple question. A short "
        "message disputing a charge is not.\n"
        "Several rooms on one enquiry is one sale, not several bookings and not "
        "a job for the staff: '5 kamre chahiye', 'we are 12 people' is somebody "
        "asking what it costs.\n"
        "When two labels could fit, choose the cheaper, more ordinary one. When "
        "nothing fits, reply with the first label in the list."
    )


def _subject(message: str, history: list[dict] | None) -> str:
    """
    The message, with a little of what came before it.

    Two prior turns, truncated. Context matters — "yes, book it" is a booking
    only because of what was said before — and more than two costs more than the
    accuracy is worth on a model this cheap.
    """
    parts: list[str] = []
    for turn in (history or [])[-2:]:
        who = "Them" if turn.get("role") == "user" else "Us"
        body = (turn.get("content") or "")[:300]
        if body:
            parts.append(f"{who}: {body}")
    parts.append(f"Them: {message[:1500]}")
    return "\n".join(parts)


def _read(reply: str | None, allowed: tuple[str, ...]) -> str | None:
    """
    Strict, then forgiving once, and only for something short.

    Exact match first. Failing that, one substring pass — because a model told
    to answer with one word occasionally answers `"booking."` or
    `` `availability` `` — but only when the reply is short enough to be a label
    with punctuation on it rather than a sentence.

    The length bound is the whole point. Without it, "this is definitely not a
    revenue_analysis or a booking" contains exactly one candidate and resolves
    to `booking` — a negation read as an answer. A sentence that mentions a
    label is not a model choosing it, and falling back costs a cheap turn on the
    workhorse while guessing costs the wrong tier and the wrong price.
    """
    if not reply:
        return None
    cleaned = reply.strip().strip("`\"'.").lower().replace(" ", "_").replace("-", "_")
    if cleaned in allowed:
        return cleaned
    if len(cleaned) > _MAX_LABEL_CHARS:
        return None
    matches = [t for t in allowed if t in cleaned]
    return matches[0] if len(matches) == 1 else None


# ── what the runtime calls ───────────────────────────────────────────────────

def for_provider(provider: Provider | None) -> "StaticClassifier | ModelClassifier":
    """
    The classifier that goes with a run's provider.

    An injected provider means the caller is scripting the model — a test, or
    the E2E suite. A scripted turn that then makes a live classification call is
    neither deterministic nor free, and the two failures would be indistinguish-
    able from each other in the output. So a scripted run classifies statically
    and says so on the run row; a real run classifies for real.
    """
    if provider is None:
        return ModelClassifier()
    return StaticClassifier()
