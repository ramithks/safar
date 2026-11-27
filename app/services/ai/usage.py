"""
Usage — what a turn consumed, and what it cost.

**This module used to measure and not charge.** That was right while there was
no rate card: "instrument now, price later, and do not build a fake billing
implementation to make a meter look real." The prices exist now, so every
countable thing goes through `billing.charge()` — which resolves the credit
rate and the provider cost independently, appends the ledger and returns.

The one thing that did not change is the idempotency key. It is still computed
from the run or the message and never from the clock, because that is the part
that could not have been retro-fitted: a retried webhook that produced two rows
before pricing would have produced two charges the day pricing arrived, and
back-dated.

    agent_run    ──► ai.guest_conversation │ ai.frontdesk_assist │ …
    agent_action ──► ai.tool_read / tool_write / tool_quote / tool_booking
    outbound msg ──► whatsapp.message_sent / whatsapp.template_sent

The per-tool rows stay non-billable. They are the operator's cost model, not
the hotel's bill: a guest asking one question that takes four lookups bought
one conversation, not four.

Failures here are logged and swallowed, by `billing.charge()` rather than by
this module. A meter that can take down a guest conversation is a worse bug
than a meter with a gap in it, and the gap is recoverable — that is what
`reconcile_provider_invoice` is for.
"""
import logging
from dataclasses import dataclass, field

from app.services import billing

logger = logging.getLogger(__name__)


@dataclass
class TurnUsage:
    """
    One run's consumption.

    **Two things, kept apart on purpose.**

    `tokens_*` and `model` are what the *provider* charged Safar. They are a
    cost, they are denominated in a vendor's units, and they change when Google
    changes a price list.

    `units` is what the *hotel* consumed: conversation turns, messages. That is
    what the rate card prices, and it is deliberately not derived from tokens —
    a hotel billed by the token would pay more for a chatty guest than for a
    demanding one, which is both unsellable and wrong.

    Both go to `charge_usage`, which prices the first from
    `provider_cost_rates` and the second from `usage_rate_card`, and neither
    from the other.
    """

    provider: str = ""
    model: str = ""
    # The capability tier the router chose: 'conversation' | 'reasoning' |
    # 'classify'. Carried because for one agent it changes the *price* — a sales
    # turn on the reasoning tier is a negotiation and is charged as one — and
    # deriving it back from a model id would break the day a tier moves.
    tier: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    # Reasoning tokens, where the provider reports them separately. Billed, and
    # not the same thing as output — the cost card prices them on their own unit.
    tokens_thought: int = 0
    # Every model this run touched. A turn that escalated from the workhorse to
    # the reasoning tier cost two different prices, and one `model` field would
    # hide the expensive half.
    models: list[str] = field(default_factory=list)

    # usage_type -> count. The billable turn is added by the runtime; the
    # per-tool counts are for the operator's own cost model.
    units: dict[str, int] = field(default_factory=dict)

    def add_tokens(self, tokens_in: int, tokens_out: int, tokens_thought: int = 0) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.tokens_thought += tokens_thought

    def used_model(self, model: str) -> None:
        if model and model not in self.models:
            self.models.append(model)

    def count(self, usage_type: str, n: int = 1) -> None:
        self.units[usage_type] = self.units.get(usage_type, 0) + n


def idempotency_key(scope: str, usage_type: str, seq: int | None = None) -> str:
    """
    One key per countable thing.

    Keyed on the run (or the message), never on the clock, so a retry of the
    same turn reuses it. The seq disambiguates several billable actions inside
    one run without collapsing them into one event.
    """
    return f"{usage_type}:{scope}" + (f":{seq}" if seq is not None else "")


async def record_run(
    *,
    run_id: str,
    property_id: str,
    partner_id: str,
    agent_id: str | None,
    conversation_id: str | None,
    usage: TurnUsage,
    agent_type: str | None = None,
    action_ids: dict[str, list[str]] | None = None,
    latency_ms: int | None = None,
    channel: str | None = None,
) -> int:
    """
    Charge the events for one finished run.

    **Two different things, on purpose.** The turn is what the *provider* cost
    us and is recorded per run, carrying the tokens. The conversation is what
    the *hotel* bought and is charged once per thread per day, by channel. A
    guest who asks six questions is one conversation and six turns, and the two
    numbers answer two different people's questions.

    Plus one non-billable row per tool call: a guest asking one question that
    takes four lookups bought one conversation, not four.
    """
    turn_type = billing.usage_type_for_agent(agent_type, tier=usage.tier or None)

    unit_detail = {
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "tokens_thought": usage.tokens_thought,
    }

    result = await billing.charge(
        usage_type=turn_type,
        units=1,
        idempotency_key=idempotency_key(run_id, turn_type),
        property_id=property_id,
        provider=usage.provider or None,
        model=usage.model or None,
        unit_detail=unit_detail,
        agent_id=agent_id,
        run_id=run_id,
        conversation_id=conversation_id,
        actor_type="agent",
        actor_id=agent_id,
        metadata={
            "models": usage.models or ([usage.model] if usage.model else []),
            "latencyMs": latency_ms,
            "agentType": agent_type,
            "tier": usage.tier or None,
        },
    )
    written = 1 if result.recorded else 0

    # ── the conversation itself ─────────────────────────────────────────────
    #
    # Charged once per conversation per 24-hour window, whatever the turn above
    # cost. This is the line on the hotel's bill: "42 Instagram conversations".
    #
    # The key carries the conversation and the window and nothing else — not the
    # run, not the message, not the clock beyond the window index. That is the
    # whole idempotency argument: every turn in the thread computes the same
    # key, so the second message costs nothing, and so does a redelivered
    # webhook, a Celery retry, a duplicated inbound message and a guest
    # refreshing the page. `charge_usage` holds the unique index that makes the
    # second call a no-op rather than a race.
    conversation_type = billing.usage_type_for_channel(channel)
    if conversation_type and conversation_id:
        window = billing.conversation_window()
        conversation_result = await billing.charge(
            usage_type=conversation_type,
            units=1,
            idempotency_key=f"{conversation_type}:conv:{conversation_id}:{window}",
            property_id=property_id,
            agent_id=agent_id,
            run_id=run_id,
            conversation_id=conversation_id,
            actor_type="agent",
            actor_id=agent_id,
            metadata={"channel": channel, "window": window},
        )
        written += 1 if conversation_result.recorded else 0

    for usage_type, count in usage.units.items():
        # The runtime still counts 'ai.conversation_turn' for its own debug
        # line. The billable turn is the one above; counting it twice under two
        # names would be a duplicate charge wearing a different type.
        if usage_type in (turn_type, "ai.conversation_turn"):
            continue
        ids = (action_ids or {}).get(usage_type) or []
        tool_result = await billing.charge(
            usage_type=usage_type,
            units=count,
            idempotency_key=idempotency_key(run_id, usage_type),
            property_id=property_id,
            provider=usage.provider or None,
            model=usage.model or None,
            agent_id=agent_id,
            run_id=run_id,
            action_id=ids[0] if ids else None,
            conversation_id=conversation_id,
            actor_type="agent",
            actor_id=agent_id,
            metadata={"actionIds": ids} if len(ids) > 1 else None,
        )
        written += 1 if tool_result.recorded else 0

    return written


