"""
Booking.

The agent cannot conjure a reservation. `create_booking` takes a `quote_id` and
nothing else that matters — no price, no dates, no room type — because
`services/pms/booking.create_reservation()` reads all of that from the quote it
already priced and held. There is no argument for the model to get wrong and no
argument for a guest to talk it into.

That is also what makes the flow safe under retry: a quote can be converted
once. A second call finds it consumed and is refused, so a model that repeats
itself produces one booking, not two.

`release_hold` exists because a guest who says "actually, no" should not leave a
room unsellable for fifteen minutes. It is level 2 rather than 3: giving stock
back is the safe direction.
"""
from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_RESERVATION
from app.services.pms import booking as booking_service
from app.services.pms import creators as creators_service


class BookingArgs(BaseModel):
    quote_id: ResourceId = Field(description="From get_quote. The price and dates come from it.")
    guest_name: str = Field(min_length=2, max_length=120)
    guest_phone: str | None = Field(default=None, max_length=20)
    guest_email: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=500)


class HoldArgs(BaseModel):
    hold_id: ResourceId = Field(description="From get_quote.")


async def _revalidate_booking(args: "BookingArgs", ctx: AgentContext) -> None:
    """
    Is the quote a manager approved still the quote we would honour?

    This is the case the whole revalidation path exists for. A quote holds
    inventory for fifteen minutes and carries a price. Approve it twenty minutes
    later and the hold has expired, the room may have sold, and the price on the
    row is a promise nobody is standing behind any more.

    Consuming it anyway would sell a room the hotel no longer has at a price it
    no longer offers, on the strength of an approval that was true when it was
    given.
    """
    from datetime import datetime, timezone

    def _read():
        rows = (
            get_db().table("quotes")
            .select("id,property_id,status,expires_at,total_paise,hold_id")
            .eq("id", args.quote_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    quote = await run_db(_read)
    if not quote or quote["property_id"] != ctx.property_id:
        raise OutOfScope(
            f"QUOTE_NOT_FOUND: quote {args.quote_id} is not this property's",
            "Something is out of step with that price — the team will follow up.",
        )

    if quote["status"] != "active":
        raise OutOfScope(
            f"QUOTE_CONSUMED: the quote is {quote['status']}, not active",
            "That price has already been used — the team will follow up.",
        )

    expires = quote.get("expires_at")
    if expires:
        when = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        if when < datetime.now(timezone.utc):
            raise OutOfScope(
                f"QUOTE_EXPIRED: the quote expired at {when.isoformat()} and "
                "its hold has been released",
                "That price has expired — the team will send you a fresh one.",
            )


@tool(
    name="create_booking",
    description=(
        "Turn a quote into a confirmed reservation. The price, dates and rooms "
        "all come from the quote — you cannot set them here. Only call this "
        "when the guest has clearly agreed to the quoted total."
    ),
    input_model=BookingArgs,
    permission="frontdesk",
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_booking",
    result_type="reservation",
    creates_result=True,
    revalidate=_revalidate_booking,
    # Even at level 3. The first hotel to let an AI take bookings unattended
    # will not be the first hotel to try it, and the queue costs a manager ten
    # seconds against a booking they cannot un-take.
    always_requires_approval=True,
    # And this is the sentence that keeps the gate from reading as a fault.
    #
    # Because the flag above is unconditional, *every* guest who says "book it"
    # lands here. Told only that approval was required, the model apologised —
    # "I'm sorry, I wasn't able to book the room directly" — and a guest who
    # had just been quoted a real price heard the hotel's own safety rule as
    # the hotel being unable to sell them a room.
    #
    # There is nothing to be sorry about: the guest can pay right now, and
    # paying is what confirms a booking anyway. So the deferral points at the
    # road that works instead of describing the one that is closed.
    when_deferred=(
        "Nothing is booked and no room is held. Do NOT apologise and do NOT "
        "say the booking failed — a hotel confirms on payment. Call "
        "create_booking_payment_link for this same quote and give the guest "
        "the link. Tell them the room is confirmed once the payment goes "
        "through; do NOT tell them it is reserved, held or booked before "
        "then, because it is not and somebody else may take it. Only if that "
        "also refuses should you hand over to the team."
    ),
)
async def create_booking(args: BookingArgs, ctx: AgentContext) -> tuple[dict, str]:
    ceiling = ctx.limits.get("max_booking_value_paise")
    if ceiling is not None:
        def _read():
            rows = (
                get_db()
                .table("quotes")
                .select("total_paise,status,property_id")
                .eq("id", args.quote_id)
                .limit(1)
                .execute()
                .data
                or []
            )
            return rows[0] if rows else None

        quote = await run_db(_read)
        if not quote or quote["property_id"] != ctx.property_id:
            raise OutOfScope(
                f"quote {args.quote_id} does not belong to this property",
                "Something's out of step with that price — let me get the team.",
            )
        if quote["total_paise"] > int(ceiling):
            raise OutOfScope(
                f"quote total {quote['total_paise']} exceeds the agent's "
                f"max_booking_value_paise of {ceiling}",
                "That's a larger booking than I can complete myself — "
                "I'll bring in the hotel team to finish it with you.",
            )

    try:
        reservation = await booking_service.create_reservation(
            property_id=ctx.property_id,
            quote_id=args.quote_id,
            guest_name=args.guest_name,
            guest_phone=args.guest_phone,
            guest_email=args.guest_email,
            source="whatsapp" if ctx.channel == "whatsapp" else ctx.channel,
            token_paise=0,
            payment_method=None,
            notes=args.notes,
            created_by=ACTOR_RESERVATION,
            member_id=None,
            member_name=ctx.principal.display_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise ToolUnavailable(
            f"booking failed: {type(exc).__name__}: {exc}",
            "I couldn't complete that booking — putting you through to the team now.",
        ) from exc

    # Who sent this guest, decided by the server from the touch chain. The model
    # is not consulted and could not be: it has no way to name a creator, and
    # `resolve` reads the conversation id off the run's own context rather than
    # off anything the model produced. An AI that could assign attribution would
    # be an AI that could assign money.
    await creators_service.resolve(
        reservation["id"], conversation_id=ctx.conversation_id
    )

    return (
        {
            "reservationId": reservation["id"],
            "ref": reservation["ref"],
            "status": reservation["status"],
            "checkIn": reservation["check_in"],
            "checkOut": reservation["check_out"],
            "totalPaise": reservation["total_paise"],
            "instruction": (
                "Confirm using the reference. Do not say the booking is paid — "
                "paymentStatus says what has actually been collected."
            ),
        },
        reservation["id"],
    )


@tool(
    name="release_hold",
    description=(
        "Give held rooms back when a guest decides against a quote. Safe to "
        "call — it only ever returns stock."
    ),
    input_model=HoldArgs,
    permission="rates",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="hold",
)
async def release_hold(args: HoldArgs, ctx: AgentContext) -> tuple[dict, str]:
    def _write():
        db = get_db()
        # The property check is here rather than trusted from the argument: a
        # hold id is a uuid, and a uuid from another hotel would otherwise
        # release their stock.
        rows = (
            db.table("inventory_holds")
            .select("id,property_id,status")
            .eq("id", args.hold_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not rows or rows[0]["property_id"] != ctx.property_id:
            return None
        db.rpc("release_hold", {"p_hold": args.hold_id}).execute()
        return rows[0]

    row = await run_db(_write)
    if row is None:
        raise OutOfScope(
            f"hold {args.hold_id} does not belong to this property",
            "I couldn't find that reservation hold.",
        )
    return {"released": True, "holdId": args.hold_id}, args.hold_id


# ── closing the sale ─────────────────────────────────────────────────────────


class BookingPaymentLinkArgs(BaseModel):
    quote_id: ResourceId = Field(description="The quote the guest agreed to.")
    guest_name: str = Field(
        min_length=1, max_length=120,
        description="The guest's full name, as they gave it.",
    )
    guest_phone: str | None = Field(
        default=None, max_length=32,
        description="Their phone number, if they have given one.",
    )
    guest_email: str | None = Field(
        default=None, max_length=200,
        description="Their email, if they have given one.",
    )


@tool(
    name="create_booking_payment_link",
    description=(
        "Take payment for a quote the guest has agreed to, when they do NOT "
        "already have a booking. Returns a link: the guest taps it, pays by "
        "UPI, card or netbanking, and the booking is confirmed automatically "
        "the moment the payment goes through. Use this for 'book it', 'yes "
        "please', 'how do I pay' on a new stay. Get a quote first. Does NOT "
        "confirm anything by itself — tell the guest their booking is confirmed "
        "once they have paid, never because they say they have."
    ),
    input_model=BookingPaymentLinkArgs,
    # `rates`, matching `create_payment_link`. Asking for money against a price
    # this agent was allowed to quote is part of selling a stay.
    permission="rates",
    # Selling, so the same floor as `get_quote` and `create_booking`. A hotel at
    # level 2 gets an approval request, which is the same answer it gets today.
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="payment_link",
    # Deliberately NOT `always_requires_approval`, and this is the one place in
    # the system where that asymmetry is load-bearing, so it is worth stating.
    #
    # `create_booking` carries it because it takes a room off sale on a model's
    # say-so, before any money has moved, and nobody can un-take a night. This
    # tool takes no inventory at all: it raises a Razorpay order, and the
    # reservation is created by `_fulfil_booking` only after a signed webhook
    # says the guest has paid — the identical unattended path the hotel's own
    # booking page has always used. A guest who never pays costs a held quote
    # that expires by itself.
    #
    # Requiring approval here would mean a manager has to press a button before
    # a guest is allowed to *give the hotel money*, which is not a safety
    # property. It is the thing that stopped the AI employee closing a sale.
    always_requires_approval=False,
)
async def create_booking_payment_link(
    args: BookingPaymentLinkArgs, ctx: AgentContext
) -> tuple[dict, str]:
    from app.services.pms import quote_payment

    try:
        link = await quote_payment.create_link(
            property_id=ctx.property_id,
            partner_id=ctx.partner_id,
            quote_id=args.quote_id,
            guest_name=args.guest_name,
            guest_phone=args.guest_phone or ctx.guest_phone,
            guest_email=args.guest_email,
            source=ctx.channel,
        )
    except quote_payment.QuotePaymentError as exc:
        # Already written for a guest to read — see the module. Raised as
        # OutOfScope so the runtime hands the sentence to the model rather than
        # turning it into a failed run the guest sees nothing from.
        raise OutOfScope(f"quote {args.quote_id}: {exc}", str(exc)) from exc

    # Three amounts, and only one of them is what the link charges.
    #
    # The first version returned `amountText`, `totalText` and `balanceText` as
    # a flat set with an instruction to "give the guest the link and the
    # amount". The model picked the total. A guest was told
    #
    #     Please use this link to complete your booking for ₹7,168
    #
    # and the page it opened asked for ₹1,434, because this property takes a
    # deposit and the rest is paid at the hotel. Both numbers were correct and
    # the sentence was wrong, which is the worst of the three possibilities —
    # a guest who spots it thinks they are being overcharged, and a guest who
    # does not thinks their stay is paid for.
    #
    # So the ready sentence is built here, where the numbers are, and the
    # instruction is to use it rather than to compose one. The fields stay for
    # a question like "what's the total?", which the model should still be able
    # to answer without another tool call.
    amount_text = f"₹{link.amount_paise / 100:,.0f}"
    total_text = f"₹{link.total_paise / 100:,.0f}"
    due_at_hotel = link.total_paise - link.amount_paise

    if due_at_hotel > 0:
        say = (
            f"{amount_text} to confirm now, and the balance of "
            f"₹{due_at_hotel / 100:,.0f} at the hotel — total {total_text}."
        )
    else:
        say = f"{amount_text}, which is the full amount."

    return (
        {
            "payUrl": link.pay_url,
            "sayThis": say,
            "payNowText": amount_text,
            "payNowPaise": link.amount_paise,
            "totalText": total_text,
            "dueAtHotelText": f"₹{due_at_hotel / 100:,.0f}",
            "instruction": (
                "Send the guest the payUrl and say sayThis, close to word for "
                "word. The link charges payNowText and nothing else — never "
                "tell them to pay totalText through it. Say the booking is "
                "confirmed as soon as the payment goes through."
            ),
        },
        link.order_id,
    )
