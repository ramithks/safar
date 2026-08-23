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
from app.services.pms import rates as rate_rules
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
    # How many units this *plan* may sell, which is the room type's stock unless
    # the plan carries a smaller allocation. An agency contracted for two rooms
    # in a ten-room hotel must not be shown ten.
    units_left: int = 0
    # Set when a volume tier moved the nightly rate. Carries the band so a
    # hotelier reading a quote can see which of their own rules produced it.
    group_tier: rate_rules.AppliedTier | None = None


@dataclass
class StayOption:
    room_type_id: str
    room_type_code: str
    room_type_name: str
    units_left: int
    max_occupancy: int
    rate_plans: list[RatePlanOption]


def _own_holds(property_id: str, conversation_id: str,
               check_in: date, check_out: date) -> dict[str, int]:
    """
    Units this conversation is holding right now, by room type.

    Only `active` holds that have not expired, and only ones covering the same
    stay — a hold for different dates is not a room this guest can have on
    these ones. Failure is an empty dict rather than an exception: showing a
    guest slightly less availability is a worse day than a 500, and the numbers
    without this are what every other caller already sees.
    """
    from datetime import datetime, timezone

    try:
        rows = (
            get_db().table("inventory_holds")
            .select("room_type_id,units,check_in,check_out,expires_at,status")
            .eq("property_id", property_id)
            .eq("conversation_id", conversation_id)
            .eq("status", "active")
            .execute().data or []
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not read own holds for conversation %s",
                       conversation_id, exc_info=True)
        return {}

    now = datetime.now(timezone.utc)
    held: dict[str, int] = {}
    for row in rows:
        if str(row.get("check_in")) != check_in.isoformat():
            continue
        if str(row.get("check_out")) != check_out.isoformat():
            continue
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        if expires <= now:
            continue
        held[row["room_type_id"]] = held.get(row["room_type_id"], 0) + int(row.get("units") or 0)
    return held


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
        .select("id,room_type_id,code,name,meal_plan,audience,refundable,inclusions,base_price_paise,min_stay,derived_from_plan_id,derive_discount_pct,valid_from,valid_to,days_of_week,allocation_units")
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
    for_conversation: str | None = None,
) -> list[StayOption]:
    """
    Every room type that could take this stay, priced on every rate plan the
    audience may see.

    `for_conversation` adds back the rooms this conversation is *already*
    holding. A quote takes a hold, and the hold counts against `units_left`
    like any other — so on the very next turn the agent asked what was
    available and was told the rooms it had just quoted were gone. In
    production, to a real guest:

        Ok, 10 September se 14 September tak 2 rooms available hain.
        …
        Maaf karna, King Bed rooms 10 September se 14 September tak available
        nahi hain.

    Two rooms of that type existed and the agent's own quote held both. The
    guest was told the room was theirs, then that it did not exist. Nobody
    else's hold is added back — only the caller's own, which is the one that
    was taken *for* them.

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

    mine = (
        await run_db(_own_holds, property_id, for_conversation, check_in, check_out)
        if for_conversation else {}
    )

    # room_type_id -> date -> units_left
    left_by_type: dict[str, dict[str, int]] = {}
    stop_sell: dict[str, set[str]] = {}
    for row in avail:
        # A room this conversation is already holding is a room it can still
        # have. Added back per room type, for every night of the stay, because
        # a hold covers the whole stay or none of it.
        left = row["units_left"] + mine.get(row["room_type_id"], 0)
        left_by_type.setdefault(row["room_type_id"], {})[row["date"]] = left
        if row.get("stop_sell"):
            stop_sell.setdefault(row["room_type_id"], set()).add(row["date"])

    # rate_plan_id -> date -> row
    rate_by_plan: dict[str, dict[str, dict]] = {}
    for row in rates:
        rate_by_plan.setdefault(row["rate_plan_id"], {})[row["date"]] = row

    arrival = check_in.isoformat()
    departure_night = days[-1].isoformat()
    options: list[StayOption] = []

    # Rates are resolved for every plan before any room type is built, in two
    # passes, because a derived plan cannot be priced until its parent has been.
    # Doing it inside the room-type loop would mean a plan's price depended on
    # whether its parent's room type happened to be iterated first.
    nightly_by_plan: dict[str, list[tuple[date, int]]] = {}
    closed_by_plan: dict[str, bool] = {}
    for plan in plans:
        per_date = rate_by_plan.get(plan["id"], {})
        base = plan.get("base_price_paise") or 0
        resolved: list[tuple[date, int]] = []
        shut = False
        for d in days:
            # A plan outside its contract dates, or off its selling days, is
            # closed for this stay rather than absent — the difference matters to
            # an agency asking why its rate disappeared.
            if not rate_rules.plan_open_on(plan, d):
                shut = True
            row = per_date.get(d.isoformat())
            if row is None:
                resolved.append((d, base))
                continue
            if row.get("closed"):
                shut = True
            resolved.append((d, row.get("price_paise") or base))
        # A derived plan's own base price is meaningless and is about to be
        # overwritten; it is carried this far only so the map has an entry to
        # replace.
        nightly_by_plan[str(plan["id"])] = resolved
        closed_by_plan[str(plan["id"])] = shut

    nightly_by_plan = rate_rules.resolve_derived(plans, nightly_by_plan)

    # Volume pricing, once, for the whole request. `units` is the number of rooms
    # being asked for, so a tier applies to every room type the same way — the
    # tier itself decides whether it is scoped to one.
    tiers = await rate_rules.tiers_for(property_id) if units > 1 else []

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
            nightly = nightly_by_plan.get(str(plan["id"]))
            if nightly is None:
                # A derived plan whose parent could not be resolved. Skipped
                # rather than shown at zero — see rates.resolve_derived.
                continue
            closed = closed_by_plan.get(str(plan["id"]), False)

            nightly, applied_tier = rate_rules.apply_tier(
                nightly, rate_rules.select_tier(tiers, units, rt["id"])
            )

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
                    # The smaller of the room type's stock and this plan's own
                    # allocation. Enforced again at quote time; shown here so a
                    # caller never offers what it cannot then sell.
                    units_left=min(
                        max(units_left, 0),
                        int(plan["allocation_units"])
                        if plan.get("allocation_units") is not None
                        else max(units_left, 0),
                    ),
                    group_tier=applied_tier,
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
    property_id: str, rate_plan_id: str, check_in: date, check_out: date, units: int = 1
) -> tuple[list[tuple[date, int]], "rate_rules.AppliedTier | None"] | None:
    """
    The per-night prices for one rate plan, or None when the plan is closed or
    unknown. Used by the quote path, which must not re-derive prices from a
    caller-supplied number.

    **`units` is not decoration.** It was added because volume pricing has to be
    decided in exactly one place, and this is the function the quote path calls.
    Resolving the tier here means a hotel's group rate reaches a booking through
    the same route as its seasonal rate — no second code path, no chance of the
    dashboard and the guest seeing different numbers for six rooms.

    Returns the rates *and* which tier produced them, because a quote that
    silently came out 10% lower than the published rate is one nobody can explain
    to a hotelier three weeks later.
    """
    options = await stay_options(property_id, check_in, check_out, units=units, audience=None)
    for opt in options:
        for plan in opt.rate_plans:
            if plan.rate_plan_id == rate_plan_id:
                if plan.closed:
                    return None
                return plan.nightly_paise, plan.group_tier
    return None
