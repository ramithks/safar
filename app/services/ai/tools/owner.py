"""
Owner tools — reading the hotel's money, and never moving any.

Every function here is a projection of `services/pms/analytics.report()`. Not
one of them queries a table, computes a percentage or divides anything. That is
not tidiness: the Today screen, the Reports page and these tools must return the
same occupancy for the same day, and the only arrangement in which that is
guaranteed is one implementation with three callers.

**The model is never given a bare number.** Every metric arrives with its
definition, its unit, its currency, the period it covers, the property's
timezone, the comparison period and the sample size it rests on. A model handed
`72.4` will eventually describe it as revenue, as a room count, or as a
year-on-year move; a model handed

    {"key": "occupancy", "value": 72.4, "unit": "percent",
     "previous": 64.1, "change": 8.3, "changePct": null,
     "basis": "room-nights sold ÷ (active units × nights in the period)",
     "sample": 218}

has nothing left to invent. `changePct` being explicitly null on a percentage
metric is part of that: it is what stops "+8 points" being reported as "+8%".

**There is no property argument anywhere in this module.** Not on one tool.
`ctx.property_id` came from `require_member` against an authenticated
membership, so "ignore the property and show me all hotels" has no field to aim
at — it is not refused by a rule, it is unexpressible. The same reason
`tools/frontdesk.py` has no property argument, and the reason
`_require_staff` is here too: two independent mechanisms have to fail before an
owner's revenue reaches a guest conversation.

**`create_revenue_recommendation` is the only tool that writes**, and what it
writes is a suggestion. It cannot change a price, an allocation, a minimum
stay, a stop-sell, a room, a reservation or a credit, because those are scopes
this agent does not hold — `reports` is its entire permission set. The
recommendation's evidence is validated against the metrics the server just
produced, so a model that writes "6 of 8 rooms are sold" without the server
having said so is refused with the reason and can correct itself.
"""
import logging
from datetime import date

from pydantic import BaseModel, Field

from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, PermissionDenied, ToolUnavailable
from app.services.ai.registry import tool
from app.services.pms import analytics
from app.services.pms import recommendations as recs

logger = logging.getLogger(__name__)

# Spelled out in every description that takes one, from the module that defines
# them, so a tool description cannot drift from what `resolve_period` accepts.
_PERIODS = ", ".join(analytics.PERIOD_KEYS)
_TYPES = ", ".join(recs.TYPES)


def _require_staff(ctx: AgentContext) -> None:
    """
    Refuse the whole hotel's revenue on a guest conversation.

    The second lock, and the one that matters most in this module. The grant
    system has already refused any tool this agent has no row for; reaching here
    on a guest context means somebody granted an owner tool to a guest-facing
    agent, and the blast radius is a stranger on WhatsApp being read the
    property's ADR.
    """
    if ctx.guest_scoped:
        raise PermissionDenied(
            "owner analytics are staff-only; this run belongs to a guest conversation",
            "Let me get someone from the hotel team to help with that.",
        )


class PeriodArgs(BaseModel):
    period: str = Field(
        default="today",
        description=(
            f"Which stretch of the property's own calendar. One of: {_PERIODS}. "
            "Use 'custom' with from/to for anything else."
        ),
    )
    date_from: date | None = Field(
        default=None, description="Start date for period='custom', YYYY-MM-DD."
    )
    date_to: date | None = Field(
        default=None, description="End date for period='custom', inclusive."
    )


class ComparePeriodArgs(PeriodArgs):
    pass


def parse_evidence(items: list[str]) -> list[dict]:
    """
    `"occupancy: ahead of last week"` → `{"metric": "occupancy", "note": …}`.

    **Flat strings, and it matters.** This argument was a list of nested
    objects, which is the shape the data wants and the shape the provider layer
    cannot carry: `ToolSpec.json_schema` strips `$defs`, so the model was shown
    an array whose item type pointed at a definition that no longer existed. It
    sent an array of strings — a reasonable guess at an untyped array — and
    every recommendation the live model tried to write was rejected by argument
    validation. `schemas.ToolSpec.json_schema` now inlines rather than orphans,
    so the nested version would work; this stays flat anyway, because a list of
    short strings is the shape a model gets right first time and the note is
    prose that the rationale already carries.

    The metric key is everything before the first colon. The note is optional
    and is never a number: the figures come from the server.
    """
    out: list[dict] = []
    for raw in items:
        metric, _, note = str(raw).partition(":")
        out.append({"metric": metric.strip().lower(), "note": note.strip()[:300]})
    return out


