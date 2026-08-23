"""
Which rate applies, before anything is negotiated.

`price_stay()` turns per-night rates into money and does it well. This module is
the step before: deciding what the per-night rate *is* for this particular
request, when the answer depends on how many rooms are being taken, which plan
is being quoted, and whether that plan has a price of its own at all.

Two mechanisms, both of which the schema could not express until now:

**A derived rate plan has no price.** "Agents get 15% off whatever we're
charging" was previously a second set of numbers a hotelier re-typed every time
the public calendar moved, and the day somebody forgot, agencies were selling
last season's rate. A derived plan resolves its parent's rate *per night* and
takes the percentage off, so a seasonal public rate carries its agency rate with
it automatically.

**A group tier is a rate, not a discount.** This is the load-bearing decision in
this file. Six rooms at 10% off could be modelled either as a lower nightly rate
or as a discount on the total, and they are not the same thing:

  * As a *discount*, it competes with the negotiation ceiling — a hotel whose
    `max_discount_pct` is 10 could never negotiate a rupee off a group booking,
    because the volume tier had already spent the whole allowance.
  * As a *rate*, it flows into `gross_paise` like any other price, `nightly_paise`
    records what each night actually cost so the quote stays reconstructible, and
    the GST slab is decided on the rate the guest is really paying — which
    matters, because a tier that drops a night below ₹7,500 genuinely moves it
    into the 12% band.

So group pricing lowers the nightly rate and negotiation remains a separate
allowance on top of it. The precedence in §34 falls out of that rather than being
enforced by a rule somewhere:

    negotiated rate   ← applied last, as an authorised discount on the gross
    group tier        ← here, lowers the nightly
    B2B / derived     ← here, resolves the plan's own nightly
    seasonal calendar ← rate_plan_rates, already
    plan base price   ← rate_plans.base_price_paise, already

**One level of derivation, and it is checked rather than assumed.** A plan
derived from a plan that is itself derived would need a resolution order, and a
cycle would need a visited set; the honest version of that feature is "no", said
out loud, until a hotel asks for it.
"""
import logging
from dataclasses import dataclass
from datetime import date

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GroupTier:
    """One band of the volume ladder."""

    id: str
    room_type_id: str | None
    min_units: int
    max_units: int | None
    discount_pct: int | None
    fixed_price_paise: int | None
    negotiable: bool

    def covers(self, units: int) -> bool:
        return units >= self.min_units and (self.max_units is None or units <= self.max_units)

    @property
    def is_quotable(self) -> bool:
        """
        Whether this tier produces a number.

        A `negotiable` tier with no discount and no fixed price is the "10+
        rooms, let's talk" band: real, configured, and deliberately not a price.
        The agent may open a negotiation on it; it may not invent a rate for it,
        and `apply()` returning the rate unchanged is what makes that true even
        if somebody forgets.
        """
        return self.discount_pct is not None or self.fixed_price_paise is not None


@dataclass(frozen=True)
class GroupPolicy:
    """What else changes when a booking is a group."""

    min_units: int
    deposit_pct: int | None
    payment_due_days: int | None
    cancellation: dict
    meal_package: str | None
    blackout_dates: tuple[str, ...]
    terms: str | None


@dataclass(frozen=True)
class AppliedTier:
    """A tier that was actually used, and what it did — for the quote's story."""

    tier_id: str
    min_units: int
    max_units: int | None
    kind: str  # 'fixed' | 'discount' | 'negotiable'
    discount_pct: int | None
    fixed_price_paise: int | None


# ── group tiers ──────────────────────────────────────────────────────────────


