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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    # A *turn*, not the copy. `ai.content_generation` is priced per item at 15
    # and is charged separately by `propose_campaign_message` — it is what
    # writing two hundred people's message costs, and it is not what asking
    # "how many repeat guests do I have" costs. Mapping every marketing turn to
    # it billed a question that generates nothing at nearly twice the guest
    # agent's rate.
    "marketing": "ai.marketing_assist",
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


# What a hotel is sold, by the place the guest is standing in.
#
# **A conversation, not a turn.** `ai.guest_conversation` is charged once per
# agent run, which is once per guest message — so a guest who asks six questions
# was six charges, and nobody could answer "how many conversations does 3,000
# credits buy?", which is the only question an owner asks about credits. These
# types are charged once per conversation per session window; the per-turn types
# stay, at zero, because Vertex still bills us per turn and the operator's cost
# model needs to see it.
#
# Absent from here on purpose: `internal`. A receptionist asking the front desk
# agent something is not a guest conversation and is priced per turn, which is
# what it is.
USAGE_BY_GUEST_CHANNEL = {
    "website":   "ai.conversation.website",
    "instagram": "ai.conversation.instagram",
    "whatsapp":  "ai.conversation.whatsapp",
}

# How long one conversation lasts for billing.
#
# Twenty-four hours, matching what Meta means by a conversation and what
# `record_conversation` already does for WhatsApp — a hotel reading one bill
# should not find two definitions of the word in it. A thread that goes quiet
# and wakes a week later is a new conversation, which is also how a hotelier
# would count it.
#
# It is a number here rather than a row because it is a definition, not a
# price: changing it changes what the word "conversation" means on an invoice
# already sent, and that is a decision with a migration attached, not a tuning
# knob.
CONVERSATION_WINDOW_SECONDS = 24 * 60 * 60


def usage_type_for_channel(channel: str | None) -> str | None:
    """
    The conversation type for a guest channel, or None for anything else.

    None is a real answer: a staff or internal thread has no conversation
    price, and inventing one would bill a hotel for its own receptionist.
    """
    return USAGE_BY_GUEST_CHANNEL.get((channel or "").lower())


def conversation_window(started_at: datetime | None = None) -> int:
    """
    Which window we are in, as an integer.

    Absolute rather than relative to the conversation's own start: the key has
    to be derivable from the clock alone by any process handling any retry,
    and a window that began when the conversation did would need the
    conversation row read first — on the path where the whole point is not
    reading anything twice.
    """
    now = started_at or datetime.now(timezone.utc)
    return int(now.timestamp()) // CONVERSATION_WINDOW_SECONDS


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
# trigger when the property is created — so it is cached. The charge path is on
# a guest's critical path and a round trip to resolve a near-constant is a round
# trip nobody needs.
#
# **"Approximately never" is not never, and the cache used to last the process
# lifetime.** That was found in the pilot rehearsal: a property whose billing
# account had been replaced kept reading the old one, so the hotel's usage
# screen showed a zero balance and an unknown tier while its actual wallet was
# full. In production the same staleness would outlive an account closure or a
# group restructuring for as long as the process ran, and the charge path would
# be pointing at an account that no longer exists.
#
# So the cache has a ceiling on how wrong it can be. Five minutes is far shorter
# than the interval at which this value genuinely changes and far longer than
# the burst of calls a single guest conversation makes, which is the traffic the
# cache exists for.

_ACCOUNT_CACHE_TTL_SECONDS = 300.0

# Two dicts rather than one of tuples, so the cached value stays a plain account
# id: this is a private structure, but it is one several tests reach into, and a
# changed representation would have made them fail for a reason unrelated to
# what they assert.
_account_cache: dict[str, str] = {}
_account_cached_at: dict[str, float] = {}


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
    if cached is not None:
        # An entry with no timestamp was put here directly rather than resolved
        # — a test, or a caller reaching in. Stamp it now rather than trusting
        # it forever: there is no path that should be permanently exempt from
        # the ceiling.
        cached_at = _account_cached_at.setdefault(property_id, time.monotonic())
        if time.monotonic() - cached_at < _ACCOUNT_CACHE_TTL_SECONDS:
            return cached
        forget_account(property_id)

    account = await run_db(_lookup_account, property_id)
    if account:
        _account_cache[property_id] = account
        _account_cached_at[property_id] = time.monotonic()
    return account


def forget_account(property_id: str) -> None:
    """
    Drop a cached resolution now rather than waiting for it to expire.

    Called by the provisioning path when a property is attached to a different
    account, by `wallet_for_property` when the cache is caught being wrong, and
    by tests. The TTL is the backstop; this is the fast path.
    """
    _account_cache.pop(property_id, None)
    _account_cached_at.pop(property_id, None)


