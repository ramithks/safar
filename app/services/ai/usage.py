"""
Usage — what a turn consumed.

**This slice measures. It does not charge.** There is no rate card, no wallet
and no ledger, and nothing here reads a balance or refuses anything. That is the
credit rule stated as code: a hotel with no credits still checks guests in,
still posts folios, still runs its night audit. Building a plausible-looking
debit against prices nobody has agreed would make the meter *look* finished and
make the first real invoice wrong.

What it does do is write a `usage_events` row for every countable thing, with an
idempotency key computed from the run rather than the clock. That is the part
that cannot be retro-fitted: a key invented at billing time cannot cover the
events that happened before it, and a retried webhook that produced two rows
today produces two charges the day pricing arrives — back-dated.

    agent_run   ──► ai.conversation_turn
    agent_action ──► ai.tool_read / tool_write / tool_quote / tool_booking
    outbound msg ──► whatsapp.message_sent / whatsapp.template_sent

Failures here are logged and swallowed. A meter that can take down a guest
conversation is a worse bug than a meter with a gap in it.
"""
import logging
from dataclasses import dataclass, field

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


@dataclass
class TurnUsage:
    """
    One run's consumption.

    **Two things, kept apart on purpose.**

    `tokens_*` and `model` are what the *provider* charged TravBuddy. They are a
    cost, they are denominated in a vendor's units, and they change when Google
    changes a price list.

    `units` is what the *hotel* consumed: conversation turns, messages. That is
    what a rate card will price, and it is deliberately not derived from tokens
    — a hotel billed by the token would pay more for a chatty guest than for a
    demanding one, which is both unsellable and wrong.

    Recording both now, separately, is what lets a later rate card compute
    provider_cost and credits_charged independently. Collapsing them into one
    number today would be a decision nobody has made yet, made permanently.
    """

    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    # Reasoning tokens, where the provider reports them separately. Billed, and
    # not the same thing as output — a rate card will treat them differently.
    tokens_thought: int = 0
    # Every model this run touched. A turn that escalated from the workhorse to
    # the reasoning tier cost two different prices, and one `model` field would
    # hide the expensive half.
    models: list[str] = field(default_factory=list)

    # usage_type -> count. `ai.conversation_turn` is the billable unit a hotel
    # understands; the per-tool counts are for the operator's own cost model.
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


def _insert(rows: list[dict]) -> int:
    db = get_db()
    written = 0
    for row in rows:
        try:
            # One at a time, on purpose. A batch insert where one key collides
            # loses the whole batch, and a collision is the *expected* case on a
            # replay rather than an error.
            db.table("usage_events").upsert(
                row, on_conflict="idempotency_key", ignore_duplicates=True
            ).execute()
            written += 1
        except Exception:  # noqa: BLE001
            logger.warning("could not record usage event %s", row.get("idempotency_key"),
                           exc_info=True)
    return written


async def record_run(
    *,
    run_id: str,
    property_id: str,
    partner_id: str,
    agent_id: str | None,
    conversation_id: str | None,
    usage: TurnUsage,
    action_ids: dict[str, list[str]] | None = None,
    latency_ms: int | None = None,
) -> int:
    """
    Write the events for one finished run.

    One `ai.conversation_turn` — the unit a hotel is billed in — plus one row
    per tool call for the operator's own cost model. The token counts ride on
    the turn, because that is where the provider cost actually lands.
    """
    # The customer-facing unit is one turn, whatever it cost us to produce.
    # The provider's side of the bargain rides in `metadata` — tokens, thinking
    # tokens, every model the run touched — so the two can be priced
    # independently when there is a rate card to price them with.
    rows: list[dict] = [{
        "property_id": property_id,
        "partner_id": partner_id,
        "usage_type": "ai.conversation_turn",
        "quantity": 1,
        "provider": usage.provider or None,
        "model": usage.model or None,
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "agent_id": agent_id,
        "run_id": run_id,
        "conversation_id": conversation_id,
        "metadata": {
            "providerCost": {
                "tokensIn": usage.tokens_in,
                "tokensOut": usage.tokens_out,
                "tokensThought": usage.tokens_thought,
                "models": usage.models or ([usage.model] if usage.model else []),
                "latencyMs": latency_ms,
            },
        },
        "idempotency_key": idempotency_key(run_id, "ai.conversation_turn"),
    }]

    for usage_type, count in usage.units.items():
        if usage_type == "ai.conversation_turn":
            continue
        ids = (action_ids or {}).get(usage_type) or []
        rows.append({
            "property_id": property_id,
            "partner_id": partner_id,
            "usage_type": usage_type,
            "quantity": count,
            "provider": usage.provider or None,
            "model": usage.model or None,
            "agent_id": agent_id,
            "run_id": run_id,
            "action_id": ids[0] if ids else None,
            "conversation_id": conversation_id,
            "metadata": {"actionIds": ids} if len(ids) > 1 else {},
            "idempotency_key": idempotency_key(run_id, usage_type),
        })

    return await run_db(_insert, rows)


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

    Templates are their own usage type because Meta prices them differently and
    because a hotel reading its bill needs to see the difference between "we
    replied inside the window" and "we sent a paid template".

    Keyed on the message id rather than on the send attempt, so the retry path
    in `messaging.service.send()` — which returns the already-sent row — cannot
    produce a second event.
    """
    usage_type = (
        f"{message['channel']}.template_sent" if template_name
        else f"{message['channel']}.message_sent"
    )
    row = {
        "property_id": message["property_id"],
        "partner_id": message["partner_id"],
        "usage_type": usage_type,
        "quantity": 1,
        "agent_id": agent_id,
        "run_id": run_id,
        "action_id": action_id,
        "conversation_id": message["conversation_id"],
        "message_id": message["id"],
        "external_ref": message.get("external_id"),
        "metadata": {"template": template_name} if template_name else {},
        "idempotency_key": idempotency_key(message["id"], usage_type),
    }
    return await run_db(_insert, [row])


def record(run_id: str, usage: TurnUsage) -> None:
    """The old logging hook, kept so `runtime` has one call for the debug line."""
    logger.debug(
        "agent run %s used %s/%s tokens=%d/%d units=%s",
        run_id, usage.provider, usage.model, usage.tokens_in, usage.tokens_out, usage.units,
    )