def select_tier(tiers: list[GroupTier], units: int, room_type_id: str) -> GroupTier | None:
    """
    The tier that applies to this request, or None.

    Two rules, both of which a hotelier would guess correctly and neither of
    which is free:

    **A tier naming this room type beats one that names none.** A hotel that
    gives 10% on everything and 5% on its suites means the suite rule to win,
    not to be shadowed by whichever row was created first.

    **Within the same specificity, the highest `min_units` the request satisfies
    wins.** Overlapping bands are a configuration mistake rather than an
    impossibility — 1–5 and 3–9 can both be saved — and resolving them by
    insertion order would make the price depend on a column nobody can see.
    Taking the highest qualifying floor reads the hotelier's intent: more rooms
    is never meant to be worse for the guest.
    """
    candidates = [t for t in tiers if t.covers(units)]
    if not candidates:
        return None
    specific = [t for t in candidates if t.room_type_id == room_type_id]
    pool = specific or [t for t in candidates if t.room_type_id is None]
    if not pool:
        return None
    return max(pool, key=lambda t: t.min_units)


def apply_tier(
    nightly: list[tuple[date, int]], tier: GroupTier | None
) -> tuple[list[tuple[date, int]], AppliedTier | None]:
    """
    Rewrite the per-night rates under a tier.

    A fixed price *replaces* every night; a discount takes a percentage off each
    one. Fixed wins where a hotelier has somehow saved both, because it is the
    more specific statement — "groups of ten pay ₹3,800 a night" leaves nothing
    for a percentage to modify.

    A tier that is only `negotiable` changes nothing and says so. That is not a
    no-op to be optimised away: the caller needs to know a negotiable band was
    reached, because that is the difference between "here is your group rate"
    and "let me check with the hotel".
    """
    if tier is None:
        return nightly, None

    if tier.fixed_price_paise is not None:
        price = max(int(tier.fixed_price_paise), 0)
        return (
            [(d, price) for d, _ in nightly],
            AppliedTier(tier.id, tier.min_units, tier.max_units, "fixed", None, price),
        )

    if tier.discount_pct:
        pct = max(0, min(int(tier.discount_pct), 100))
        # Rounded per night rather than once on the total, because the per-night
        # number is what the quote stores and what the guest is shown. Rounding
        # the total and dividing back would produce nightly rates that do not
        # sum to it.
        return (
            [(d, p - round(p * pct / 100)) for d, p in nightly],
            AppliedTier(tier.id, tier.min_units, tier.max_units, "discount", pct, None),
        )

    return (
        nightly,
        AppliedTier(tier.id, tier.min_units, tier.max_units, "negotiable", None, None),
    )


def _tier_from_row(row: dict) -> GroupTier:
    return GroupTier(
        id=str(row["id"]),
        room_type_id=str(row["room_type_id"]) if row.get("room_type_id") else None,
        min_units=int(row.get("min_units") or 1),
        max_units=int(row["max_units"]) if row.get("max_units") is not None else None,
        discount_pct=int(row["discount_pct"]) if row.get("discount_pct") is not None else None,
        fixed_price_paise=(
            int(row["fixed_price_paise"]) if row.get("fixed_price_paise") is not None else None
        ),
        negotiable=bool(row.get("negotiable")),
    )


def _read_tiers(property_id: str) -> list[dict]:
    return (
        get_db().table("group_rate_tiers").select("*")
        .eq("property_id", property_id).eq("is_active", True)
        .order("min_units").limit(50).execute().data or []
    )


async def tiers_for(property_id: str) -> list[GroupTier]:
    """
    Every active tier for one hotel.

    A read that fails returns no tiers rather than raising. The consequence is
    that a group is quoted the public rate — the hotel's own list price, which
    is defensible and which the hotel can honour — where the alternative is a
    500 at the moment a guest asked for a price. Logged loudly, because a hotel
    losing its volume pricing silently is its own kind of bug.
    """
    try:
        return [_tier_from_row(r) for r in await run_db(_read_tiers, property_id)]
    except Exception:  # noqa: BLE001
        logger.warning("could not read group tiers for %s", property_id, exc_info=True)
        return []


