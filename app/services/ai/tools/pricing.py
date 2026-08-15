"""
Pricing.

One tool, and one rule: **the model may never type a price.**

Every number a guest is shown comes out of a `quotes` row, produced by
`services/pms/booking.create_quote()`, which is the same call the front desk
makes. The GST slab, the per-night allocation and the negotiation floor all live
there. This module supplies dates and a room type and reports what came back.

The discount is worth stating separately, because it is where an AI is most
obviously dangerous. `create_quote` takes the discount as a *request* and clamps
it against the property's floor server-side. So an agent that is talked into
"give me 90% off" produces a quote at the property's maximum, not at 90% — the
clamp is in Python and Postgres, not in the prompt. The agent is told what it
was clamped to, so it can be honest about it.
"""
from datetime import date

from pydantic import BaseModel, Field

from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_QUOTE
from app.services.pms import booking as booking_service


class QuoteArgs(BaseModel):
    room_type_id: str = Field(description="From search_availability or get_stay_options.")
    rate_plan_id: str = Field(description="From get_stay_options.")
    check_in: date
    check_out: date
    units: int = Field(default=1, ge=1, le=10)
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)
    discount_pct: int = Field(
        default=0, ge=0, le=100,
        description=(
            "A discount to request, not to grant. The server clamps it to what "
            "the property allows; ask for what the guest asked for."
        ),
    )


async def _revalidate_quote(args: "QuoteArgs", ctx: AgentContext) -> None:
    """
    Is this still a stay we can sell, at the moment of approval?

    A manager approving a proposal five minutes later is approving the *idea* of
    quoting — the price itself has not been computed yet, so there is no stale
    number to honour. What can have gone stale is whether the stay is sellable
    at all: the dates can have passed and the last room can have sold while the
    approval sat on somebody's phone.

    Executing anyway would produce a quote for a room that does not exist and
    hold inventory the hotel has already sold.
    """
    from app.services.pms import availability as availability_service
    from app.services.pms import clock

    today = clock.business_date(ctx.property_id)
    if args.check_in < today:
        raise OutOfScope(
            f"PROPOSAL_STALE: the arrival date {args.check_in} is in the past",
            "That request is out of date now — the team will get back to you with fresh dates.",
        )

    options = await availability_service.stay_options(
        ctx.property_id, args.check_in, args.check_out,
        units=args.units, audience="public",
    )
    room = next((o for o in options if o.room_type_id == args.room_type_id), None)
    if room is None or room.units_left < args.units:
        left = room.units_left if room else 0
        raise OutOfScope(
            f"INVENTORY_CHANGED: {args.units} room(s) were free when this was "
            f"proposed and {left} are free now",
            "Those rooms have gone since we spoke — the team will find you an alternative.",
        )


@tool(
    name="get_quote",
    description=(
        "The firm price for a stay, and a 15-minute hold on the rooms. This is "
        "the only source of a price — never state a number that did not come "
        "from here. Returns the total, the tax, and what the discount was "
        "actually set to."
    ),
    input_model=QuoteArgs,
    permission="rates",
    # Level 3: it moves inventory. A quote takes a hold, and a hold is a room
    # somebody else cannot book for the next fifteen minutes.
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_quote",
    result_type="quote",
    creates_result=True,
    revalidate=_revalidate_quote,
)
async def get_quote(args: QuoteArgs, ctx: AgentContext) -> tuple[dict, str]:
    if args.check_out <= args.check_in:
        raise OutOfScope(
            "check_out must be after check_in",
            "Could you confirm the dates — which night would you arrive?",
        )

    # The agent's own ceiling, on top of the property's. A property that lets
    # its staff go to 15% may want its AI to stop at 10, and that is a number on
    # the agent row rather than a sentence in its prompt.
    requested = args.discount_pct
    agent_ceiling = ctx.limits.get("max_discount_pct")
    if agent_ceiling is not None:
        requested = min(requested, int(agent_ceiling))

    try:
        quote = await booking_service.create_quote(
            property_id=ctx.property_id,
            rate_plan_id=args.rate_plan_id,
            check_in=args.check_in,
            check_out=args.check_out,
            units=args.units,
            adults=args.adults,
            children=args.children,
            requested_discount_pct=requested,
            discount_reason="negotiation" if requested else None,
            issued_by=ACTOR_QUOTE,
            conversation_id=ctx.conversation_id,
            # The same 'public' that `search_availability` and
            # `get_stay_options` pass. Those filter what the guest is *shown*;
            # this decides what may be *priced*, and the two have to agree or
            # the filter is decoration — a rate plan id is a uuid, and a uuid
            # from the b2b tier would otherwise quote a guest the agent rate.
            audience="public",
        )
    except Exception as exc:  # noqa: BLE001
        # Never fall back to an estimate. A pricing service that is down means
        # the agent does not know the price, and saying so is the only safe
        # answer available.
        raise ToolUnavailable(
            f"pricing failed: {type(exc).__name__}: {exc}",
            "I can't get you a firm price this second — let me put you through "
            "to the team so nobody quotes you the wrong thing.",
        ) from exc

    priced = quote.priced
    payload = {
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
        "instruction": (
            "Quote totalPaise and nothing else. Say the hold lasts 15 minutes. "
            + (
                f"The discount was set to {priced.discount_pct_applied}% rather than the "
                f"{args.discount_pct}% requested — be straightforward about that "
                "if the guest asked for a specific figure."
                if priced.discount_pct_applied != args.discount_pct else ""
            )
        ).strip(),
    }
    return payload, quote.id
