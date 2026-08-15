"""
Billing — the Python side of one SQL function.

Everything that costs money goes through `charge_usage` in the database, and
this module is the only thing that calls it. That is deliberate: the price
lookup, the cost lookup, the bucket walk and the ledger append have to happen
inside one transaction holding one row lock, and no arrangement of Python
statements can offer that.

So this file is thin on purpose. What it does add is the two failure policies,
which are opposite and both intentional:

    charge()    fails CLOSED and swallows.   A metering error must never take
                down a guest conversation. It is logged loudly; the gap is
                found by reconciling against a provider invoice, which is what
                `reconcile_provider_invoice` exists for.

    allowed()   fails OPEN.                  A billing outage must never take a
                hotel's AI offline. If we cannot read the wallet we let the
                turn through and eat the cost — the alternative is that a
                Supabase hiccup looks to a hotelier exactly like being cut off
                for non-payment.

Neither is the safe-looking default, and both were chosen after asking what the
hotel experiences when the thing goes wrong.

The PMS never imports this module. Front desk, reservations, inventory, rates,
folios, invoicing, check-in/out, housekeeping and tasks are the subscription;
only AI and messaging degrade.
"""
import logging
from dataclasses import dataclass, field
from typing import Any

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


# ── the taxonomy, in one place ───────────────────────────────────────────────
#
# An agent asks to be charged for what it *is*, not for what it cost. Which
# model answered a guest is a routing decision the router may change tomorrow;
# what the hotel bought is "a guest conversation", and that is the row on the
# rate card. Collapsing the two would mean a cheaper model silently repricing
# every hotel's bill.

USAGE_BY_AGENT = {
    "guest":     "ai.guest_conversation",
    "frontdesk": "ai.frontdesk_assist",
    "owner":     "ai.owner_copilot",
    "sales":     "ai.sales_assist",
    "marketing": "ai.content_generation",
}

# The one agent whose price depends on how hard the turn was.
#
# `ai.negotiation` is 20 credits and its rate-card description says exactly what
# it is for: "one turn on the reasoning tier where a price is being argued".
# Mapping every sales turn to it would have charged a hotel 20 credits for "do
# you have a room on Friday" — two and a half times what the guest agent charges
# for the same sentence, on the agent most likely to be asked it.
#
# So the tier decides, and the tier is the router's own decision recorded on
# `agent_runs.model_tier`. A hotel that queries its bill can join the charge back
# to the run and see why. No other agent varies: a front desk lookup and a front
# desk analysis are the same product to the hotel, and only this one has a
# separately priced hard case that was already on the card.
USAGE_BY_AGENT_TIER = {
    ("sales", "reasoning"): "ai.negotiation",
}

# Meta bills per conversation, by category, and tells us the category on the
# delivery callback. These are the four it can send.
USAGE_BY_WA_CATEGORY = {
    "service":        "whatsapp.service",
    "utility":        "whatsapp.utility",
    "marketing":      "whatsapp.marketing",
    "authentication": "whatsapp.authentication",
    "referral_conversion": "whatsapp.service",
}

# Message purposes that must send even at a zero balance. Withholding a
# confirmation for a room a guest has already paid for, to collect a credit
# debt, is not a product decision anyone should be able to make by accident.
CRITICAL_PURPOSES = {
    "booking_confirmation",
    "payment_confirmation",
    "cancellation",
    "checkin_link",
    # A guest who has been put through to a person needs to be told. That is an
    # operational message, not a marketing one.
    "human_handoff",
}


def usage_type_for_agent(agent_type: str | None, tier: str | None = None) -> str:
    """
    What one turn of this agent is sold as.

    `tier` is optional and usually absent: the pre-run credit gate consults this
    before anything has been routed, so it prices the ordinary case. That is the
    right way round — a gate that assumed the expensive type would pause an
    agent that had the credits for the turn it was about to take. The ledger,
    which runs after and knows the tier, records what actually happened.
    """
    base = USAGE_BY_AGENT.get(agent_type or "", "ai.guest_conversation")
    if tier:
        return USAGE_BY_AGENT_TIER.get((agent_type or "", tier), base)
    return base


def is_critical(purpose: str | None) -> bool:
    return (purpose or "") in CRITICAL_PURPOSES