async def wallet_for_property(property_id: str) -> tuple[str | None, dict]:
    """
    A property's account **and** its wallet, resolved together and self-healing.

    The pair matters. `account_for_property` answers from a cache, and a cache
    can be wrong; the wallet lookup is the cheapest possible proof of whether it
    is. A cached account whose wallet does not exist is not a hotel with an
    empty wallet — it is a stale answer — so this drops it and resolves once
    more before believing the zero.

    Without this the failure is quiet and expensive: the usage screen reads
    "0 credits, tier unknown" beside a wallet that is full, and nobody can tell
    from the screen which of the two is lying.
    """
    account = await account_for_property(property_id)
    if not account:
        return None, {}

    state = await wallet_state(account)
    if state.get("exists"):
        return account, state

    forget_account(property_id)
    fresh = await account_for_property(property_id)
    if not fresh or fresh == account:
        return fresh, state
    return fresh, await wallet_state(fresh)


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


async def debit(
    *,
    billing_account_id: str,
    credits: int,
    idempotency_key: str,
    reason: str | None = None,
    actor_type: str = "admin",
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """
    Take credits back. The mirror of `grant`, and the only way to.

    `grant_credits` refuses a negative — a grant makes a bucket and a negative
    bucket is not a thing — and `guard_wallet_cache` refuses an UPDATE on the
    balance, correctly. So an operator who granted 5,000 by mistake had no way
    back at all until this existed.

    Positive `credits` here means "remove this many". A signed amount would be
    one sign error away from doubling a balance somebody meant to halve.
    """
    return await run_db(_rpc, "debit_credits", {
        "p_billing_account": billing_account_id,
        "p_credits": credits,
        "p_idempotency_key": idempotency_key,
        "p_reason": reason,
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


# ── low balance ─────────────────────────────────────────────────────────────
#
# `credit_wallets.low_balance_notified_at` has existed since migration 12 and
# nothing has ever written it. It could not support "notify once" on its own: a
# wallet that dips to LOW, is topped up, and dips again should notify twice, and
# a wallet sitting at LOW through four hundred messages should notify once. A
# timestamp cannot tell those apart, which is why migration 17 put the tier
# beside it.
#
# The rule is a comparison, not a lock: notify when the current tier is *worse*
# than the last one notified, and clear the record when the wallet recovers.
# Concurrent callers converge — two workers both seeing 'critical' both compute
# "worse than low", and the second write is the same write.

# Worst last. Anything not here — 'ok', 'unknown' — is a recovery.
_TIER_SEVERITY = {"low": 1, "critical": 2, "empty": 3}


@dataclass
class LowBalanceEvent:
    """A tier crossing worth telling somebody about. `notify` is false for most."""

    notify: bool
    tier: str
    previous: str | None = None
    available: int = 0
    billing_account_id: str | None = None


def _read_notified(account: str) -> dict | None:
    rows = (
        get_db().table("credit_wallets")
        .select("id,low_balance_tier,low_balance_notified_at,balance_credits")
        .eq("billing_account_id", account).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _write_notified(wallet_id: str, patch: dict) -> None:
    get_db().table("credit_wallets").update(patch).eq("id", wallet_id).execute()


async def check_low_balance(billing_account_id: str) -> LowBalanceEvent:
    """
    Decide whether this wallet has just crossed into a worse tier.

    Returns `notify=True` at most once per crossing. The caller does the telling
    — this function only owns the idempotency, because "have we already said
    this" is a question about state and not about notification channels.

    Swallows its own failures into `notify=False`. A wallet read that fails must
    never be the reason a campaign stops or a message is not sent; the worst
    outcome of getting this wrong is a hotel not being warned, and the worst
    outcome of raising is a hotel not being served.
    """
    try:
        state = await wallet_state(billing_account_id)
        wallet = await run_db(_read_notified, billing_account_id)
    except Exception:  # noqa: BLE001
        logger.warning("could not evaluate low balance for %s", billing_account_id,
                       exc_info=True)
        return LowBalanceEvent(notify=False, tier="unknown")

    if not wallet or not state.get("exists"):
        return LowBalanceEvent(notify=False, tier="unknown")

    tier = str(state.get("tier") or "ok")
    previous = wallet.get("low_balance_tier")
    available = int(state.get("available") or 0)
    severity = _TIER_SEVERITY.get(tier, 0)
    previous_severity = _TIER_SEVERITY.get(previous or "", 0)

    if severity == 0:
        # Recovered. Clearing the record is what makes the *next* dip notify
        # again — without it a hotel is warned once, ever.
        if previous is not None:
            await run_db(_write_notified, wallet["id"], {
                "low_balance_tier": None, "low_balance_notified_at": None,
            })
        return LowBalanceEvent(notify=False, tier=tier, previous=previous,
                               available=available,
                               billing_account_id=billing_account_id)

    if severity <= previous_severity:
        # Still low, or improved without recovering. Already said.
        return LowBalanceEvent(notify=False, tier=tier, previous=previous,
                               available=available,
                               billing_account_id=billing_account_id)

    await run_db(_write_notified, wallet["id"], {
        "low_balance_tier": tier,
        "low_balance_notified_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("billing account %s crossed into %s (available %s)",
                billing_account_id, tier, available)
    return LowBalanceEvent(notify=True, tier=tier, previous=previous,
                           available=available,
                           billing_account_id=billing_account_id)
