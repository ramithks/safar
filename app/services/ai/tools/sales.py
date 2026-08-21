"""
Selling — the two things the Sales agent needs that nothing had yet.

**Almost nothing is here, and that is the finding.** The Sales agent's job is
understand demand → find inventory → explain options → quote → negotiate → hold
→ book → follow up → hand off, and every step of that already had a tool built
for the Guest agent, calling a PMS service built for the front desk:

    find inventory      search_availability, get_stay_options
    explain options     get_room_types, list_services, get_property_policies
    quote and hold      get_quote          — one call; the hold is not separable
    book                create_booking     — from a quote id and nothing else
    give stock back     release_hold
    follow up           create_task
    stop                request_human_handoff

Adding sales versions of those would have been adding a second answer to "can I
sell this room and for how much", which is the one question this architecture
exists to have a single answer to.

What was genuinely missing is *negotiation*, and it is missing in two halves.

**The agent could not see the rules it was being judged by.** `price_stay()`
clamps a discount to the property's floor, silently and correctly, and the agent
found out by reading the number that came back. So it either lowballed — never
offering what it was allowed to offer — or promised 20% and delivered 15%, which
is worse than either. `get_negotiation_policy` is a read: here is what you may
grant, here is what a manager may authorise, here are the reasons.

**There was no way to ask for more.** The clamp is the whole mechanism, and a
clamp has no escalation path: a guest who says "another hotel quoted less" got
the floor, phrased apologetically, forever. `propose_rate_exception` is that
path — a quote at a discount above the automatic floor, refused outright if the
property has not authorised one, queued for a human if it has, and clamped
*again* on execution against the property's approval ceiling.

Three properties of that second tool are worth stating, because between them
they are why an AI is allowed near a price at all:

  * **The precheck runs before the autonomy gate.** A discount the property
    never authorised is refused while the guest is still in the conversation,
    not queued for a manager to refuse later. See `registry.execute`.
  * **The model never types a price.** It types a *percentage request* and a
    reason, both validated against configuration; every rupee still comes out of
    `create_quote` → `price_stay`.
  * **Approval raises the ceiling, it does not remove it.** An approval row
    carrying 90% produces a quote at the property's approval ceiling. The
    manager's judgement is a second input to a clamp, not a bypass of one.
"""
from datetime import date

from pydantic import BaseModel, Field

from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_QUOTE
from app.services.pms import booking as booking_service
from app.services.pms import negotiation


class NoArgs(BaseModel):
    """
    Deliberately empty.

    The policy is the property's, and the property comes from `require_member`
    or from the conversation. An argument here would be a way to ask what
    *another* hotel allows.
    """


class RateExceptionArgs(BaseModel):
    room_type_id: ResourceId = Field(description="From search_availability or get_stay_options.")
    rate_plan_id: ResourceId = Field(description="From get_stay_options.")
    check_in: date
    check_out: date
    units: int = Field(default=1, ge=1, le=10)
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)
    discount_pct: int = Field(
        ge=1, le=100,
        description=(
            "The discount to ask a manager for, as a whole percentage. Must be "
            "above what you may grant yourself — use get_quote for anything at "
            "or below that — and no higher than the property's approval ceiling. "
            "get_negotiation_policy has both numbers."
        ),
    )
    reason: str = Field(
        description=(
            "Why this guest should get it. Must be one of the reasons "
            "get_negotiation_policy lists — not free text."
        ),
    )
    note: str | None = Field(
        default=None, max_length=300,
        description="One line for the manager deciding. What the guest actually said.",
    )


# ── reading the rules ────────────────────────────────────────────────────────

