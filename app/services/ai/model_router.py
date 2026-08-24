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

**Four tiers, because guest traffic is not one workload — and "conversation"
was quietly doing two jobs.**

    classify       gemini-3.5-flash-lite routing, intent, language, spam
    fast           gemini-3.5-flash      FAQs, lookups, simple CRUD: the volume
    conversation   gemini-3.6-flash      the agent brain: tools, operations,
                                         money, multi-step work
    deep           gemini-3.1-pro-preview escalation only, after the brain failed

The split that is new is `fast`. A guest asking for the wifi password and a
guest disputing a cancellation fee were both "the workhorse", which meant the
cheap question paid the expensive model's bill on every message of the day. The
brain keeps the work that needs a brain; the lookups go to a model that answers
them in 1.8s from our own region.

`deep` is the other end of the same idea. It is a preview model and it is slow,
so it is never a default and never an automatic retry — it is reached when the
brain has already failed at something expensive, and by nothing else.

The default is the brain and escalation is deliberate. Calling the expensive
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
from app.services import model_catalog

logger = logging.getLogger(__name__)

PROVIDER_VERTEX = "vertex_ai"

# Capability tiers. The names are what the rest of the system uses.
CONVERSATION = "conversation"
REASONING = "reasoning"
CLASSIFY = "classify"
FAST = "fast"
DEEP = "deep"


def _model_for(tier: str) -> str:
    """
    The configured model id for a tier, honouring the rollback switch.

    `hotel_models_v3=false` puts every tier back on the 2.5 model that was in
    production before the upgrade. One variable, no deploy — which is what makes
    3.x the default safe rather than brave. See `app/config.py`.
    """
    v3, stable = _TIER_SETTINGS[tier]
    return getattr(settings, v3 if settings.hotel_models_v3 else stable)


_TIER_SETTINGS: dict[str, tuple[str, str]] = {
    CLASSIFY:     ("hotel_model_classify",     "hotel_model_classify_stable"),
    FAST:         ("hotel_model_fast",         "hotel_model_fast_stable"),
    CONVERSATION: ("hotel_model_conversation", "hotel_model_conversation_stable"),
    REASONING:    ("hotel_model_reasoning",    "hotel_model_reasoning_stable"),
    DEEP:         ("hotel_model_deep",         "hotel_model_deep_stable"),
}


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
        """
        How long one model *step* may take.

        Per tier, not one number for everything. A classification is a
        sub-second call on the cheapest model and waiting forty-five seconds
        for one is waiting for a call that is never coming back; a reasoning
        turn on a disputed cancellation fee legitimately takes longer than a
        greeting does.
        """
        return _STEP_TIMEOUTS.get(self.tier, settings.hotel_model_timeout_seconds)

    @property
    def budget(self) -> float:
        """
        How long the **whole turn** may take, tool calls included.

        The thing that was missing, and the reason a real guest turn took 107
        seconds. `hotel_model_timeout_seconds` is per request; a turn may make
        `agent_max_tool_turns` of them, so the worst case was six times the
        number anybody had looked at, plus the tools in between. Nothing
        anywhere held a wall clock against the turn as a whole.

        A guest on Instagram who has waited two minutes has gone. So the runtime
        stops asking for another tool round once this is spent and lets the
        model answer with what it has — which is a slower reply, not a lost one.
        """
        return _TURN_BUDGETS.get(self.tier, settings.hotel_turn_budget_seconds)

    @property
    def fallbacks(self) -> list[str]:
        """
        Models to try, in order, if this one cannot be reached at all.

        Not the escalation ladder — the opposite direction. Escalation is "that
        answer was not good enough, think harder"; this is "that model is not
        answering, get the guest a reply". The chain is
        `3.6-flash → 3.5-flash → 2.5-flash` and it ends on the model that has
        been serving production all along, which is the property that matters:
        every degradation path terminates somewhere known-good rather than
        somewhere new.

        Excludes the model already chosen, so the caller can iterate it directly
        after its first failure. Never includes a model switched off in the
        catalogue, which is what stops a fallback from wandering into
        `gemini-3.7-flash`.
        """
        return model_catalog.fallback_chain(self.model, include_self=False)


