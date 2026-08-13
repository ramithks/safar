"""
Pricing — the one place a stay is turned into money.

Every caller goes through here: the front desk, the booking engine, the agent
portal and the AI agent. Three implementations of "what does this stay cost"
would be three chances to disagree, and the one that disagrees cheaply is the
one that quotes it to a guest.

Rules that are not negotiable and are enforced here rather than trusted to a
caller:

* **Integer paise, always.** No float touches a total. Percentages are applied
  with `round()` on an integer, once, at a defined point in the sequence — a
  discount computed on a taxed total and a tax computed on a discounted total
  are different numbers, and only one of them is what the guest owes.

* **The discount floor is the property's, not the caller's.** A discount request
  beyond the floor is clamped, not rejected. That is what makes it safe to let a
  negotiating AI agent ask for 30% off: the worst it can do is get the floor.

* **GST follows the tariff slab per night**, not per booking. India charges 12%
  below ₹7,500 per unit per night and 18% at or above it, and a five-night stay
  that crosses the threshold on the two weekend nights is taxed at both rates.
  Charging one rate for the whole stay is the common bug and it is a tax
  filing problem, not a rounding problem.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# The slab boundary, in paise per unit per night. ₹7,500.
GST_SLAB_BOUNDARY_PAISE = 750_000
GST_LOW = 12
GST_HIGH = 18

# The floor a property gets if it has not set one. Deliberately conservative: a
# property that never opens its settings should not discover its AI agent has
# been giving away a third of the rate.
DEFAULT_MAX_DISCOUNT_PCT = 10
# Fraction of the total taken as a booking token when the property has not said.
DEFAULT_TOKEN_PCT = 20


@dataclass
class NightlyRate:
    date: date
    price_paise: int


@dataclass
class NightMoney:
    """
    One night, resolved to money.

    The folio is posted from these, one line per night, rather than from a total
    divided by a night count. That is not a presentation choice: the night audit
    has to be able to post night three without knowing what it did about nights
    one and two, and a stay whose discount does not divide evenly has to still
    sum to the number the guest agreed to.
    """
    date: date
    # Tariff × units, before anything is taken off.
    gross_paise: int
    # This night's share of the stay discount. The last night carries the
    # rounding remainder so the shares sum exactly to the stay discount.
    discount_paise: int
    # What this night puts on the folio as a room charge: gross − discount.
    room_paise: int
    # GST for this night, at this night's slab.
    tax_paise: int


@dataclass
class PricedStay:
    nightly: list[NightlyRate]
    units: int
    gross_paise: int
    discount_paise: int
    discount_pct_applied: int
    tax_paise: int
    total_paise: int
    token_paise: int
    balance_paise: int
    # Set when the requested discount was above the property's floor. The caller
    # shows this to a human; an agent must never present the requested number.
    clamped_from_pct: int | None = None
    tax_breakdown: dict[str, int] = field(default_factory=dict)
    # Per-night money, in stay order. Sums to (gross − discount) and to tax, by
    # construction — see NightMoney.
    nights: list[NightMoney] = field(default_factory=list)


def nights_between(check_in: date, check_out: date) -> int:
    """Half-open: you occupy the nights you sleep, not the morning you leave."""
    return (check_out - check_in).days


def date_range(check_in: date, check_out: date) -> list[date]:
    return [check_in + timedelta(days=i) for i in range(nights_between(check_in, check_out))]


def gst_pct_for_night(price_per_unit_paise: int) -> int:
    """The slab is decided by the per-unit, per-night tariff. Not the total."""
    return GST_HIGH if price_per_unit_paise >= GST_SLAB_BOUNDARY_PAISE else GST_LOW


def price_stay(
    nightly: list[NightlyRate],
    units: int,
    *,
    requested_discount_pct: int = 0,
    max_discount_pct: int | None = None,
    token_pct: int | None = None,
    tax_inclusive: bool = False,
) -> PricedStay:
    """
    Turn per-night rates into a total.

    The sequence matters and is fixed here so no caller can reorder it:

        gross    = Σ(nightly × units)
        discount = round(gross × clamped_pct / 100)      ← before tax
        tax      = Σ per night, on the discounted night rate, at that night's slab
        total    = gross − discount + tax
    """
    if units < 1:
        raise ValueError("units must be at least 1")
    if not nightly:
        raise ValueError("a stay must have at least one night")

    floor_pct = DEFAULT_MAX_DISCOUNT_PCT if max_discount_pct is None else max_discount_pct
    asked = max(int(requested_discount_pct or 0), 0)
    applied = min(asked, max(floor_pct, 0))
    clamped_from = asked if asked > applied else None

    gross = sum(n.price_paise for n in nightly) * units
    discount = round(gross * applied / 100)

    # Tax per night, at that night's slab, on the discounted rate. Spreading the
    # discount evenly across nights keeps the slab decision honest: a discount
    # that drops a night below ₹7,500 genuinely drops it into the lower slab.
    #
    # The allocation runs whether or not tax applies, because the night audit
    # needs the per-night room charge either way. Only the tax line is
    # conditional.
    tax = 0
    breakdown: dict[str, int] = {}
    per_night: list[NightMoney] = []
    remaining_discount = discount
    for i, n in enumerate(nightly):
        night_gross = n.price_paise * units
        # Give the last night whatever rounding left over, so the per-night
        # discounts sum exactly to `discount` rather than to discount ± 1.
        if i == len(nightly) - 1:
            night_discount = remaining_discount
        else:
            night_discount = round(discount * night_gross / gross) if gross else 0
            remaining_discount -= night_discount

        night_net = night_gross - night_discount

        night_tax = 0
        if not tax_inclusive:
            per_unit = night_net // units if units else night_net
            pct = gst_pct_for_night(per_unit)
            night_tax = round(night_net * pct / 100)
            tax += night_tax
            breakdown[f"{pct}%"] = breakdown.get(f"{pct}%", 0) + night_tax

        per_night.append(
            NightMoney(
                date=n.date,
                gross_paise=night_gross,
                discount_paise=night_discount,
                room_paise=night_net,
                tax_paise=night_tax,
            )
        )

    net = gross - discount
    total = net + tax
    token = round(total * (DEFAULT_TOKEN_PCT if token_pct is None else token_pct) / 100)
    token = min(max(token, 0), total)

    return PricedStay(
        nightly=nightly,
        units=units,
        gross_paise=gross,
        discount_paise=discount,
        discount_pct_applied=applied,
        tax_paise=tax,
        total_paise=total,
        token_paise=token,
        balance_paise=total - token,
        clamped_from_pct=clamped_from,
        tax_breakdown=breakdown,
        nights=per_night,
    )


def resolve_property_limits(settings: dict | None) -> tuple[int, int]:
    """
    Read the negotiation floor and token rule off `properties.settings`.

    Missing or malformed settings fall back to the conservative defaults rather
    than raising: a property whose JSON someone hand-edited should still be able
    to take a booking, just not a generous one.
    """
    settings = settings or {}
    try:
        max_discount = int(settings.get("max_discount_pct", DEFAULT_MAX_DISCOUNT_PCT))
    except (TypeError, ValueError):
        max_discount = DEFAULT_MAX_DISCOUNT_PCT
    try:
        token = int(settings.get("token_pct", DEFAULT_TOKEN_PCT))
    except (TypeError, ValueError):
        token = DEFAULT_TOKEN_PCT

    # A floor above 100 or below 0 is a typo, not an instruction.
    return max(0, min(max_discount, 100)), max(0, min(token, 100))
