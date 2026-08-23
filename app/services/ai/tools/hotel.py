"""
What the hotel is, and what it will do about a price.

Nine tools, all thin. Every one of them calls a PMS service and returns its
answer; none of them contains a rule. That is the rule of this whole package —
see `tools/__init__.py` — and it matters more here than anywhere else in the
codebase, because the decisions being surfaced are commercial ones.

**`negotiate_rate` is the tool the brief is built around, and it is the reason
the rest of this exists.** §9 asks for a backend function that takes a booking
context and a requested rate and returns allowed / rejected / counter_offer /
final_rate / reason / expiry, so that "the LLM only communicates the result".
The engine is `services/pms/negotiation.negotiate()`. This is its mouth.

Three properties hold, and between them they are why a model is allowed near a
negotiation at all:

  * **The model does no arithmetic.** It passes on the guest's own number and
    receives a verdict and a price. It never computes a discount, never compares
    against a floor, and has no way to.
  * **The floor is not in the output.** Not as a field, not as a number the
    model could be asked for. A counter-offer may happen to equal it — "₹4,300
    mein confirm kar sakta hoon" is the brief's own script — but the model is
    never told that is the bottom, so it cannot say so.
  * **An allowed rate becomes a real quote.** Not a promise in prose: a `quotes`
    row, priced by `price_stay`, holding stock, expiring in fifteen minutes.
    That is what stops "I can do ₹4,300" being a sentence nobody can honour.

The read tools exist for §32: retrieval that matches the question. Before them
the only way for an agent to answer "breakfast included hai?" was
`get_property_knowledge` over prose somebody had remembered to write, and the
only way to answer "extra bed mil sakta hai?" was to guess.
"""
import logging
from datetime import date, datetime, time, timezone

from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_QUOTE
from app.services.pms import booking as booking_service
from app.services.pms import escalation as escalation_service
from app.services.pms import money
from app.services.pms import negotiation, policies, rates

logger = logging.getLogger(__name__)


class NoArgs(BaseModel):
    """
    Empty on purpose.

    The property comes from the conversation, never from the model. An argument
    naming a hotel would be a way to ask about somebody else's.
    """


# ── the room, in detail ──────────────────────────────────────────────────────


class RoomDetailArgs(BaseModel):
    room_type_id: ResourceId | None = Field(
        default=None,
        description="From search_availability or get_stay_options. Omit for every room.",
    )


def _read_rooms(property_id: str, room_type_id: str | None) -> list[dict]:
    query = (
        get_db().table("room_types").select("*")
        .eq("property_id", property_id).eq("is_active", True)
    )
    if room_type_id:
        query = query.eq("id", room_type_id)
    return query.order("sort_order").limit(30).execute().data or []


def _describe_room(row: dict) -> dict:
    """
    One room, as facts a guest can be told.

    **Unanswered fields are omitted rather than sent as null.** A model handed
    `"extraBed": null` will eventually render it as "extra bed: not available",
    which is a claim the hotel never made — the difference between "we have
    none" and "nobody has told us" is exactly what `readiness` exists to track,
    and collapsing it here would undo that.
    """
    facts: dict = {
        "roomTypeId": str(row["id"]),
        "name": row.get("name"),
        "sleeps": row.get("max_occupancy"),
        "roomsOfThisKind": row.get("total_units"),
    }
    optional = {
        "description": row.get("description"),
        "maxAdults": row.get("max_adults"),
        "maxChildren": row.get("max_children"),
        "childrenAllowed": row.get("children_allowed"),
        "bed": row.get("bed_type"),
        "bedCount": row.get("bed_count"),
        "extraBed": row.get("extra_bed"),
        "extraBedPaise": row.get("extra_bed_paise"),
        "crib": row.get("crib"),
        "cribPaise": row.get("crib_paise"),
        "sizeSqft": row.get("size_sqft"),
        "floor": row.get("floor"),
        "view": row.get("view"),
        "smoking": row.get("smoking"),
        "accessible": row.get("accessible"),
        "connecting": row.get("connecting"),
        "bathroom": row.get("bathroom"),
    }
    facts.update({k: v for k, v in optional.items() if v is not None})
    if row.get("amenities"):
        facts["amenities"] = row["amenities"]
    facts["photoCount"] = len(row.get("photos") or [])
    return facts