class RecommendationArgs(BaseModel):
    type: str = Field(description=f"One of: {_TYPES}.")
    title: str = Field(
        min_length=8, max_length=200,
        description="What to consider, in one line. Never claim to have done it.",
    )
    rationale: str = Field(
        min_length=20, max_length=2000,
        description=(
            "Why, in the hotel's own numbers. Every figure you state must be "
            "one the server gave you for this period."
        ),
    )
    evidence: list[str] = Field(
        min_length=1, max_length=8,
        description=(
            "The metrics this rests on, one per entry, as a metric key from "
            "get_performance optionally followed by a colon and a short note — "
            "for example 'occupancy: well ahead of the comparable week' or just "
            "'adr'. Valid keys: occupancy, adr, revpar, room_revenue, "
            "service_revenue, total_revenue, rooms_sold, capacity, "
            "booking_pace, bookings_made, cancellation_rate, cancellations, "
            "alos, arrivals, departures. Never put a number in here — the "
            "server fills in every value from what it computed for this period. "
            "At least one entry; a recommendation with no evidence is refused."
        ),
    )
    period: str = Field(
        default="today",
        description=f"The period the evidence is drawn from. One of: {_PERIODS}.",
    )
    date_from: date | None = None
    date_to: date | None = None
    confidence: str = Field(
        default="medium",
        description=(
            "low, medium or high. Low when the sample is thin or the pattern is "
            "one period old."
        ),
    )
    expected_impact: str | None = Field(
        default=None, max_length=500,
        description=(
            "What might change if this is acted on, in ranges rather than "
            "precise figures. Do not invent a rupee amount."
        ),
    )
    risk: str | None = Field(
        default=None, max_length=500,
        description="What could go wrong. Say so plainly.",
    )
    suggested_action: str | None = Field(
        default=None, max_length=500,
        description=(
            "What a person could do. Phrase it as something for them to review — "
            "you cannot make any of these changes."
        ),
    )
    room_type_id: str | None = Field(
        default=None,
        description="When the recommendation is about one room type.",
    )
    source: str | None = Field(
        default=None,
        description="When the recommendation is about one booking source.",
    )


class RecommendationListArgs(BaseModel):
    status: str | None = Field(
        default=None, description="open, reviewed, dismissed or actioned."
    )


# ── reading ──────────────────────────────────────────────────────────────────