async def record_message(
    *,
    message: dict,
    template_name: str | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    action_id: str | None = None,
) -> int:
    """
    One outbound message.

    Priced at zero, and recorded anyway. Meta bills per *conversation*, not per
    message — `record_conversation` below is where the money is — so charging
    here as well would bill the same thing twice. The count still matters: "how
    many messages did we send inside that window" is a question the inbox, the
    operator and any future per-message channel all need answered.

    Templates are their own type because a hotel reading its bill needs to see
    the difference between "we replied inside the window" and "we sent a paid
    template".

    Keyed on the message id rather than on the send attempt, so the retry path
    in `messaging.service.send()` — which returns the already-sent row — cannot
    produce a second event.
    """
    usage_type = (
        f"{message['channel']}.template_sent" if template_name
        else f"{message['channel']}.message_sent"
    )
    result = await billing.charge(
        usage_type=usage_type,
        units=1,
        idempotency_key=idempotency_key(message["id"], usage_type),
        property_id=message["property_id"],
        agent_id=agent_id,
        run_id=run_id,
        action_id=action_id,
        conversation_id=message["conversation_id"],
        message_id=message["id"],
        external_reference=message.get("external_id"),
        metadata={"template": template_name} if template_name else None,
    )
    return 1 if result.recorded else 0


async def record_conversation(
    *,
    property_id: str,
    channel: str,
    category: str,
    external_conversation_id: str,
    conversation_id: str | None = None,
    message_id: str | None = None,
    country: str | None = None,
    pricing_model: str | None = None,
    usage_type: str | None = None,
) -> int:
    """
    One WhatsApp conversation window, as Meta bills it.

    **This is where the messaging money is.** Meta opens a 24-hour conversation
    on the first message and charges once for it by category, at a rate that
    differs by country. Metering per message instead would mean a hotel that
    sends six replies inside one window pays six times for something we were
    billed for once — and would leave the marketing/service spread invisible.

    Keyed on Meta's own conversation id, so every delivery callback inside the
    same window resolves to the same charge. Meta sends the conversation block
    on more than one status update; that is exactly the retry this key absorbs.
    """
    # A channel whose policy names its own conversation type says so directly;
    # only WhatsApp derives the type from a Meta category, because only WhatsApp
    # has one.
    usage_type = usage_type or billing.USAGE_BY_WA_CATEGORY.get(category or "")
    if usage_type is None:
        # A category Meta has added since. Recorded under the cheapest honest
        # type rather than dropped: an unmetered conversation is a hole in the
        # margin report, and a wrong-but-visible one gets noticed.
        logger.info("unknown %s conversation category %r", channel, category)
        usage_type = "whatsapp.service"

    dimension = {"country": country} if country else {}
    # `provider` is what the cost card resolves on. Instagram carries none: Meta
    # does not bill us for it, and naming a provider here would make the cost
    # side look for a rate that must never exist. The event then records
    # honestly that no rate matched, rather than a zero somebody could mistake
    # for a checked figure.
    provider = "meta" if channel == "whatsapp" else None

    result = await billing.charge(
        usage_type=usage_type,
        units=1,
        idempotency_key=f"{usage_type}:conv:{external_conversation_id}",
        property_id=property_id,
        provider=provider,
        dimension=dimension,
        conversation_id=conversation_id,
        message_id=message_id,
        external_reference=external_conversation_id,
        metadata={"pricingModel": pricing_model} if pricing_model else None,
    )
    return 1 if result.recorded else 0


def record(run_id: str, usage: TurnUsage) -> None:
    """The old logging hook, kept so `runtime` has one call for the debug line."""
    logger.debug(
        "agent run %s used %s/%s tokens=%d/%d units=%s",
        run_id, usage.provider, usage.model, usage.tokens_in, usage.tokens_out, usage.units,
    )