@dataclass
class ChargeResult:
    usage_event_id: str | None = None
    credits_charged: int = 0
    balance_after: int | None = None
    cost_inr_micros: int = 0
    status: str = "unknown"
    duplicate: bool = False
    # Set when the charge never reached the database. The caller carries on:
    # see the module docstring.
    error: str | None = None

    @property
    def recorded(self) -> bool:
        return self.usage_event_id is not None


@dataclass
class Decision:
    """
    The answer to "may this run at all?".

    `allowed` is the only field a caller must respect. `tier` and `reason` are
    for what the hotel is told — "you are out of credits, this conversation is
    with the team" reads very differently from a silent non-answer.
    """

    allowed: bool = True
    tier: str = "unknown"
    reason: str = "ok"
    balance: int = 0
    available: int = 0
    pct_remaining: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


# ── account resolution ───────────────────────────────────────────────────────
#
# A property's billing account changes approximately never — it is set by a
# trigger when the property is created — so it is cached for the process
# lifetime. The charge path is on a guest's critical path and a round trip to
# resolve a constant is a round trip nobody needs.

_account_cache: dict[str, str] = {}


def _lookup_account(property_id: str) -> str | None:
    rows = (
        get_db().table("properties")
        .select("billing_account_id")
        .eq("id", property_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return (rows[0] or {}).get("billing_account_id") if rows else None


async def account_for_property(property_id: str) -> str | None:
    if not property_id:
        return None
    cached = _account_cache.get(property_id)
    if cached:
        return cached
    account = await run_db(_lookup_account, property_id)
    if account:
        _account_cache[property_id] = account
    return account


def forget_account(property_id: str) -> None:
    """For tests, and for the day a property is moved between accounts."""
    _account_cache.pop(property_id, None)


# ── charging ────────────────────────────────────────────────────────────────

def _charge(args: dict) -> dict:
    return get_db().rpc("charge_usage", args).execute().data or {}


async def charge(
    *,
    usage_type: str,
    units: float,
    idempotency_key: str,
    billing_account_id: str | None = None,
    property_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    dimension: dict | None = None,
    unit_detail: dict | None = None,
    agent_id: str | None = None,
    run_id: str | None = None,
    action_id: str | None = None,
    conversation_id: str | None = None,
    message_id: str | None = None,
    user_id: str | None = None,
    external_reference: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> ChargeResult:
    """
    Meter, price, cost and debit — one call, exactly once.

    `idempotency_key` is the whole safety argument and must be derived from the
    thing consumed, never from the clock: a run id, a message id, a Meta
    conversation id. A key invented at call time turns a retried webhook into a
    second charge.
    """
    account = billing_account_id
    if account is None and property_id:
        account = await account_for_property(property_id)

    context = {
        "property_id": property_id,
        "provider": provider,
        "model": model,
        "dimension": dimension or {},
        "unit_detail": unit_detail or {},
        "agent_id": agent_id,
        "run_id": run_id,
        "action_id": action_id,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "user_id": user_id,
        "external_reference": external_reference,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "metadata": metadata or {},
    }
    context = {k: v for k, v in context.items() if v not in (None, "")}

    args = {
        "p_billing_account": account,
        "p_usage_type": usage_type,
        "p_units": units,
        "p_idempotency_key": idempotency_key,
        "p_context": context,
    }

    try:
        data = await run_db(_charge, args)
    except Exception as exc:  # noqa: BLE001
        # Swallowed on purpose. See the module docstring: the alternative is a
        # guest conversation that dies because a wallet row was locked.
        logger.warning("could not charge %s (%s): %s", usage_type, idempotency_key, exc,
                       exc_info=True)
        return ChargeResult(status="error", error=f"{type(exc).__name__}: {exc}")

    if not isinstance(data, dict):
        return ChargeResult(status="error", error="charge_usage returned nothing")

    return ChargeResult(
        usage_event_id=data.get("usage_event_id"),
        credits_charged=int(data.get("credits_charged") or 0),
        balance_after=data.get("balance_after"),
        cost_inr_micros=int(data.get("cost_inr_micros") or 0),
        status=str(data.get("status") or "unknown"),
        duplicate=bool(data.get("duplicate")),
    )


# ── the pre-check ───────────────────────────────────────────────────────────

def _allowed(args: dict) -> dict:
    return get_db().rpc("usage_allowed", args).execute().data or {}


async def allowed(
    *,
    usage_type: str,
    billing_account_id: str | None = None,
    property_id: str | None = None,
    critical: bool = False,
) -> Decision:
    """
    May this run?

    Consulted *before* the work, never after. A turn that has already happened
    is charged regardless — the ledger records what was consumed, and refusing
    to record it to protect a balance would corrupt the meter rather than the
    balance.
    """
    account = billing_account_id
    if account is None and property_id:
        account = await account_for_property(property_id)
    if account is None:
        return Decision(allowed=True, reason="no_account", tier="unknown")

    try:
        data = await run_db(_allowed, {
            "p_billing_account": account,
            "p_usage_type": usage_type,
            "p_critical": critical,
        })
    except Exception as exc:  # noqa: BLE001
        # Fails open. A hotelier cannot tell "Supabase is having a minute" from
        # "you have been cut off", and only one of those is true.
        logger.warning("could not read the wallet for %s: %s", account, exc, exc_info=True)
        return Decision(allowed=True, reason="check_failed", tier="unknown")

    if not isinstance(data, dict):
        return Decision(allowed=True, reason="check_failed", tier="unknown")

    pct = data.get("pct_remaining")
    return Decision(
        allowed=bool(data.get("allowed", True)),
        tier=str(data.get("tier") or "unknown"),
        reason=str(data.get("reason") or "ok"),
        balance=int(data.get("balance") or 0),
        available=int(data.get("available") or 0),
        pct_remaining=float(pct) if pct is not None else None,
        raw=data,
    )


def _wallet_state(account: str) -> dict:
    return get_db().rpc("wallet_state", {"p_billing_account": account}).execute().data or {}


async def wallet_state(billing_account_id: str) -> dict:
    try:
        return await run_db(_wallet_state, billing_account_id) or {}
    except Exception:  # noqa: BLE001
        logger.warning("could not read wallet state for %s", billing_account_id, exc_info=True)
        return {}


# ── grants, reversals, holds ────────────────────────────────────────────────
#
# No customer-facing path reaches these yet. Self-service purchase arrives with
# the Razorpay work; until then a top-up is an internal action and grant() is
# how it is taken.

def _rpc(name: str, args: dict) -> Any:
    return get_db().rpc(name, args).execute().data


async def grant(
    *,
    billing_account_id: str,
    kind: str,
    credits: int,
    idempotency_key: str,
    expires_at: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    actor_type: str = "admin",
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    return await run_db(_rpc, "grant_credits", {
        "p_billing_account": billing_account_id,
        "p_kind": kind,
        "p_credits": credits,
        "p_idempotency_key": idempotency_key,
        "p_expires_at": expires_at,
        "p_source_type": source_type,
        "p_source_id": source_id,
        "p_actor_type": actor_type,
        "p_actor_id": actor_id,
        "p_metadata": metadata or {},
    }) or {}


async def reverse(*, usage_event_id: str, reason: str, idempotency_key: str) -> dict:
    return await run_db(_rpc, "reverse_usage_charge", {
        "p_usage_event_id": usage_event_id,
        "p_reason": reason,
        "p_idempotency_key": idempotency_key,
    }) or {}


async def reserve(
    *,
    billing_account_id: str,
    usage_type: str,
    credits: int,
    idempotency_key: str,
    property_id: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
) -> dict:
    """
    A reservation for work that must not fail halfway.

    Unlike charge(), this one does NOT swallow: a campaign that cannot reserve
    must not start, and returning a cheerful default here would be how four
    thousand messages go out against a wallet that could not pay for them.
    """
    return await run_db(_rpc, "reserve_credits", {
        "p_billing_account": billing_account_id,
        "p_usage_type": usage_type,
        "p_credits": credits,
        "p_idempotency_key": idempotency_key,
        "p_property_id": property_id,
        "p_reference_type": reference_type,
        "p_reference_id": reference_id,
    }) or {}


async def settle(*, hold_id: str, status: str = "settled") -> dict:
    return await run_db(_rpc, "settle_credit_hold", {
        "p_hold_id": hold_id, "p_status": status,
    }) or {}
