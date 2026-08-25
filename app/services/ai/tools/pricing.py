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
import logging
from datetime import date

from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_QUOTE
from app.services.pms import booking as booking_service
from app.services.pms import money
from app.services.pms import rates

logger = logging.getLogger(__name__)


class QuoteArgs(BaseModel):
    room_type_id: ResourceId = Field(description="From search_availability or get_stay_options.")
    rate_plan_id: ResourceId = Field(description="From get_stay_options.")
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
            f"INVENTORY_CHANGED: {args.units} room(s) were free when this was "
            f"proposed and {left} are free now",
            "Those rooms have gone since we spoke — the team will find you an alternative.",
        )


async def _room_name(property_id: str, room_type_id: str) -> str | None:
    """
    What this room is called. One indexed read on a tool that already writes a
    quote and takes a hold, and the difference between a reply that says
    "Deluxe Balcony" and one that says "the room you chose".
    """
    def _read():
        rows = (
            get_db().table("room_types").select("name")
            .eq("id", room_type_id).eq("property_id", property_id)
            .limit(1).execute().data or []
        )
        return rows[0]["name"] if rows else None

    try:
        return await run_db(_read)
    except Exception:  # noqa: BLE001 — a name is never worth failing a quote
        logger.warning("could not read the name of room type %s", room_type_id)
        return None


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
        # The room, by the name the guest used. Carried so the reply can say
        # "Deluxe Balcony" and so the *session* can — the choice was on the
        # conversation as a uuid and invisible to the next turn's prompt, which
        # is how a guest who picked a room and then changed the number of rooms
        # got asked which room they wanted.
        "roomTypeId": str(args.room_type_id),
        "roomName": await _room_name(ctx.property_id, str(args.room_type_id)),
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
        # The same figure already written the way a guest reads it, so the
        # model quotes rather than calculates. See `money.display`.
        "total": money.display(priced.total_paise),
        "currency": "INR",
        "discountPctApplied": priced.discount_pct_applied,
        "discountPctRequested": args.discount_pct,
        "instruction": (
            "Quote `total` exactly as written and nothing else — never convert "
            "a paise figure yourself. Say the hold lasts 15 minutes. "
            + (
                f"The discount was set to {priced.discount_pct_applied}% rather than the "
                f"{args.discount_pct}% requested — be straightforward about that "
                "if the guest asked for a specific figure."
                if priced.discount_pct_applied != args.discount_pct else ""
            )
        ).strip(),
    }

    # **The deposit, worked out here rather than by the model.**
    #
    # A real group turn on 2026-08-24 ended with the reply guardrail refusing
    # ₹19,784 — thirty per cent of a ₹65,946 total, which the model had
    # multiplied out itself because nothing had given it the figure.
    # `get_group_rates` returns `depositPct: 30`, this returns the total, and
    # between them they invited exactly the arithmetic §8 forbids.
    #
    # A percentage and a total together in a model's context is a
    # multiplication waiting to happen, and the answer is not a firmer
    # instruction — it is the number, in the shape the model is allowed to
    # quote, so there is nothing left to work out. Same reasoning as `total`
    # above; see `money.display`.
    #
    # **The first version of this only did it for groups, and that was the bug
    # rather than the fix.** It read: *"a single room carries no deposit, and a
    # `deposit: ₹0` on every quote is a field the model would eventually mention
    # to somebody."* The first half is false wherever the property has set a
    # token percentage, which Casa Mandala has at 20. Caught on the live model by
    # `scripts/replay_e2e.py`, on a two-room quote against a group minimum of
    # three:
    #
    #     Pool Villa ke 2 rooms ka total price tax ke sath ₹84,016 hai. Isme se
    #     abhi aapko sirf 20% advance (₹16,803) dena hoga…
    #
    # ₹84,016 × 0.20 = ₹16,803.20, rounded, by the model. The outbound guardrail
    # refused it, so the guest was handed to a person at the moment they were
    # being told how to pay — the identical shape as the group failure, one
    # month-old fix away from being caught.
    #
    # Nothing had to be computed to close it: `price_stay` already returns
    # `token_paise` and `balance_paise` from the property's own `token_pct`, and
    # this payload simply never carried them. The group percentage still
    # overrides where the hotel set one, because a twenty-room block is the case
    # a hotel most wants a bigger commitment on.
    #
    # The second half of that comment is still right, and is why the fields are
    # conditional: a property that takes nothing up front gets no deposit field
    # rather than a `₹0` one for the model to volunteer.
    deposit_paise = priced.token_paise
    deposit_pct: int | None = None

    group = await rates.group_policy_for(ctx.property_id)
    is_group = bool(group and group.deposit_pct and args.units >= group.min_units)
    if is_group:
        deposit_pct = group.deposit_pct
        deposit_paise = round(priced.total_paise * group.deposit_pct / 100)

    deposit_paise = min(max(int(deposit_paise or 0), 0), priced.total_paise)
    if deposit_paise > 0:
        balance_paise = priced.total_paise - deposit_paise
        if deposit_pct is not None:
            payload["depositPct"] = deposit_pct
        payload["depositPaise"] = deposit_paise
        payload["deposit"] = money.display(deposit_paise)
        payload["balancePaise"] = balance_paise
        payload["balance"] = money.display(balance_paise)
        payload["instruction"] += (
            (" This is a group booking:" if is_group else " If the guest asks what to pay now:")
            + " the amount due now is `deposit` and the rest is `balance`. "
            "Quote both exactly as written — do not work out a percentage of "
            "anything."
        )

    return payload, quote.id
