"""
Availability — what can be sold, for a specific stay.

Reads `property_availability()` and `property_rates()` (migration 3), which
materialise the sparse-row inheritance in SQL so nothing up here reimplements
the coalesce.

The two rules this module exists to get right, both of which are easy to get
subtly wrong and expensive when you do:

**Availability across a stay is the worst night, not the average.** A room type
free on four nights of five is not sellable for a five-night stay. Taking a
minimum rather than a mean is the difference between an honest "not available"
and an overbooking discovered at 2 PM on arrival day.

**Minimum stay is checked against the arrival night only.** That is how every
channel manager and every OTA evaluates it, and checking every night would
reject a perfectly good week-long stay because it passes through a Saturday with
a two-night minimum. Closed-to-arrival is likewise an arrival-night rule, and
closed-to-departure a departure-night one.
"""
import logging
from dataclasses import dataclass
from datetime import date

from app.db.client import get_db, run_db
from app.services.pms.pricing import date_range, nights_between

logger = logging.getLogger(__name__)


@dataclass
class RatePlanOption:
    rate_plan_id: str
    code: str
    name: str
    meal_plan: str
    audience: str
    refundable: bool
    inclusions: list[str]
    nightly_paise: list[tuple[date, int]]
    total_paise: int
    per_night_paise: int
    # Non-null when the arrival night carries a minimum longer than this stay.
    min_stay_violation: int | None
    closed: bool


@dataclass
class StayOption:
    room_type_id: str
    room_type_code: str
    room_type_name: str
    units_left: int
    max_occupancy: int
    rate_plans: list[RatePlanOption]


def _fetch(property_id: str, check_in: date, check_out: date, audience: str | None):
    db = get_db()
    avail = db.rpc(
        "property_availability",
        {"p_property": property_id, "p_from": check_in.isoformat(), "p_to": check_out.isoformat()},
    ).execute()
    rates = db.rpc(
        "property_rates",
        {
            "p_property": property_id,
            "p_from": check_in.isoformat(),
            "p_to": check_out.isoformat(),
            "p_audience": audience,
        },
    ).execute()
    plans = (
        db.table("rate_plans")
        .select("id,room_type_id,code,name,meal_plan,audience,refundable,inclusions,base_price_paise,min_stay")
        .eq("property_id", property_id)
        .eq("is_active", True)
        .execute()
    )
    types = (
        db.table("room_types")
        .select("id,code,name,max_occupancy,total_units,sort_order")
        .eq("property_id", property_id)
        .eq("is_active", True)
        .order("sort_order")
        .execute()
    )
    return avail.data or [], rates.data or [], plans.data or [], types.data or []


async def stay_options(
    property_id: str,
    check_in: date,
    check_out: date,
    units: int = 1,
    audience: str | None = None,
) -> list[StayOption]:
    """
    Every room type that could take this stay, priced on every rate plan the
    audience may see.

    `audience=None` returns everything — the front desk sees agent rates because
    it books on behalf of agents. The public booking engine must pass 'public',
    and the agent portal is the only caller that may pass 'b2b'. That decision
    belongs to the endpoint, which knows who is asking; this function only
    filters.
    """
    nights = nights_between(check_in, check_out)
    if nights <= 0:
        return []

    avail, rates, plans, types = await run_db(_fetch, property_id, check_in, check_out, audience)
    days = date_range(check_in, check_out)

    # room_type_id -> date -> units_left
    left_by_type: dict[str, dict[str, int]] = {}
    stop_sell: dict[str, set[str]] = {}
    for row in avail:
        left_by_type.setdefault(row["room_type_id"], {})[row["date"]] = row["units_left"]
        if row.get("stop_sell"):
            stop_sell.setdefault(row["room_type_id"], set()).add(row["date"])

    # rate_plan_id -> date -> row
    rate_by_plan: dict[str, dict[str, dict]] = {}
    for row in rates:
        rate_by_plan.setdefault(row["rate_plan_id"], {})[row["date"]] = row

    arrival = check_in.isoformat()
    departure_night = days[-1].isoformat()
    options: list[StayOption] = []

    for rt in types:
        by_date = left_by_type.get(rt["id"], {})
        blocked = stop_sell.get(rt["id"], set())

        # The worst night, not the average. A night with no row inherits the
        # room type's total_units, which property_availability already returned —
        # a missing key here means the RPC did not cover the day, so treat it as
        # zero rather than as "probably fine".
        units_left = min(
            (0 if d.isoformat() in blocked else by_date.get(d.isoformat(), 0)) for d in days
        )

        plan_options: list[RatePlanOption] = []
        for plan in plans:
            if plan["room_type_id"] != rt["id"]:
                continue
            if audience is not None and plan["audience"] != audience:
                continue

            per_date = rate_by_plan.get(plan["id"], {})
            nightly: list[tuple[date, int]] = []
            closed = False
            for d in days:
                row = per_date.get(d.isoformat())
                if row is None:
                    nightly.append((d, plan["base_price_paise"]))
                    continue
                if row.get("closed"):
                    closed = True
                nightly.append((d, row.get("price_paise") or plan["base_price_paise"]))

            # Arrival-night restrictions. CTA blocks a stay *starting* here and
            # says nothing about one passing through.
            arrival_row = per_date.get(arrival, {})
            if arrival_row.get("closed_to_arrival"):
                closed = True
            departure_row = per_date.get(departure_night, {})
            if departure_row.get("closed_to_departure"):
                closed = True

            arrival_min = arrival_row.get("min_stay") or plan.get("min_stay")
            violation = int(arrival_min) if arrival_min and nights < int(arrival_min) else None

            total = sum(p for _, p in nightly) * units
            plan_options.append(
                RatePlanOption(
                    rate_plan_id=plan["id"],
                    code=plan["code"],
                    name=plan["name"],
                    meal_plan=plan["meal_plan"],
                    audience=plan["audience"],
                    refundable=plan["refundable"],
                    inclusions=plan.get("inclusions") or [],
                    nightly_paise=nightly,
                    total_paise=total,
                    per_night_paise=round(total / nights / units) if nights and units else 0,
                    min_stay_violation=violation,
                    closed=closed,
                )
            )

        if plan_options:
            options.append(
                StayOption(
                    room_type_id=rt["id"],
                    room_type_code=rt["code"],
                    room_type_name=rt["name"],
                    units_left=max(units_left, 0),
                    max_occupancy=rt["max_occupancy"],
                    rate_plans=plan_options,
                )
            )

    return options


async def nightly_for_plan(
    property_id: str, rate_plan_id: str, check_in: date, check_out: date
) -> list[tuple[date, int]] | None:
    """
    The per-night prices for one rate plan, or None when the plan is closed or
    unknown. Used by the quote path, which must not re-derive prices from a
    caller-supplied number.
    """
    options = await stay_options(property_id, check_in, check_out, units=1, audience=None)
    for opt in options:
        for plan in opt.rate_plans:
            if plan.rate_plan_id == rate_plan_id:
                return None if plan.closed else plan.nightly_paise
    return None
