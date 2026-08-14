"""
Which model, and how hard should it think.

The runtime asks for a **capability**, never for a model:

    router.select(agent_type="guest", task="guest_conversation")
    router.select(agent_type="guest", task="complex_negotiation")
    router.select(agent_type="owner", task="revenue_analysis")

That indirection is the point. A model id scattered across a runtime is a model
id that gets upgraded in four places and missed in a fifth, and the fifth is
found in production by a hotel. Here there is one table, and it is the only
place any model is named outside `app/config.py`.

**Three tiers, because guest traffic is not one workload.**

    conversation   gemini-3-flash        the workhorse: every guest turn
    reasoning      gemini-3.1-pro        the exception: negotiation, disputes,
                                         owner analysis
    classify       gemini-3.1-flash-lite routing, intent, language, spam

The default is the workhorse and escalation is deliberate. Calling the expensive
model on every message is the easy mistake, and it is expensive in the two ways
that matter — the invoice, and the seconds a guest spends watching "typing…".

**Escalation is about task complexity, not message length.** A guest writing
four paragraphs about their honeymoon still wants breakfast times. A guest
writing one line about a cancellation fee they dispute is a rate decision with
money on it. So escalation is keyed on the *task* the runtime has identified,
never on the size of the input.
"""
import logging
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

PROVIDER_VERTEX = "vertex_ai"

# Capability tiers. The names are what the rest of the system uses.
CONVERSATION = "conversation"
REASONING = "reasoning"
CLASSIFY = "classify"


@dataclass(frozen=True)
class ModelChoice:
    """What the runtime was given, and what the meter records."""

    provider: str
    model: str
    tier: str
    # Gemini's thinking budget in tokens. 0 disables extended reasoning
    # entirely, which is right for a classification call and wrong for a
    # negotiation. `None` means "let the model decide", which is the sane
    # default for a conversation.
    thinking_budget: int | None
    temperature: float
    max_output_tokens: int
    # Why this tier was chosen. Recorded on the run, so a slow or expensive turn
    # can be explained without re-deriving the decision.
    reason: str

    @property
    def timeout(self) -> float:
        return settings.hotel_model_timeout_seconds


# ── the table ────────────────────────────────────────────────────────────────
#
# Task → tier. Everything not named here falls to `conversation`, which is the
# safe default: the workhorse can do the job, just less well than the reasoning
# tier would. Defaulting the other way would make one forgotten task name
# expensive on every guest message.

_TASKS: dict[str, tuple[str, str]] = {
    # The bulk of the product.
    "guest_conversation": (CONVERSATION, "a guest turn on a messaging channel"),
    "guest_faq":          (CONVERSATION, "a question the hotel has written down"),
    "guest_request":      (CONVERSATION, "a request that becomes a ticket"),
    "reservation_lookup": (CONVERSATION, "reading the guest's own booking"),
    "availability":       (CONVERSATION, "an availability question"),
    "simple_quote":       (CONVERSATION, "a straightforward priced stay"),
    "booking":            (CONVERSATION, "a booking against an existing quote"),
    "handoff":            (CONVERSATION, "handing the conversation to a person"),

    # The front desk. Almost all of it is the workhorse: "who is arriving",
    # "is 102 clean", "check in TB-9F2K" are lookups and one action, however
    # long the sentence asking for them was. The reasoning tier is reached by
    # naming a harder task, not by measuring the input — a receptionist who
    # writes four lines about a group arrival still wants the arrivals list.
    "frontdesk_operations": (CONVERSATION, "a front desk lookup or action"),
    "frontdesk_summary":    (CONVERSATION, "the day's board, summarised"),

    # Worth the expensive model: money, ambiguity, or several moving parts.
    "complex_negotiation":   (REASONING, "a rate negotiation with money on it"),
    "cancellation_dispute":  (REASONING, "a disputed charge or cancellation fee"),
    "multi_reservation":     (REASONING, "a change spanning several bookings"),
    "policy_conflict":       (REASONING, "hotel policies that disagree"),
    "complex_request":       (REASONING, "a request with several dependent steps"),
    "revenue_analysis":      (REASONING, "an owner question about performance"),
    "operational_analysis":  (REASONING, "an unusual operational scenario"),
    "escalation":            (REASONING, "escalated after the workhorse failed"),

    # Never worth more than the cheapest model.
    "intent_classification": (CLASSIFY, "deciding what a message is about"),
    "language_detection":    (CLASSIFY, "which language to reply in"),
    "spam_detection":        (CLASSIFY, "whether this is a real guest"),
    "needs_tool":            (CLASSIFY, "whether a message needs a lookup at all"),
    "simple_extraction":     (CLASSIFY, "pulling dates or a name out of a sentence"),
    "routing":               (CLASSIFY, "which agent should answer"),
}

_TIERS: dict[str, dict] = {
    CONVERSATION: {
        "model": lambda: settings.hotel_model_conversation,
        # None, not 0: a guest turn choosing between twenty tools benefits from
        # some deliberation, and the model is better placed than we are to
        # decide how much for a given message.
        "thinking_budget": None,
        # Low but not zero. The hotel's voice comes from its own configuration;
        # what varies here is phrasing, and a deterministic agent reads like a
        # form letter after the third message.
        "temperature": 0.3,
        "max_output_tokens": 900,
    },
    REASONING: {
        "model": lambda: settings.hotel_model_reasoning,
        "thinking_budget": None,
        # Lower still. This tier is reached when something is disputed or
        # expensive, and that is not the moment for a creative answer.
        "temperature": 0.2,
        "max_output_tokens": 1600,
    },
    CLASSIFY: {
        "model": lambda: settings.hotel_model_classify,
        # Explicitly off. A classifier that thinks is a classifier that costs
        # what the conversation model costs, which defeats the point of it.
        "thinking_budget": 0,
        "temperature": 0.0,
        "max_output_tokens": 200,
    },
}

# Agents whose *whole job* is the expensive tier, whatever task is named. The
# Owner Copilot reads revenue and explains it; there is no cheap version of that
# question worth answering.
_ALWAYS_REASONING = frozenset({"owner"})


def select(
    *,
    agent_type: str = "guest",
    task: str = "guest_conversation",
    override_model: str | None = None,
    escalated: bool = False,
) -> ModelChoice:
    """
    Pick a model for one call.

    `override_model` is the per-property escape hatch: `agent_configs.model` may
    name a specific model, and a hotel that has been given one for a reason
    should keep it. It overrides the id and not the tier, so the thinking budget
    and token ceiling still come from the task.

    `escalated` marks a retry after the workhorse failed. It moves the tier up
    rather than repeating the same call, because a model that could not answer
    once will usually not answer the same way twice.
    """
    tier, why = _TASKS.get(task, (CONVERSATION, "an unrecognised task, treated as a guest turn"))

    if escalated and tier == CONVERSATION:
        tier, why = REASONING, "escalated after the conversation model failed"
    if agent_type in _ALWAYS_REASONING and tier != CLASSIFY:
        tier, why = REASONING, f"the {agent_type} agent reasons about money by definition"

    spec = _TIERS[tier]
    return ModelChoice(
        provider=PROVIDER_VERTEX,
        model=override_model or spec["model"](),
        tier=tier,
        thinking_budget=spec["thinking_budget"],
        temperature=spec["temperature"],
        max_output_tokens=spec["max_output_tokens"],
        reason=why,
    )


def known_tasks() -> tuple[str, ...]:
    """For the tests, and for anything that wants to show an operator the list."""
    return tuple(sorted(_TASKS))


def tier_of(task: str) -> str:
    return _TASKS.get(task, (CONVERSATION, ""))[0]