# ── how long each tier is allowed ───────────────────────────────────────────
#
# Two clocks, and they answer different questions.
#
# `timeout` is one HTTP request to Vertex. `budget` is the guest sitting on
# Instagram watching "typing…", and covers every step of the turn plus the tools
# between them. Before this there was only the first, one value for everything,
# and the second did not exist — which is how a turn reached 107 seconds without
# anything being technically wrong.
#
# The numbers come from the measured distribution rather than from taste. Over
# 1,000 real runs: a turn calling no tools averages 6.0 s, one tool 9.9 s, two
# 13.1 s, three 20.7 s, four 21.0 s. So a 40-second budget on the conversation
# tier is roughly twice the slowest ordinary turn — generous enough that nothing
# real is cut off, tight enough that a guest is never left for two minutes.
#
# The reasoning tier keeps room, because the turns that reach it — a disputed
# cancellation fee, a rate negotiation — are the ones where thinking longer is
# the point and where a wrong fast answer costs money.
_STEP_TIMEOUTS: dict[str, float] = {
    CLASSIFY: 10.0,
    # Tighter than the brain on purpose: this tier exists for questions that come
    # back in under two seconds, and one that has not answered in twenty is not
    # a slow lookup, it is a call that is not coming back.
    FAST: 20.0,
    CONVERSATION: 30.0,
    REASONING: 60.0,
    # The slowest measured of the lot (~3.8s empty, and it is reached for the
    # hardest questions), so it gets the most room. It is also the rarest.
    DEEP: 75.0,
}

_TURN_BUDGETS: dict[str, float] = {
    CLASSIFY: 12.0,
    FAST: 25.0,
    CONVERSATION: 40.0,
    REASONING: 90.0,
    DEEP: 110.0,
}


# ── the table ────────────────────────────────────────────────────────────────
#
# Task → tier. Everything not named here falls to `conversation`, which is the
# safe default: the workhorse can do the job, just less well than the reasoning
# tier would. Defaulting the other way would make one forgotten task name
# expensive on every guest message.