@tool(
    name="get_room_details",
    description=(
        "Everything the hotel has recorded about a room: how many adults and "
        "children it sleeps, the bed and how many, whether an extra bed or a "
        "crib is possible and what they cost, the view, the floor, whether it "
        "is smoking, accessible or connecting, and what is in it. Use this for "
        "any question about what a room *is*. It carries no price — "
        "get_stay_options and get_quote do that."
    ),
    input_model=RoomDetailArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_room_details(args: RoomDetailArgs, ctx: AgentContext) -> dict:
    rows = await run_db(_read_rooms, ctx.property_id, args.room_type_id)
    if not rows:
        return {
            "found": False,
            "instruction": (
                "There is no such room here. Do not describe one — offer to show "
                "what the hotel does have."
            ),
        }
    return {
        "found": True,
        "rooms": [_describe_room(r) for r in rows],
        "currency": "INR",
        "instruction": (
            "Answer only from the fields present. A field that is absent is one "
            "the hotel has not told us about — say you will check rather than "
            "guessing, and never say something is unavailable just because it is "
            "not listed."
        ),
    }


# ── arriving early, leaving late ─────────────────────────────────────────────


class StayTimeArgs(BaseModel):
    kind: str = Field(
        description="'early_in' for arriving before check-in, 'late_out' for leaving after check-out.",
    )
    at_time: str = Field(
        description="The time the guest asked about, as HH:MM on a 24-hour clock. '10 baje' in the morning is 10:00.",
    )
    room_type_id: ResourceId | None = Field(
        default=None,
        description="Which room, if known. Only affects a charge quoted as a share of the nightly rate.",
    )


def _read_time_context(property_id: str, room_type_id: str | None) -> tuple[dict, list[dict], int]:
    db = get_db()
    prop = (
        db.table("properties").select("check_in_time,check_out_time")
        .eq("id", property_id).limit(1).execute().data or [{}]
    )[0]
    bands = (
        db.table("stay_time_rules").select("*")
        .eq("property_id", property_id).eq("is_active", True)
        .order("until_time").limit(50).execute().data or []
    )
    # A percentage band needs a nightly rate to be a percentage *of*. The
    # cheapest active plan for the room, or for the hotel, which is the
    # conservative direction: quoting half of the cheapest night is never more
    # than the guest would owe on a dearer one.
    query = (
        db.table("rate_plans").select("base_price_paise,room_type_id")
        .eq("property_id", property_id).eq("is_active", True)
    )
    if room_type_id:
        query = query.eq("room_type_id", room_type_id)
    plans = query.limit(50).execute().data or []
    nightly = min((int(p.get("base_price_paise") or 0) for p in plans), default=0)
    return prop, bands, nightly


@tool(
    name="get_checkin_options",
    description=(
        "Whether the hotel can take a guest early or let them leave late, and "
        "what it costs. Use this for any question about arriving before "
        "check-in or staying past check-out — 'main 10 baje aa sakta hoon?', "
        "'late checkout mil jayega?'. Never answer these from memory."
    ),
    input_model=StayTimeArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_checkin_options(args: StayTimeArgs, ctx: AgentContext) -> dict:
    if args.kind not in ("early_in", "late_out"):
        raise OutOfScope(
            f"unknown kind {args.kind!r}",
            "Could you tell me whether you mean arriving early or leaving late?",
        )
    try:
        wanted = datetime.strptime(args.at_time.strip()[:5], "%H:%M").time()
    except ValueError as exc:
        raise OutOfScope(
            f"unparseable time {args.at_time!r}",
            "What time were you thinking of?",
        ) from exc

    prop, band_rows, nightly = await run_db(_read_time_context, ctx.property_id, args.room_type_id)
    standard_raw = prop.get("check_in_time" if args.kind == "early_in" else "check_out_time")
    standard = (
        datetime.strptime(str(standard_raw)[:5], "%H:%M").time()
        if standard_raw else time(14, 0) if args.kind == "early_in" else time(11, 0)
    )

    verdict = policies.resolve_time_request(
        policies.bands_from_rows(band_rows),
        kind=args.kind, wanted=wanted, standard=standard, nightly_paise=nightly,
    )
    out = verdict.as_dict()
    out["requestedTime"] = wanted.strftime("%H:%M")
    out["currency"] = "INR"

    if not verdict.available:
        out["instruction"] = (
            "The hotel has not said it can do this. Say you will check with the "
            "team rather than refusing outright, and offer to hold their bags."
            if not band_rows else
            "The hotel cannot do this at that time. Say so plainly and offer the "
            "earliest time it can."
        )
    elif verdict.free:
        out["instruction"] = (
            "This is complimentary. Say so, and "
            + ("mention it is subject to availability." if verdict.subject_to_availability
               else "confirm it plainly.")
        )
    else:
        out["instruction"] = (
            "Quote chargePaise and nothing else — do not work out a different "
            "figure. "
            + ("Say it is subject to availability."
               if verdict.subject_to_availability else "")
        )
    return out


# ── meals ────────────────────────────────────────────────────────────────────


class MealArgs(BaseModel):
    rate_plan_id: ResourceId | None = Field(
        default=None,
        description=(
            "The rate being discussed, from get_stay_options. Pass it whenever "
            "you have it: whether a meal is *included* depends on the rate, and "
            "without it you will only learn what the meals are."
        ),
    )


def _read_meals(property_id: str, rate_plan_id: str | None) -> tuple[list[dict], str | None]:
    db = get_db()
    options = (
        db.table("meal_options").select("*")
        .eq("property_id", property_id).limit(10).execute().data or []
    )
    meal_plan = None
    if rate_plan_id:
        rows = (
            db.table("rate_plans").select("meal_plan")
            .eq("id", rate_plan_id).eq("property_id", property_id).limit(1).execute().data or []
        )
        meal_plan = rows[0].get("meal_plan") if rows else None
    return options, meal_plan


@tool(
    name="get_meal_info",
    description=(
        "What the hotel serves, when, at what price, and whether a given rate "
        "already includes it. Use this for 'breakfast included hai?', meal "
        "timings, and whether there is vegetarian food. Do not infer any of it "
        "from a rate plan's name."
    ),
    input_model=MealArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_meal_info(args: MealArgs, ctx: AgentContext) -> dict:
    options, meal_plan = await run_db(_read_meals, ctx.property_id, args.rate_plan_id)
    meals = policies.meals_for_plan(meal_plan, options)
    described = [m for m in meals if m["configured"]]

    return {
        "meals": meals,
        "ratePlanCode": meal_plan,
        "currency": "INR",
        "instruction": (
            (
                "Whether a meal is included is `included` — it comes from the "
                "rate, not from the meal. "
                if args.rate_plan_id else
                "No rate was named, so `included` is against room-only. If the "
                "guest is asking about a specific rate, get_stay_options first. "
            )
            + (
                "A meal with configured=false is one the hotel has not written "
                "down: say you will check, never that it is unavailable."
                if len(described) < 3 else
                "Quote the times and prices exactly as given."
            )
        ),
    }


# ── group rates ──────────────────────────────────────────────────────────────


class GroupArgs(BaseModel):
    units: int = Field(ge=1, le=500, description="How many rooms the guest wants.")
    room_type_id: ResourceId | None = Field(
        default=None, description="Which room, if they have chosen one.",
    )


@tool(
    name="get_group_rates",
    description=(
        "What changes when a booking is several rooms: the hotel's volume "
        "bands, the minimum that counts as a group, the deposit and when the "
        "balance is due. Use this the moment a guest says a number of rooms or "
        "of people — 'we are 20 people', '4 rooms chahiye'. It does not quote; "
        "get_quote does, and it already applies the band."
    ),
    input_model=GroupArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_group_rates(args: GroupArgs, ctx: AgentContext) -> dict:
    tiers = await rates.tiers_for(ctx.property_id)
    policy = await rates.group_policy_for(ctx.property_id)
    tier = rates.select_tier(tiers, args.units, args.room_type_id or "")

    is_group = args.units >= (policy.min_units if policy else 3)
    out: dict = {
        "rooms": args.units,
        "isGroup": is_group,
        "groupFrom": policy.min_units if policy else None,
        "depositPct": policy.deposit_pct if policy else None,
        "balanceDueDaysBefore": policy.payment_due_days if policy else None,
        "mealPackage": policy.meal_package if policy else None,
        "currency": "INR",
        # The whole ladder, so the agent can say "from six rooms it improves
        # again" — which is a sentence that sells another two rooms and that no
        # amount of prompt engineering can produce without the numbers.
        "bands": [
            {
                "fromRooms": t.min_units,
                "toRooms": t.max_units,
                "discountPct": t.discount_pct,
                "fixedNightlyPaise": t.fixed_price_paise,
                "negotiable": t.negotiable,
            }
            for t in tiers
        ],
    }

    if tier is None:
        out["instruction"] = (
            "This hotel has no volume pricing, or none that covers this many "
            "rooms. Quote the ordinary rate through get_quote. Do not invent a "
            "group discount."
        )
    elif tier.negotiable and not tier.is_quotable:
        out["instruction"] = (
            "This many rooms is a negotiated rate rather than a published one. "
            "Say you can check the best rate for them, then use negotiate_rate. "
            "Do not name a figure yet."
        )
    else:
        out["appliedBand"] = {"fromRooms": tier.min_units, "toRooms": tier.max_units}
        out["instruction"] = (
            "get_quote already applies this band — call it with the number of "
            "rooms and quote what comes back. Do not apply the discount yourself."
        )
    return out


# ── cancellation and payment terms ───────────────────────────────────────────


class TermsArgs(BaseModel):
    rate_plan_id: ResourceId | None = Field(
        default=None, description="The rate being discussed, from get_stay_options.",
    )


def _read_terms(property_id: str, rate_plan_id: str | None) -> tuple[dict, dict]:
    db = get_db()
    prop = (
        db.table("properties").select("settings,policies")
        .eq("id", property_id).limit(1).execute().data or [{}]
    )[0]
    plan = {}
    if rate_plan_id:
        rows = (
            db.table("rate_plans")
            .select("name,meal_plan,refundable,cancellation_cutoff_hours,cancellation_policy")
            .eq("id", rate_plan_id).eq("property_id", property_id).limit(1).execute().data or []
        )
        plan = rows[0] if rows else {}
    return prop, plan


@tool(
    name="get_cancellation_terms",
    description=(
        "What a guest gets back if they cancel, and by when. Structured, with "
        "each stage of the policy. Use this for any 'cancel karne pe paisa wapas "
        "milega?' — never answer it from the rate plan's name or from a house "
        "rule written in prose."
    ),
    input_model=TermsArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_cancellation_terms(args: TermsArgs, ctx: AgentContext) -> dict:
    prop, plan = await run_db(_read_terms, ctx.property_id, args.rate_plan_id)
    terms = policies.cancellation_terms(
        plan.get("cancellation_policy"),
        refundable=bool(plan.get("refundable", True)),
        cutoff_hours=plan.get("cancellation_cutoff_hours"),
    )
    out = terms.as_dict()
    out["ratePlan"] = plan.get("name")
    # The hotel's own words, where it has written them, alongside the structure.
    # A hotelier's sentence often carries a nuance the ladder cannot — "no
    # refunds during Diwali week" — and dropping it would be losing information
    # the guest is entitled to.
    out["houseNote"] = (prop.get("policies") or {}).get("cancellation")

    if terms.derived:
        out["instruction"] = (
            "The hotel has not set a staged policy, so this is read from whether "
            "the rate is refundable and its cut-off. Say it plainly and offer to "
            "confirm with the team if the guest is deciding on it."
        )
    elif terms.non_refundable:
        out["instruction"] = (
            "This rate is non-refundable. Say so before the guest pays, not "
            "after. If they want flexibility, offer a refundable rate from "
            "get_stay_options."
        )
    else:
        out["instruction"] = (
            "Give the tiers in the guest's own terms — how long they have for a "
            "full refund, and what happens after. Do not compute a rupee figure; "
            "the percentages are what you may state."
        )
    return out


@tool(
    name="get_payment_terms",
    description=(
        "How much a guest pays now to confirm, and how much at the hotel. Use "
        "this for 'kitna advance dena hoga?' and before sending a payment link. "
        "The rupee amounts for a specific stay come from get_quote — this is the "
        "rule, not the arithmetic."
    ),
    input_model=TermsArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_payment_terms(args: TermsArgs, ctx: AgentContext) -> dict:
    prop, _plan = await run_db(_read_terms, ctx.property_id, args.rate_plan_id)
    policy = negotiation.resolve(prop.get("settings"))
    group_policy = await rates.group_policy_for(ctx.property_id)

    return {
        "depositPct": policy.token_pct,
        "groupDepositPct": group_policy.deposit_pct if group_policy else None,
        "groupFromRooms": group_policy.min_units if group_policy else None,
        "balanceDueAt": "check-in",
        "currency": "INR",
        "instruction": (
            "State the percentage. For a rupee figure, use get_quote — its "
            "tokenPaise is the deposit for that stay and its balancePaise is "
            "what is left. Never work either out yourself. Say nothing about "
            "what the platform charges the hotel; that is not the guest's "
            "business."
        ),
    }


# ── experiences ──────────────────────────────────────────────────────────────


def _read_experiences(property_id: str) -> list[dict]:
    return (
        get_db().table("experiences").select("*")
        .eq("property_id", property_id).eq("is_active", True)
        .neq("availability", "not_available")
        .order("sort_order").limit(50).execute().data or []
    )


@tool(
    name="get_experiences",
    description=(
        "What guests can *do* here, as opposed to order: treks, boat rides, "
        "bonfires, food walks. Each with its price, how long it takes, where it "
        "starts, the party size and any age limit. Use it when a guest asks what "
        "there is to do, and when recommending something alongside a room."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_experiences(args: NoArgs, ctx: AgentContext) -> dict:
    rows = await run_db(_read_experiences, ctx.property_id)
    if not rows:
        return {
            "found": False,
            "instruction": (
                "This hotel has not listed any experiences. Do not invent one. "
                "You may still mention services from list_services, and offer to "
                "ask the team what is on locally."
            ),
        }
    return {
        "found": True,
        "experiences": [
            {
                "id": str(r["id"]),
                "title": r.get("title"),
                "description": r.get("description"),
                "pricePaise": r.get("price_paise"),
                "priceUnit": r.get("price_unit"),
                "durationMinutes": r.get("duration_minutes"),
                "meetingPoint": r.get("meeting_point"),
                "startsAt": str(r["starts_at"])[:5] if r.get("starts_at") else None,
                "minParticipants": r.get("min_participants"),
                "maxParticipants": r.get("max_participants"),
                "minAge": r.get("min_age"),
                "requirements": r.get("requirements"),
                "availability": r.get("availability"),
                "bookAheadHours": r.get("lead_time_hours"),
                "photoCount": len(r.get("photos") or []),
            }
            for r in rows
        ],
        "currency": "INR",
        "instruction": (
            "Quote pricePaise with its priceUnit — per person and per group are "
            "very different numbers and the guest needs to know which. Mention "
            "minParticipants where there is one; a couple cannot book something "
            "that needs six."
        ),
    }


# ── the negotiation ──────────────────────────────────────────────────────────


class NegotiateArgs(BaseModel):
    rate_plan_id: ResourceId = Field(description="From get_stay_options.")
    check_in: date
    check_out: date
    units: int = Field(default=1, ge=1, le=200, description="How many rooms.")
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)
    requested_nightly_rupees: int | None = Field(
        default=None, ge=0, le=10_000_000,
        description=(
            "The figure the guest asked for, per room per night, in rupees, "
            "exactly as they said it. Do not convert it into a discount and do "
            "not adjust it. Omit it entirely if they asked for 'your best price' "
            "without naming one."
        ),
    )


def _sentence(decision, quote) -> str:
    """
    What the model is instructed to do with the verdict.

    Instructions rather than a script: the reply has to be in the guest's
    language and in the agent's voice, and a canned sentence would be neither.
    What is fixed is which *number* may be said, which is the part that must not
    be left to judgement.
    """
    if decision.verdict == "allowed":
        return (
            "This is agreed. Quote totalPaise from the quote and say the hold "
            "lasts 15 minutes. Do not describe it as a special or a favour "
            "unless the guest asked for a discount."
        )
    if decision.verdict == "counter":
        base = (
            "You cannot do the figure they asked for. Say so directly, then "
            "offer counterNightlyPaise per night — that number and no other. Do "
            "not explain why, do not mention limits, minimums or policies, and "
            "never say it is the lowest you can go."
        )
        return base + (
            " If they turn it down, you may offer to ask the hotel — use "
            "propose_rate_exception."
            if decision.approval_available else
            " If they turn it down, offer a cheaper room or different dates."
        )
    if decision.verdict == "needs_approval":
        return (
            "A person has to agree to this. Tell the guest you are checking with "
            "the hotel and will come back to them. Name NO figure at all — not "
            "theirs, not a counter. Then use propose_rate_exception."
        )
    return {
        "negotiation_closed": "These rates are fixed. Say so plainly and quote the published price.",
        "blackout_dates": "Those dates are excluded from any offer. Say the rate is fixed for those nights.",
        "outside_valid_dates": "No offer is running for those dates. Quote the published price.",
        "outside_valid_days": "No offer is running on those days. Quote the published price.",
        "below_minimum_nights": "The stay is too short for an offer. Quote the published price; you may mention a longer stay does better if that is true of the bands.",
        "below_minimum_rooms": "Too few rooms for an offer. Quote the published price.",
        "not_enough_rooms": "There are not enough rooms free. Say that first — the price is beside the point.",
        "no_rate_available": "There is no open rate for those dates. Offer other dates.",
        "invalid_stay": "Check the dates with the guest.",
    }.get(decision.reason, "No offer is available. Quote the published price.")


@tool(
    name="negotiate_rate",
    description=(
        "Ask the hotel's own rules what can be done about a price. Use this the "
        "moment a guest pushes back on a rate or asks for a better one — "
        "'₹4,000 mein ho jayega?', 'best price kya doge?'. Pass their figure as "
        "they said it. You get back one of: agreed (with a real quote), a "
        "counter-offer you may name, needs-approval (name no figure), or a "
        "refusal with the reason. Never work out a discount yourself and never "
        "state a number this tool did not give you."
    ),
    input_model=NegotiateArgs,
    permission="rates",
    # Level 3, the same as get_quote and for the same reason: when the answer is
    # yes this takes a hold, and a hold is a room somebody else cannot book for
    # the next fifteen minutes.
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_quote",
    result_type="quote",
    creates_result=True,
)
async def negotiate_rate(args: NegotiateArgs, ctx: AgentContext) -> tuple[dict, str | None]:
    if args.check_out <= args.check_in:
        raise OutOfScope(
            "check_out must be after check_in",
            "Could you confirm the dates — which night would you arrive?",
        )

    try:
        decision = await negotiation.negotiate(
            property_id=ctx.property_id,
            rate_plan_id=args.rate_plan_id,
            check_in=args.check_in,
            check_out=args.check_out,
            units=args.units,
            requested_nightly_paise=(
                args.requested_nightly_rupees * 100
                if args.requested_nightly_rupees is not None else None
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # Never fall back to an estimate, for the same reason `get_quote` never
        # does: an engine that is down means the agent does not know what it may
        # offer, and offering anything at that point is the failure this whole
        # design exists to prevent.
        raise ToolUnavailable(
            f"negotiation failed: {type(exc).__name__}: {exc}",
            "I can't work out our best rate this second — let me put you through "
            "to the team rather than quote you something wrong.",
        ) from exc

    payload = decision.as_dict()
    payload["currency"] = "INR"
    # What the guest asked for, returned rather than dropped.
    #
    # Not for the model's benefit — it just said it — but for the reply
    # guardrail's. "₹4,000 mein nahi ho payega, ₹4,300 kar sakta hoon" is the
    # ordinary shape of a counter-offer, and the first of those two figures came
    # from the guest. With nothing in the run holding it, the guardrail read a
    # price nobody computed and turned a normal haggle into a handoff.
    #
    # Deliberately narrow: it is backed on turns where the engine was actually
    # asked about that number, and nowhere else. A figure a guest writes is not
    # a figure the agent may repeat back for free.
    if args.requested_nightly_rupees is not None:
        payload["requestedNightlyPaise"] = args.requested_nightly_rupees * 100

    # The agent's own ceiling, applied on top of the property's.
    #
    # `agents.limits.max_discount_pct` is what *this* agent may grant unattended
    # and may be stricter than what the hotel permits its staff. An allowance the
    # agent does not have turns a yes into an escalation rather than into a
    # cheaper quote — which is the conservative direction and the same one
    # `get_quote` and `propose_rate_exception` already take.
    agent_ceiling = ctx.limits.get("max_discount_pct")
    if (
        decision.verdict == "allowed"
        and agent_ceiling is not None
        and (decision.discount_pct or 0) > int(agent_ceiling)
    ):
        payload["verdict"] = "needs_approval"
        payload["reason"] = "above_agent_ceiling"
        payload.pop("finalTotalPaise", None)
        payload.pop("finalNightlyPaise", None)
        payload["approvalAvailable"] = True
        payload["instruction"] = (
            "This is beyond what you may agree on your own. Tell the guest you "
            "are checking with the hotel and name no figure. Then use "
            "propose_rate_exception."
        )
        return payload, None

    if decision.verdict != "allowed":
        payload["instruction"] = _sentence(decision, None)
        return payload, None

    # Agreed — so it becomes a real quote rather than a sentence.
    #
    # `discount_pct` is the percentage the engine worked out to land exactly on
    # `final_total_paise`, and `create_quote` clamps it again against the
    # property's own ceilings and floor. Two independent computations of the same
    # number, and the quote's is authoritative: if they ever disagree, the guest
    # is told the quote's figure, which is the one that was actually charged.
    try:
        quote = await booking_service.create_quote(
            property_id=ctx.property_id,
            rate_plan_id=args.rate_plan_id,
            check_in=args.check_in,
            check_out=args.check_out,
            units=args.units,
            adults=args.adults,
            children=args.children,
            requested_discount_pct=decision.discount_pct or 0,
            discount_reason="negotiation" if decision.discount_pct else None,
            issued_by=ACTOR_QUOTE,
            conversation_id=ctx.conversation_id,
            audience="public",
        )
    except Exception as exc:  # noqa: BLE001
        raise ToolUnavailable(
            f"quoting a negotiated rate failed: {type(exc).__name__}: {exc}",
            "I can't hold that rate this second — let me get the team to confirm "
            "it for you.",
        ) from exc

    priced = quote.priced
    payload.update({
        "quoteId": quote.id,
        "expiresAt": quote.expires_at.isoformat(),
        "totalPaise": priced.total_paise,
        "taxPaise": priced.tax_paise,
        "tokenPaise": priced.token_paise,
        "balancePaise": priced.balance_paise,
        # Written out, so the one number a haggling guest is waiting for is
        # never the output of the model dividing by a hundred.
        "total": money.display(priced.total_paise),
        "token": money.display(priced.token_paise),
        "balance": money.display(priced.balance_paise),
        "nights": len(priced.nights),
        "rooms": args.units,
        # What the quote actually came to per room per night, which is the number
        # the guest asked about and the one the model should say.
        "agreedNightlyPaise": (
            round(priced.total_paise / len(priced.nights) / args.units)
            if priced.nights and args.units else None
        ),
    })
    payload["agreedNightly"] = money.display(payload["agreedNightlyPaise"])
    payload["instruction"] = _sentence(decision, quote)
    return payload, quote.id


# ── reaching the hotel's own team ────────────────────────────────────────────


class EscalateArgs(BaseModel):
    kind: str = Field(
        description=(
            "What this is about, one of: discount_request, group_inquiry, "
            "special_request, transfer_request, payment_issue, "
            "cancellation_exception, other. Pick from what the guest wants, not "
            "from how urgent it feels."
        ),
    )
    reason: str = Field(
        max_length=400,
        description=(
            "One sentence on what the guest actually said, in their own terms. "
            "Do NOT include prices, dates or room names — those are read from "
            "the booking context and put in the message for you."
        ),
    )
    quote_id: ResourceId | None = Field(
        default=None,
        description="The live quote this is about, from get_quote or negotiate_rate.",
    )
    rate_plan_id: ResourceId | None = Field(default=None)
    check_in: date | None = None
    check_out: date | None = None
    units: int | None = Field(default=None, ge=1, le=500)
    requested_nightly_rupees: int | None = Field(
        default=None, ge=0, le=10_000_000,
        description="What the guest asked for, if they named a figure. In rupees, as they said it.",
    )


_ESCALATION_KINDS = frozenset({
    "discount_request", "group_inquiry", "special_request",
    "transfer_request", "payment_issue", "cancellation_exception", "other",
})


def _read_escalation_context(property_id: str, quote_id: str | None,
                             rate_plan_id: str | None) -> dict:
    db = get_db()
    prop = (
        db.table("properties").select("name,partner_id")
        .eq("id", property_id).limit(1).execute().data or [{}]
    )[0]
    out = {"hotel": prop.get("name") or "Your hotel", "partner_id": prop.get("partner_id")}

    if quote_id:
        rows = (
            db.table("quotes")
            .select("id,room_type_id,rate_plan_id,check_in,check_out,units,adults,"
                    "children,total_paise,nightly_paise,status")
            .eq("id", quote_id).eq("property_id", property_id).limit(1).execute().data or []
        )
        if rows:
            out["quote"] = rows[0]
            rate_plan_id = rate_plan_id or rows[0].get("rate_plan_id")

    if rate_plan_id:
        rows = (
            db.table("rate_plans").select("id,room_type_id,name")
            .eq("id", rate_plan_id).eq("property_id", property_id).limit(1).execute().data or []
        )
        if rows:
            out["plan"] = rows[0]
            names = (
                db.table("room_types").select("name")
                .eq("id", rows[0]["room_type_id"]).limit(1).execute().data or []
            )
            if names:
                out["room_name"] = names[0].get("name")
    return out


@tool(
    name="escalate_to_team",
    description=(
        "Ask the hotel's own team about something you cannot decide: a discount "
        "beyond what you may give, a group rate, a cancellation exception, a "
        "special arrangement. They are messaged on WhatsApp with the full "
        "context. Tell the guest you are checking with the hotel — never that "
        "it is agreed. Asking twice about the same thing does nothing, so if you "
        "have already escalated, say you are still waiting."
    ),
    input_model=EscalateArgs,
    permission="messaging",
    # Level 2: it writes to somebody outside the conversation. Below `get_quote`,
    # because it moves no stock and commits no money — the hotel's own team
    # reading a message is the least consequential outbound act in the product.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="escalation",
    creates_result=True,
    scoped_to_conversation=True,
)
async def escalate_to_team(args: EscalateArgs, ctx: AgentContext) -> tuple[dict, str | None]:
    if args.kind not in _ESCALATION_KINDS:
        raise OutOfScope(
            f"unknown escalation kind {args.kind!r}",
            "Let me check with the team about that.",
        )
    if not ctx.conversation_id:
        raise OutOfScope(
            "escalation requires a conversation",
            "Let me put you through to the team.",
        )

    # Already asked? Answered before anything is sent, so a guest who repeats
    # themselves does not put a second message on a manager's phone.
    state = await escalation_service.state_for(ctx.conversation_id)

    context = await run_db(
        _read_escalation_context, ctx.property_id, args.quote_id, args.rate_plan_id,
    )
    quote = context.get("quote") or {}

    # Everything numeric comes from the quote and the rules. The model supplied
    # a sentence; it did not supply a single figure in the message the manager
    # will read, which is the property that makes an approval safe to act on.
    facts: dict = {
        "guest_name": ctx.guest_name,
        "channel": ctx.channel,
        "room_type_id": quote.get("room_type_id"),
        "rate_plan_id": quote.get("rate_plan_id") or args.rate_plan_id,
        "room_name": context.get("room_name"),
        "check_in": str(quote.get("check_in") or args.check_in or "") or None,
        "check_out": str(quote.get("check_out") or args.check_out or "") or None,
        "units": quote.get("units") or args.units,
        "guests": (
            (quote.get("adults") or 0) + (quote.get("children") or 0)
            if quote else None
        ),
        "total_paise": quote.get("total_paise"),
        "requested_paise": (
            args.requested_nightly_rupees * 100
            if args.requested_nightly_rupees is not None else None
        ),
    }

    # The published nightly and the floor, for the manager's eyes only. Read
    # here rather than passed in, and deliberately absent from every projection
    # this function returns.
    if facts["rate_plan_id"] and facts["check_in"] and facts["check_out"]:
        try:
            rules = await negotiation.rules_for(
                ctx.property_id,
                rate_plan_id=str(facts["rate_plan_id"]),
                room_type_id=str(facts["room_type_id"]) if facts.get("room_type_id") else None,
            )
            nights = (date.fromisoformat(facts["check_out"])
                      - date.fromisoformat(facts["check_in"])).days
            units = int(facts.get("units") or 1)
            if quote.get("total_paise") and nights > 0:
                facts["published_paise"] = round(
                    int(quote["total_paise"]) / nights / max(units, 1)
                )
            gross = int(quote.get("gross_paise") or quote.get("total_paise") or 0)
            floor = negotiation.floor_total_for(gross, rules, nights, units)
            if floor and nights > 0:
                facts["floor_paise"] = round(floor / nights / max(units, 1))
        except Exception:  # noqa: BLE001
            # A manager can decide without the floor line; they cannot decide if
            # the escalation never arrives. Never fatal.
            logger.warning("could not resolve rules for escalation", exc_info=True)

    result = await escalation_service.raise_escalation(
        property_id=ctx.property_id,
        partner_id=str(context.get("partner_id")),
        kind=args.kind,
        facts=facts,
        reason=args.reason,
        hotel_name=context.get("hotel") or "Your hotel",
        conversation_id=ctx.conversation_id,
        quote_id=args.quote_id,
    )

    payload = {
        "escalationId": str(result["id"]),
        "kind": args.kind,
        "status": result.get("status"),
        "alreadyAsked": bool(result.get("duplicate")),
        "teamNotified": bool(result.get("notified")),
        "otherOpen": len(state.get("open") or []),
    }
    if result.get("duplicate"):
        payload["instruction"] = (
            "You have already asked the hotel about exactly this and they have "
            "not come back yet. Tell the guest you are still waiting — do not "
            "ask again, and do not imply this is a new request."
        )
    elif result.get("notified"):
        payload["instruction"] = (
            "The team has been messaged. Tell the guest you are checking with "
            "the hotel and will come back to them. Name no figure and give no "
            "timeline you were not given."
        )
    else:
        payload["instruction"] = (
            "This is recorded for the hotel but nobody could be messaged right "
            "now. Do not tell the guest the hotel has been contacted. Say you "
            "are putting them through to the team, and use request_human_handoff."
        )
    return payload, str(result["id"])