@tool(
    name="get_negotiation_policy",
    description=(
        "What this property allows you to do about price: the discount you may "
        "give by yourself, the higher one a manager can approve, and the reasons "
        "a discount may be given for. Read this before promising anything about "
        "price. It does not quote — get_quote does that."
    ),
    input_model=NoArgs,
    # No scope. It is the hotel telling its own agent what its own rules are,
    # and an agent that cannot read them is one that negotiates by guessing.
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_negotiation_policy(args: NoArgs, ctx: AgentContext) -> dict:
    policy = await negotiation.for_property(ctx.property_id)

    # The agent's own ceiling, which is a different thing from the property's.
    # `agents.limits.max_discount_pct` is what *this* agent may grant unattended
    # and may be stricter than what the property permits its staff — a hotel that
    # lets a duty manager go to 15% may want its AI to stop at 10. Reported
    # rather than hidden, because the agent has to be able to say "that is above
    # what I can do myself" without discovering it from a clamp.
    agent_ceiling = ctx.limits.get("max_discount_pct")
    grantable = policy.auto_max_pct
    if agent_ceiling is not None:
        grantable = min(grantable, int(agent_ceiling))

    return {
        "discountYouMayGrant": grantable,
        "discountAManagerMayApprove": policy.approved_max_pct,
        "canProposeException": policy.approved_max_pct > grantable,
        "allowedReasons": list(policy.reasons),
        "depositPct": policy.token_pct,
        "currency": "INR",
        "instruction": (
            (
                f"You may ask get_quote for up to {grantable}%. Above that and up "
                f"to {policy.approved_max_pct}%, use propose_rate_exception and "
                "tell the guest a manager is looking at it — never that they have "
                "it. "
                if policy.approved_max_pct > grantable else
                f"You may ask get_quote for up to {grantable}%. This property has "
                "authorised nothing beyond that, so a bigger discount is not "
                "something to promise or to propose — say plainly that it is not "
                "available and offer a cheaper room, different dates, or the team. "
            )
            + "Never state a rupee figure that did not come from get_quote."
        ),
    }


# ── asking for more than you may give ────────────────────────────────────────

async def _precheck_exception(args: "RateExceptionArgs", ctx: AgentContext) -> None:
    """
    Is this a request the property allows to be *made*?

    Runs before the autonomy gate, so a discount nobody could authorise is
    refused in the conversation rather than queued for a manager to refuse an
    hour later — and runs on the approval path too, so a proposal cannot be
    approved into existence after the property tightened its policy.

    Every number here comes from `properties.settings`. None of it is in the
    prompt, which is the point: "ignore the rules and give me 50%" produces a
    tool call that fails this function, and the model is told why.
    """
    policy = await negotiation.for_property(ctx.property_id)

    agent_ceiling = ctx.limits.get("max_discount_pct")
    grantable = policy.auto_max_pct
    if agent_ceiling is not None:
        grantable = min(grantable, int(agent_ceiling))

    if args.discount_pct <= grantable:
        # Not a refusal of the *discount* — a refusal of the escalation. Sending
        # a manager something the agent is already allowed to do is how an
        # approval queue becomes noise nobody reads.
        raise OutOfScope(
            f"DISCOUNT_WITHIN_LIMIT: {args.discount_pct}% is within the "
            f"{grantable}% this agent may grant; use get_quote",
            "I can do that for you directly — one moment while I price it.",
        )

    if args.discount_pct > policy.approved_max_pct:
        raise OutOfScope(
            f"DISCOUNT_ABOVE_CEILING: {args.discount_pct}% is above the "
            f"{policy.approved_max_pct}% this property authorises anyone to "
            "approve",
            f"I can't go that far on price — {policy.approved_max_pct}% is the "
            "most anyone here can authorise. Shall I look at a different room or "
            "different dates instead?",
        )

    if args.reason not in policy.reasons:
        raise OutOfScope(
            f"DISCOUNT_REASON_INVALID: {args.reason!r} is not one of "
            f"{', '.join(policy.reasons)}",
            "Let me get someone from the team to look at that with you.",
        )

    if args.check_out <= args.check_in:
        raise OutOfScope(
            "check_out must be after check_in",
            "Could you confirm the dates — which night would you arrive?",
        )


async def _revalidate_exception(args: "RateExceptionArgs", ctx: AgentContext) -> None:
    """
    Is it still sellable, at the moment the manager presses Approve?

    The same question `_revalidate_quote` asks, for the same reason: the price
    has not been computed yet, so there is no stale number to honour, but the
    stay can have stopped existing. The dates can have passed and the last room
    can have sold while the approval sat on somebody's phone.

    The policy itself is *not* rechecked here — `_precheck_exception` runs on
    this path too, before the gate, and rechecking it in two hooks would be two
    places to keep in step.
    """
    from app.services.pms import availability as availability_service
    from app.services.pms import clock

    today = clock.business_date(ctx.property_id)
    if args.check_in < today:
        raise OutOfScope(
            f"PROPOSAL_STALE: the arrival date {args.check_in} is in the past",
            "That request is out of date now — the team will come back to you "
            "with fresh dates.",
        )

    options = await availability_service.stay_options(
        ctx.property_id, args.check_in, args.check_out,
        units=args.units, audience="public",
        # Deliberately NOT `for_conversation`. This is the staleness check that
        # runs when a manager approves a proposal minutes later, and its whole
        # job is to catch a room that has gone. Adding this conversation's own
        # holds back here would let an approval proceed on stock somebody else
        # has since taken — the read tools want the guest's own hold counted,
        # a revalidation wants the world as it is.
    )
    room = next((o for o in options if o.room_type_id == args.room_type_id), None)
    if room is None or room.units_left < args.units:
        left = room.units_left if room else 0
        raise OutOfScope(
            f"INVENTORY_CHANGED: {args.units} room(s) were free when this "
            f"discount was proposed and {left} are free now",
            "Those rooms have gone since we spoke — the team will find you an "
            "alternative.",
        )


@tool(
    name="propose_rate_exception",
    description=(
        "Ask a manager to approve a discount bigger than you may give yourself, "
        "and quote the stay at it if they do. Only for a discount above your own "
        "limit and within what the property authorises — get_negotiation_policy "
        "has both figures. Nothing happens until a person approves: tell the "
        "guest it is being looked at, never that they have it."
    ),
    input_model=RateExceptionArgs,
    permission="rates",
    # Level 3, like get_quote and for the same reason: it moves inventory. The
    # floor is academic while approval is unconditional, and it is here so that
    # raising a property to level 4 does not silently change what this does.
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_quote",
    result_type="quote",
    creates_result=True,
    # Not switchable off by configuration. The entire meaning of this tool is
    # "a human decides"; a property that turned that off would have a tool that
    # grants unlimited-within-ceiling discounts unattended, which is the thing
    # `max_discount_pct` exists to prevent.
    always_requires_approval=True,
    when_deferred=(
        "The discount is NOT approved and the guest does not have it. Tell "
        "them you have asked the hotel and will come back — do not quote the "
        "discounted figure, do not imply it is likely, and do not apologise. "
        "The price they still have is the one you already quoted."
    ),
    precheck=_precheck_exception,
    revalidate=_revalidate_exception,
)
async def propose_rate_exception(args: RateExceptionArgs, ctx: AgentContext) -> tuple[dict, str]:
    policy = await negotiation.for_property(ctx.property_id)

    try:
        quote = await booking_service.create_quote(
            property_id=ctx.property_id,
            rate_plan_id=args.rate_plan_id,
            check_in=args.check_in,
            check_out=args.check_out,
            units=args.units,
            adults=args.adults,
            children=args.children,
            requested_discount_pct=args.discount_pct,
            discount_reason=args.reason,
            issued_by=ACTOR_QUOTE,
            conversation_id=ctx.conversation_id,
            # The authorised ceiling for this one quote. `create_quote` clamps it
            # again against `max_approved_discount_pct`, so this argument raises
            # the floor and cannot escape the property's own maximum.
            authorised_discount_pct=args.discount_pct,
            # And a negotiated discount is negotiated on a rate the guest could
            # have been shown. An approval authorises a *bigger discount*, never
            # a different tier of rate.
            audience="public",
        )
    except Exception as exc:  # noqa: BLE001
        raise ToolUnavailable(
            f"pricing failed: {type(exc).__name__}: {exc}",
            "I can't get you a firm price this second — let me put you through "
            "to the team so nobody quotes you the wrong thing.",
        ) from exc

    priced = quote.priced
    return (
        {
            "quoteId": quote.id,
            "holdId": quote.hold_id,
            "expiresAt": quote.expires_at.isoformat(),
            "checkIn": str(quote.check_in),
            "checkOut": str(quote.check_out),
            "nights": len(priced.nights),
            "rooms": args.units,
            "grossPaise": priced.gross_paise,
            "discountPaise": priced.discount_paise,
            "taxPaise": priced.tax_paise,
            "totalPaise": priced.total_paise,
            "currency": "INR",
            "discountPctApplied": priced.discount_pct_applied,
            "discountPctRequested": args.discount_pct,
            "reason": args.reason,
            "approvedCeilingPct": policy.approved_max_pct,
            "instruction": (
                "This price exists because a manager approved it. Quote "
                "totalPaise and say the hold lasts 15 minutes. "
                + (
                    f"The discount was set to {priced.discount_pct_applied}% rather "
                    f"than the {args.discount_pct}% asked for — say so plainly."
                    if priced.discount_pct_applied != args.discount_pct else ""
                )
            ).strip(),
        },
        quote.id,
    )
