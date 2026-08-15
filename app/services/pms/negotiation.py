"""
How far a price may move, and who has to say so.

The discount floor has existed since the first pricing work: `price_stay()`
clamps a requested discount to `properties.settings.max_discount_pct`, which is
what makes it safe to let a model *ask* for 30% off — the worst it can get is
the floor. That was enough while an agent could only ever discount by itself.

Selling needs a second number. A guest who says "another hotel quoted me less"
is asking for something the agent cannot grant and a manager might, and the
honest answer to that is neither "no" nor a silent clamp to the automatic floor
— it is a proposal a human approves. So:

    max_discount_pct           what the agent may grant unattended
    max_approved_discount_pct  the ceiling an approval may authorise
    discount_reasons           what an exception may be filed under

**The approval ceiling defaults to the automatic one.** A property that has
never opened its settings gets no negotiating headroom at all, and
`propose_rate_exception` tells the agent so rather than queuing something no
manager can grant. Defaulting the other way — say, twice the automatic floor —
would have handed every hotel on the platform a discount authority it never
asked for, the day this shipped, discoverable only by reading a bill.

**Both ceilings are enforced in the same place: `price_stay()`.** This module
resolves numbers; it does not apply them. An approved exception raises the
clamp, it does not remove it, so an approval carrying `discount_pct: 90` still
produces a quote at the property's approval ceiling. That is the second layer,
and it exists because the first layer is an argument on an approval row that a
manager pressed a button on without reading.

Nothing here is a table. See migration 16, §4, for why.
"""
import logging
from dataclasses import dataclass

from app.db.client import get_db, run_db
from app.services.pms.pricing import DEFAULT_MAX_DISCOUNT_PCT, resolve_property_limits

logger = logging.getLogger(__name__)

# What a discount may be filed under.
#
# **This vocabulary is the database's, not this module's.**
# `quotes_discount_reason_check` has enumerated it since migration 4, and the
# first draft of this file invented a second, more descriptive one —
# `long_stay`, `price_match`, `goodwill`. It read better and it was wrong: the
# insert failed on the check constraint at the moment a manager pressed Approve,
# which is the worst possible place to discover a vocabulary mismatch.
#
# The existing words cover the sales cases once you read them as what they are:
#
#     volume       a long stay or several rooms
#     loyalty      a guest who has stayed before
#     negotiation  a price argued down, including a competitor match
#     coupon       a campaign the hotel is running
#     manual       everything else, and a human should look at it
#
# `creator` is deliberately not offered to a sales agent: it is a marketing
# mechanism with its own approval path, and an agent filing a negotiated
# discount under it would corrupt the one report that reads it.
QUOTE_REASONS = frozenset({"volume", "negotiation", "coupon", "loyalty", "manual"})

DEFAULT_REASONS = ("volume", "negotiation", "loyalty", "manual")


@dataclass(frozen=True)
class NegotiationPolicy:
    """One property's negotiating rules, as the agent is allowed to see them."""

    auto_max_pct: int
    approved_max_pct: int
    reasons: tuple[str, ...]
    token_pct: int

    @property
    def negotiable(self) -> bool:
        """Whether an exception is worth proposing at all."""
        return self.approved_max_pct > self.auto_max_pct

    def ceiling_for(self, *, approved: bool) -> int:
        return self.approved_max_pct if approved else self.auto_max_pct


def resolve(settings: dict | None) -> NegotiationPolicy:
    """
    Read the policy off `properties.settings`, forgivingly.

    Malformed values fall back rather than raise, for the same reason
    `resolve_property_limits` does: a property whose JSON somebody hand-edited
    should still be able to take a booking, just not a generous one. Every
    fallback here is the conservative direction.
    """
    settings = settings or {}
    auto, token = resolve_property_limits(settings)

    try:
        approved = int(settings.get("max_approved_discount_pct", auto))
    except (TypeError, ValueError):
        approved = auto

    # An approval ceiling below the automatic floor is a contradiction — it would
    # mean a manager may authorise less than the agent already may — and the safe
    # reading is that the property meant no extra headroom, not that it meant to
    # revoke what the agent has.
    approved = max(auto, min(approved, 100))

    raw = settings.get("discount_reasons")
    reasons: tuple[str, ...] = DEFAULT_REASONS
    if isinstance(raw, list):
        # Filtered against the column's own check constraint, not merely
        # cleaned. A hotelier who types 'long_stay' into their settings gets a
        # reason the agent refuses to use, rather than one it accepts and then
        # fails to insert under — which would turn a typo in a JSON field into a
        # 500 at the moment a manager approved a discount.
        cleaned = tuple(
            r for r in (
                str(x).strip().lower().replace(" ", "_")
                for x in raw if isinstance(x, str) and x.strip()
            )
            if r in QUOTE_REASONS
        )
        # An empty list means the property configured its way to no valid
        # reasons, which would make every exception unfileable. Read as "not
        # configured" instead of as a lock nobody could have intended.
        if cleaned:
            reasons = cleaned

    return NegotiationPolicy(
        auto_max_pct=auto,
        approved_max_pct=approved,
        reasons=reasons,
        token_pct=token,
    )


def _read_settings(property_id: str) -> dict | None:
    rows = (
        get_db().table("properties").select("settings")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return rows[0].get("settings") if rows else None


async def for_property(property_id: str) -> NegotiationPolicy:
    """
    The policy for one property.

    Not cached. A hotelier who lowers their discount ceiling because the agent
    has been too generous expects the next message to obey it, and the read is
    one indexed row on a table already in every relevant query plan.
    """
    try:
        return resolve(await run_db(_read_settings, property_id))
    except Exception:  # noqa: BLE001
        logger.warning("could not read negotiation policy for %s", property_id, exc_info=True)
        # The conservative default, not the property's. A settings read that
        # failed must never widen what the agent may do.
        return resolve({"max_discount_pct": DEFAULT_MAX_DISCOUNT_PCT})