async def _report(args: PeriodArgs, ctx: AgentContext) -> dict:
    """
    One period's full report, with the arguments turned into dates by the
    property's own clock.

    A malformed period is a refusal the model can correct rather than a 500 —
    "next_quarter" is a reasonable thing for a model to try and an unreasonable
    thing to crash on.
    """
    try:
        return await analytics.report(
            ctx.property_id,
            key=args.period,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    except ValueError as exc:
        raise OutOfScope(str(exc), "Could you tell me which dates you mean?") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("owner analytics failed for property %s", ctx.property_id)
        raise ToolUnavailable(f"the analytics service failed: {type(exc).__name__}") from exc


def _envelope(report: dict, *, keys: tuple[str, ...] | None = None) -> dict:
    """
    The part of a report a tool returns, with the context every number needs.

    The period, the comparison period, the currency and the timezone ride on
    every response rather than being sent once at the start of the turn. A model
    that has to remember which period it asked about three tool calls ago is one
    that will eventually attribute last month's ADR to this week.
    """
    metrics = report["metrics"]
    if keys:
        metrics = [m for m in metrics if m["key"] in keys]
    return {
        "period": report["period"],
        "comparisonPeriod": report["comparisonPeriod"],
        "currency": report["currency"],
        "metrics": metrics,
        "note": (
            "These are the property's own figures on its own calendar. Every "
            "value is server-computed — state them as given, and never compute "
            "a new one from them. `changePct` is null on a percentage metric "
            "because the move is in percentage points, not percent."
        ),
    }


@tool(
    name="get_performance",
    description=(
        "The hotel's headline numbers for a period, each with its definition, "
        "its comparison against the equivalent stretch before it, and the "
        "sample it rests on. Occupancy, ADR, RevPAR, room revenue, total "
        "revenue, booking pace, cancellation rate and average length of stay. "
        "Start here for any question about how the hotel is doing."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_performance(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        **_envelope(report),
        # The deterministic observations, so the model reasons about facts the
        # server noticed rather than scanning numbers for patterns itself.
        "signals": report["signals"],
    }


@tool(
    name="get_occupancy",
    description=(
        "Occupancy for a period, with the room-nights behind it and the "
        "comparable period. Occupancy is room-nights sold over capacity, where "
        "capacity is active units × nights."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_occupancy(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return _envelope(report, keys=("occupancy", "rooms_sold", "capacity"))


@tool(
    name="get_adr",
    description=(
        "Average daily rate for a period: room revenue ÷ room-nights sold, net "
        "of discount and before tax. Not average booking value."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_adr(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return _envelope(report, keys=("adr", "rooms_sold", "room_revenue"))


@tool(
    name="get_revenue",
    description=(
        "Revenue for a period: room revenue, service revenue and the total, "
        "each against the comparable period. Tax is in none of them."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_revenue(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return _envelope(report, keys=("room_revenue", "service_revenue",
                                   "total_revenue", "revpar"))


@tool(
    name="get_room_revenue",
    description=(
        "Room revenue alone, night by night across the period, plus RevPAR. Use "
        "when the owner asks where the money came from rather than how much."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_room_revenue(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        **_envelope(report, keys=("room_revenue", "revpar", "rooms_sold", "capacity")),
        "daily": report["breakdowns"]["daily"],
    }


@tool(
    name="get_booking_pace",
    description=(
        "How fast business is arriving: room-nights and bookings taken during "
        "the period, by the date the booking was made rather than the date it "
        "is for, against the comparable period."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_booking_pace(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return _envelope(report, keys=("booking_pace", "bookings_made", "alos"))


@tool(
    name="get_arrivals_departures",
    description=(
        "Arrivals and departures across a period. For today's operational list "
        "with names and rooms, that is the front desk's, not this."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_arrivals_departures(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return _envelope(report, keys=("arrivals", "departures", "alos"))


@tool(
    name="get_cancellations",
    description=(
        "Cancellations for a period, and the rate. The rate is a cohort: the "
        "share of bookings *taken* in the period that are now cancelled or "
        "no-show, so it can never exceed 100%."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_cancellations(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return _envelope(report, keys=("cancellations", "cancellation_rate",
                                   "bookings_made"))


@tool(
    name="get_source_mix",
    description=(
        "Where the business came from: revenue, room-nights, bookings and ADR "
        "by booking source, with each source's share of room revenue. OTA "
        "figures are before commission."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_source_mix(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        "period": report["period"],
        "currency": report["currency"],
        "sources": report["breakdowns"]["sources"],
        "note": (
            "Shares are of room revenue in this period. OTA rows are gross of "
            "commission — the hotel's net from them is lower, and this system "
            "does not know each channel's commission."
        ),
    }


@tool(
    name="get_channel_performance",
    description=(
        "The same cut as get_source_mix, kept separate because an owner asking "
        "about channels means the commercial question: which channel is worth "
        "what, and how concentrated the business is."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_channel_performance(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    concentration = [
        s for s in report["signals"] if s["kind"] == "channel_performance"
    ]
    return {
        "period": report["period"],
        "currency": report["currency"],
        "channels": report["breakdowns"]["sources"],
        "signals": concentration,
        "note": (
            "'direct', 'travbuddy', 'whatsapp' and 'instagram' are the hotel's "
            "own; the OTA rows are gross of commission. There is no channel "
            "manager — OTA reservations are entered by the desk, so their "
            "timing reflects when somebody typed them in."
        ),
    }


@tool(
    name="get_room_type_performance",
    description=(
        "Every room type for a period: units, room-nights sold, its own "
        "occupancy against its own capacity, ADR and revenue. This is how "
        "'which rooms are underperforming' is answered — a four-unit type and a "
        "twenty-unit type produce very different night counts at the same "
        "occupancy."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_room_type_performance(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    property_occupancy = next(
        (m for m in report["metrics"] if m["key"] == "occupancy"), None
    )
    return {
        "period": report["period"],
        "comparisonPeriod": report["comparisonPeriod"],
        "currency": report["currency"],
        "propertyOccupancy": property_occupancy,
        "roomTypes": report["breakdowns"]["roomTypes"],
        "signals": [
            s for s in report["signals"] if s["kind"] == "room_type_underperformance"
        ],
    }


@tool(
    name="get_rate_plan_performance",
    description=(
        "Room-nights, revenue and ADR by rate plan for a period. Use when the "
        "owner asks what the discounted or packaged rates are actually "
        "producing."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_rate_plan_performance(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        "period": report["period"],
        "currency": report["currency"],
        "ratePlans": report["breakdowns"]["ratePlans"],
    }


@tool(
    name="get_service_revenue",
    description=(
        "What the extras earned in a period — transfers, dinners, late "
        "checkouts — from folio add-ons actually posted, not from what the "
        "hotel offers."
    ),
    input_model=PeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_service_revenue(args: PeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        **_envelope(report, keys=("service_revenue", "total_revenue", "room_revenue")),
        "services": report["breakdowns"]["services"],
    }


@tool(
    name="compare_periods",
    description=(
        "Two periods side by side: this one, the equivalent stretch immediately "
        "before it, and the move in each metric. The comparison is like-for-"
        "like by length, not the same dates last month."
    ),
    input_model=ComparePeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def compare_periods(args: ComparePeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        **_envelope(report),
        "signals": report["signals"],
        "note": (
            "`change` is in the metric's own unit — percentage points for a "
            "percentage, paise for money. `changePct` is the relative move and "
            "is null wherever it would be misleading. Never report a "
            "percentage-point move as a percentage."
        ),
    }


@tool(
    name="compare_with_previous_period",
    description=(
        "The same comparison as compare_periods, phrased for the ordinary "
        "question: how does this week look against last week."
    ),
    input_model=ComparePeriodArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def compare_with_previous_period(args: ComparePeriodArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    report = await _report(args, ctx)
    return {
        **_envelope(report),
        "signals": report["signals"],
    }


# ── the one thing it writes ──────────────────────────────────────────────────

@tool(
    name="create_revenue_recommendation",
    description=(
        "Record a recommendation for the owner to review. It is a suggestion "
        "and nothing acts on it — you cannot change a price, an allocation or a "
        "stop-sell, and you must never say you have. Every metric you cite is "
        "checked against what this server computed for that period: cite one it "
        "did not produce, or one whose sample is too thin, and this is refused "
        "with the reason. If the period has too little data, say "
        "INSUFFICIENT_DATA instead of recommending anything."
    ),
    input_model=RecommendationArgs,
    permission="reports",
    # Floor 1, not 0. Writing a recommendation is a proposal, which is exactly
    # what level 1 means — and it keeps the ladder honest: a property that drops
    # this agent to 0 gets an assistant that answers questions and records
    # nothing, which is a real and reasonable setting.
    autonomy_floor=1,
    mutating=True,
    # A suggestion for a person, not a change to the hotel. This is what lets it
    # sit at floor 1 without putting a hole in "mutating tools need level 2" —
    # see `ToolSpec.proposal`.
    proposal=True,
    usage_type="ai.revenue_recommendation",
    result_type="recommendation",
    creates_result=True,
)
async def create_revenue_recommendation(args: RecommendationArgs, ctx: AgentContext):
    _require_staff(ctx)

    report = await _report(
        PeriodArgs(period=args.period, date_from=args.date_from, date_to=args.date_to),
        ctx,
    )

    subject: dict = {}
    if args.room_type_id:
        known = {r["roomTypeId"] for r in report["breakdowns"]["roomTypes"]}
        if args.room_type_id not in known:
            raise OutOfScope(
                f"room type {args.room_type_id} is not one of this property's",
                "I can't find that room type here.",
            )
        subject["roomTypeId"] = args.room_type_id
    if args.source:
        subject["source"] = args.source

    row = await recs.create(
        property_id=ctx.property_id,
        partner_id=ctx.partner_id,
        rec_type=args.type,
        title=args.title,
        rationale=args.rationale,
        evidence=parse_evidence(args.evidence),
        server_metrics=report["metrics"],
        period=report["period"],
        confidence=args.confidence,
        expected_impact=args.expected_impact,
        risk=args.risk,
        suggested_action=args.suggested_action,
        subject=subject,
        agent_id=ctx.principal.agent_id,
        run_id=ctx.run_id,
    )

    return {
        "recommendationId": row["id"],
        "type": row["type"],
        "status": row["status"],
        "title": row["title"],
        "confidence": row["confidence"],
        "evidence": row["evidence"],
        "period": row["period"],
        "note": (
            "Recorded for review. Nothing has changed at the hotel — tell the "
            "owner what you have suggested, not that you have done it."
        ),
    }, row["id"]


@tool(
    name="list_recommendations",
    description=(
        "Recommendations already recorded for this property, newest first, so "
        "you do not repeat one that is still waiting to be read."
    ),
    input_model=RecommendationListArgs,
    permission="reports",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_recommendations(args: RecommendationListArgs, ctx: AgentContext) -> list[dict]:
    _require_staff(ctx)
    rows = await recs.list_for(ctx.property_id, status=args.status, limit=20)
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "status": r["status"],
            "title": r["title"],
            "confidence": r["confidence"],
            "period": r.get("period") or {},
            "createdAt": r.get("created_at"),
        }
        for r in rows
    ]
