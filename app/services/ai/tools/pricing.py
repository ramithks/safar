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