_TASKS: dict[str, tuple[str, str]] = {
    # The bulk of the product. A guest turn is the brain's job — it is the one
    # that has to choose between twenty tools and notice when the answer needs a
    # second one — but the named lookups underneath it are not, and they are
    # most of the traffic.
    "guest_conversation": (CONVERSATION, "a guest turn on a messaging channel"),
    "guest_request":      (CONVERSATION, "a request that becomes a ticket"),
    "booking":            (CONVERSATION, "a booking against an existing quote"),

    # Named, cheap, and high-volume: one lookup and a sentence about it. These
    # were the workhorse's and did not need to be — a written-down FAQ answered
    # by the brain is the same answer for several times the cost.
    "guest_faq":          (FAST, "a question the hotel has written down"),
    "reservation_lookup": (FAST, "reading the guest's own booking"),
    "availability":       (FAST, "an availability question"),
    "simple_quote":       (FAST, "a straightforward priced stay"),
    "payment_status":     (FAST, "whether a payment landed or a booking is confirmed"),
    "handoff":            (FAST, "handing the conversation to a person"),

    # The front desk. Almost all of it is a lookup and one action: "who is
    # arriving", "is 102 clean", "check in TB-9F2K", however long the sentence
    # asking for them was. The brain is reached by naming a harder task, not by
    # measuring the input — a receptionist who writes four lines about a group
    # arrival still wants the arrivals list.
    #
    # `frontdesk_operations` stays on the brain because it *acts*: a check-in, a
    # room move, a folio charge. `frontdesk_summary` only reads.
    "frontdesk_operations": (CONVERSATION, "a front desk lookup or action"),
    "frontdesk_summary":    (FAST, "the day's board, summarised"),

    # Marketing. Planning a campaign and writing its copy are both the
    # workhorse's job: the audience arithmetic happens in Postgres and the copy
    # is three sentences about a hotel. Only an analysis of *why* a campaign
    # performed reaches the reasoning tier, and that is named separately.
    "marketing_planning":   (CONVERSATION, "proposing or drafting a campaign"),

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

# Per tier: the model, and what to do if the catalogue has no opinion on how
# hard it should think.
#
# **Thinking is decided by the model, not by the tier, and the tier is only the
# fallback.** Gemini 3.x thinks by default and bills for it — measured, 483
# tokens against 2.5-flash's 161 on the same trivial extraction — so leaving it
# to the model's default would have tripled the cost of the highest-volume paths
# the moment the ids changed. `model_catalog` declares a budget per model and
# `select()` reads it, which is what keeps that decision in one table instead of
# in every tier that happens to know a model is expensive.
_TIERS: dict[str, dict] = {
    CLASSIFY: {
        # Explicitly off. A classifier that thinks is a classifier that costs
        # what the conversation model costs, which defeats the point of it.
        "thinking_budget": 0,
        "temperature": 0.0,
        "max_output_tokens": 200,
    },
    FAST: {
        # Also off, and for the same reason at a hundred times the volume: this
        # tier answers questions the hotel has already written down.
        "thinking_budget": 0,
        "temperature": 0.3,
        "max_output_tokens": 700,
    },
    CONVERSATION: {
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
        "thinking_budget": None,
        # Lower still. This tier is reached when something is disputed or
        # expensive, and that is not the moment for a creative answer.
        "temperature": 0.2,
        "max_output_tokens": 1600,
    },
    DEEP: {
        "thinking_budget": None,
        "temperature": 0.2,
        "max_output_tokens": 2400,
    },
}

# Agents whose *whole job* is the expensive tier, whatever task is named. The
# Owner Copilot reads revenue and explains it; there is no cheap version of that
# question worth answering.
_ALWAYS_REASONING = frozenset({"owner"})

# One rung at a time, and `classify` does not climb at all.
#
# Retrying "which language is this" on a better model is not a better answer, it
# is the same answer for more money — so a failed classification stays cheap and
# the runtime falls back to the agent's default task instead.
#
# `deep` is the top and has nowhere to go. That is deliberate: it is a preview
# model, and a ladder whose last rung is a preview dependency is a ladder that
# ends in an outage nobody chose.
_ESCALATES_TO: dict[str, str] = {
    FAST: CONVERSATION,
    CONVERSATION: REASONING,
    REASONING: DEEP,
}


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
    once will usually not answer the same way twice. The ladder is
    `fast → conversation → reasoning → deep`, one rung at a time: `deep` is a
    preview model and is reached only from a tier that was already expensive,
    never as the automatic retry for any failure that happens to occur.
    """
    tier, why = _TASKS.get(task, (CONVERSATION, "an unrecognised task, treated as a guest turn"))

    if escalated:
        climbed = _ESCALATES_TO.get(tier)
        if climbed:
            why = f"escalated after the {tier} model failed"
            tier = climbed
    if agent_type in _ALWAYS_REASONING and tier not in (CLASSIFY, DEEP):
        tier, why = REASONING, f"the {agent_type} agent reasons about money by definition"

    spec = _TIERS[tier]
    model = override_model or _model_for(tier)

    # **A model switched off in the catalogue never reaches a guest.**
    # `gemini-3.7-flash` is a real, GA, callable model that answered 8 of 8
    # concurrent probes with RESOURCE_EXHAUSTED. Configuring it would not fail
    # loudly — it would degrade every turn into a retry ladder. So a
    # non-production model is refused here and the tier falls to its successor.
    if not model_catalog.is_production(model):
        chain = model_catalog.fallback_chain(model, include_self=False)
        if chain:
            logger.warning(
                "[model-router] %s is not cleared for production — using %s",
                model, chain[0],
            )
            model, why = chain[0], f"{why} ({model} is not cleared for production)"

    return ModelChoice(
        provider=PROVIDER_VERTEX,
        model=model,
        tier=tier,
        # The catalogue decides, the tier is the fallback. See `_TIERS`.
        thinking_budget=model_catalog.thinking_budget_for(
            model, default=spec["thinking_budget"]
        ),
        temperature=spec["temperature"],
        max_output_tokens=spec["max_output_tokens"],
        reason=why,
    )


def model_for_tier(tier: str) -> str:
    """
    The model a tier currently resolves to, honouring the rollback switch.

    For the health probes and for anything showing an operator what is
    configured. `select()` is still the only way to route a *request* — this
    answers "what is wired up", not "what should serve this".
    """
    return _model_for(tier)


def tiers() -> tuple[str, ...]:
    """Every tier, cheapest first. The order is the escalation ladder."""
    return (CLASSIFY, FAST, CONVERSATION, REASONING, DEEP)


def known_tasks() -> tuple[str, ...]:
    """For the tests, and for anything that wants to show an operator the list."""
    return tuple(sorted(_TASKS))


def tier_of(task: str) -> str:
    return _TASKS.get(task, (CONVERSATION, ""))[0]