def _read_group_policy(property_id: str) -> dict | None:
    rows = (
        get_db().table("group_policies").select("*")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


async def group_policy_for(property_id: str) -> GroupPolicy | None:
    try:
        row = await run_db(_read_group_policy, property_id)
    except Exception:  # noqa: BLE001
        logger.warning("could not read group policy for %s", property_id, exc_info=True)
        return None
    if not row:
        return None
    return GroupPolicy(
        min_units=int(row.get("min_units") or 3),
        deposit_pct=int(row["deposit_pct"]) if row.get("deposit_pct") is not None else None,
        payment_due_days=(
            int(row["payment_due_days"]) if row.get("payment_due_days") is not None else None
        ),
        cancellation=row.get("cancellation") or {},
        meal_package=row.get("meal_package"),
        blackout_dates=tuple(str(d) for d in (row.get("blackout_dates") or [])),
        terms=row.get("terms"),
    )


# ── derived plans and validity ───────────────────────────────────────────────


def plan_open_on(plan: dict, day: date) -> bool:
    """
    Whether a rate plan may be sold for a given night.

    `valid_from` / `valid_to` are inclusive — a contract that runs "to 31 March"
    includes the 31st, which is what everyone who signs one means — and
    `days_of_week` is ISO (1 = Monday … 7 = Sunday) because that is what
    Postgres's `isodow` and Python's `isoweekday()` both use, and mixing the two
    conventions is how a Sunday rate lands on a Monday.

    An empty `days_of_week` means every day. It is the default and the common
    case, and reading it as "no days" would close every plan on the platform the
    moment this shipped.
    """
    valid_from = plan.get("valid_from")
    valid_to = plan.get("valid_to")
    if valid_from and day < _as_date(valid_from):
        return False
    if valid_to and day > _as_date(valid_to):
        return False
    days = plan.get("days_of_week") or []
    if days and day.isoweekday() not in {int(d) for d in days}:
        return False
    return True


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def resolve_derived(
    plans: list[dict], nightly_by_plan: dict[str, list[tuple[date, int]]]
) -> dict[str, list[tuple[date, int]]]:
    """
    Fill in the per-night rates for plans that have no price of their own.

    Takes the map every other plan has already been resolved into and adds the
    derived ones, per night, from their parent. Returns a new map rather than
    mutating: this is called from inside `stay_options`, whose correctness
    depends on nothing else having quietly changed the rates it already computed.

    **A derived plan whose parent is missing is skipped, not zero-priced.** The
    parent may be inactive, deleted, or belong to another room type — and a rate
    plan that resolves to ₹0 is a room being given away, which is a far worse
    failure than a rate plan that does not appear. The caller sees no entry and
    treats the plan as closed.

    **A parent that is itself derived is refused.** One level, checked here
    rather than assumed, and logged so the configuration that caused it is
    findable.
    """
    by_id = {str(p["id"]): p for p in plans}
    out = dict(nightly_by_plan)

    for plan in plans:
        parent_id = plan.get("derived_from_plan_id")
        pct = plan.get("derive_discount_pct")
        if not parent_id or pct is None:
            continue

        parent = by_id.get(str(parent_id))
        if parent is None:
            logger.warning(
                "rate plan %s derives from %s, which is not active on this property",
                plan.get("id"), parent_id,
            )
            out.pop(str(plan["id"]), None)
            continue
        if parent.get("derived_from_plan_id"):
            logger.warning(
                "rate plan %s derives from %s, which is itself derived — refusing",
                plan.get("id"), parent_id,
            )
            out.pop(str(plan["id"]), None)
            continue

        parent_nightly = nightly_by_plan.get(str(parent_id))
        if not parent_nightly:
            out.pop(str(plan["id"]), None)
            continue

        cut = max(0, min(int(pct), 100))
        out[str(plan["id"])] = [(d, p - round(p * cut / 100)) for d, p in parent_nightly]

    return out
