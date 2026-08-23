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
from app.services.pms import money


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
    guest_name: str | None = Field(
        default=None, max_length=120,
        description=(
            "The guest's name, if they have given it in this conversation. "
            "Leave it out otherwise — the name on the thread is used. Never "
            "ask for a name you have already been told."
        ),
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

    # The name on the thread, when the model was not given one.
    #
    # It was required, and the model — correctly — asked for it. On a channel
    # that already carries the guest's name that is a round trip for something
    # both sides know: the guest had said "Sahil" four messages earlier, and
    # was asked again at the moment they had agreed to pay. Every extra turn
    # between agreement and payment link is a sale that can go cold.
    name = (args.guest_name or ctx.guest_name or "").strip()
    if not name:
        raise OutOfScope(
            "no name for the booking, on the args or on the conversation",
            "Could you tell me the name the booking should be in?",
        )

    try:
        link = await quote_payment.create_link(
            property_id=ctx.property_id,
            partner_id=ctx.partner_id,
            quote_id=args.quote_id,
            guest_name=name,
            guest_phone=args.guest_phone or ctx.guest_phone,
            guest_email=args.guest_email,
            source=ctx.channel,
            # Not from the model. The thread is a fact about where this turn is
            # running, and it is what get_booking_status reads back.
            conversation_id=ctx.conversation_id,
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


# ── did my payment go through? ───────────────────────────────────────────────


class BookingStatusArgs(BaseModel):
    """
    No arguments, and that is the security property.

    The subject of this tool is the conversation, which the model cannot name,
    cannot guess and cannot be talked into changing. "Check booking TB-4K2M9X"
    from a stranger reaches the same code path as "payment hogyi?" from the
    person who made it, and both get this thread's booking or nothing.
    """


def read_booking_state(conversation_id: str, property_id: str) -> dict:
    """
    Compose the money state of one conversation out of the three tables that own
    parts of it.

    Public, and read by the inbox's session panel as well as by the tool below.
    That is deliberate: if staff and the AI composed this state separately they
    would eventually describe the same booking differently, and the guest would
    be holding both descriptions.

    The lifecycle in the brief — DRAFT, QUOTE_CREATED, HOLD_ACTIVE,
    PAYMENT_PENDING, PAYMENT_RECEIVED, CONFIRMED — is not a column anywhere,
    deliberately. `quotes.status`, `payment_orders.status` and
    `reservations.{status,payment_status}` each own a real transition with a
    real writer, and a fourth column restating them would be a fourth opinion
    that drifts. This function is the one place they are read together, and it
    reads them newest-fact-first: a reservation outranks an order, an order
    outranks a quote.
    """
    db = get_db()

    reservation = (
        db.table("reservations")
        .select("id,ref,public_token,status,payment_status,check_in,check_out,"
                "nights,units,adults,children,total_paise,paid_paise,guest_name,"
                "created_at")
        .eq("property_id", property_id)
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute().data or []
    )

    if reservation:
        stay = reservation[0]
        # The folio, not `total - paid`.
        #
        # This computed `max(0, total - paid)` of its own, which is right up
        # until a guest checks in: from then on the extras they order live on
        # the folio and the reservation total does not know about them. The
        # agent would tell an in-house guest they owed ₹4,800 while the desk's
        # screen said ₹6,000, and both would be reading the same booking.
        #
        # One read, one function, four surfaces. See `services/pms/money`.
        folio = (
            db.table("folio_entries").select("kind,amount_paise")
            .eq("reservation_id", stay["id"]).execute().data or []
        )
        purse = money.state(stay, folio)
        total = purse.total_paise
        paid = purse.paid_paise
        state = {
            "confirmed": "CONFIRMED",
            "checked_in": "CHECKED_IN",
            "checked_out": "CHECKED_OUT",
            "cancelled": "CANCELLED",
            "no_show": "CANCELLED",
        }.get(stay.get("status") or "", "CONFIRMED")
        return {
            "state": state,
            "ref": stay.get("ref"),
            "reservationId": stay["id"],
            "guestName": stay.get("guest_name"),
            "checkIn": stay.get("check_in"),
            "checkOut": stay.get("check_out"),
            "nights": stay.get("nights"),
            "rooms": stay.get("units"),
            "adults": stay.get("adults"),
            "children": stay.get("children"),
            "totalPaise": total,
            "paidPaise": paid,
            "refundedPaise": purse.refunded_paise,
            "balancePaise": purse.balance_due_paise,
            "paymentStatus": stay.get("payment_status"),
            # Consumed by `get_booking_status` to build the link and then
            # dropped. It is the guest's own token, but a field the model can
            # quote is a field the model will quote somewhere it does not
            # belong, and the URL already carries everything the guest needs.
            "_publicToken": stay.get("public_token"),
        }

    order = (
        db.table("payment_orders")
        .select("id,status,amount_paise,razorpay_order_id,quote_id,"
                "failure_reason,created_at,paid_at")
        .eq("property_id", property_id)
        .eq("conversation_id", conversation_id)
        .eq("purpose", "booking")
        .order("created_at", desc=True)
        .limit(1)
        .execute().data or []
    )

    if order:
        row = order[0]
        # 'paid' with no reservation on the conversation means fulfilment is
        # mid-flight or failed. Reported as received-not-yet-confirmed, which is
        # exactly what it is — and never as confirmed, because the room has not
        # been taken out of inventory yet.
        state = {
            "created": "PAYMENT_PENDING",
            "paid": "PAYMENT_RECEIVED",
            "failed": "PAYMENT_FAILED",
        }.get(row.get("status") or "", "PAYMENT_PENDING")
        return {
            "state": state,
            "orderId": row.get("razorpay_order_id"),
            "amountPaise": int(row.get("amount_paise") or 0),
            "failureReason": row.get("failure_reason"),
        }

    from datetime import datetime, timezone

    quote = (
        db.table("quotes")
        .select("id,status,check_in,check_out,units,adults,children,"
                "total_paise,expires_at")
        .eq("property_id", property_id)
        .eq("conversation_id", conversation_id)
        .eq("status", "active")
        .order("created_at", desc=True)
        .limit(1)
        .execute().data or []
    )

    if quote:
        row = quote[0]
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
            live = expires > datetime.now(timezone.utc)
        except (ValueError, TypeError, KeyError):
            live = False
        if live:
            return {
                "state": "QUOTE_ACTIVE",
                "quoteId": row["id"],
                "checkIn": row.get("check_in"),
                "checkOut": row.get("check_out"),
                "rooms": row.get("units"),
                "totalPaise": int(row.get("total_paise") or 0),
                "expiresAt": row.get("expires_at"),
            }

    return {"state": "NONE"}


# What the model is told to do about each state, in words it can put in front of
# a guest. Kept out of the system prompt on purpose: a rule about what to say
# when a payment is pending belongs with the tool that discovers it is pending,
# not in a paragraph every turn pays for whether or not money is involved.
_GUIDANCE = {
    "CONFIRMED": (
        "The payment has landed and this booking is real. Tell the guest it is "
        "confirmed, give them the reference, and give them bookingUrl. If "
        "balancePaise is above zero, say that amount is due at the hotel."
    ),
    "CHECKED_IN": "The guest is currently staying. Answer about their stay, not about booking it.",
    "CHECKED_OUT": "This stay has ended.",
    "CANCELLED": (
        "This booking was cancelled. Do not describe it as active. Anything "
        "about a refund goes to the team — call request_human_handoff."
    ),
    "PAYMENT_RECEIVED": (
        "The money has arrived and the booking is still being written. Tell the "
        "guest the payment came through and their confirmation is moments away. "
        "Do NOT say the booking is confirmed yet, and do NOT ask them to pay again."
    ),
    "PAYMENT_PENDING": (
        "A payment link was sent and nothing has been received against it. Say "
        "the payment has not shown up yet and that you will confirm the moment "
        "it does. Never say it is confirmed because the guest says they paid — "
        "a payment can take a minute, and this tool is what will know."
    ),
    "PAYMENT_FAILED": (
        "The payment did not go through. Say so plainly, without blaming them, "
        "and offer a fresh link."
    ),
    "QUOTE_ACTIVE": (
        "There is a price on the table and no payment. Nothing is booked and no "
        "room is held for them. If they want to go ahead, call "
        "create_booking_payment_link with quoteId."
    ),
    "NONE": (
        "This conversation has no quote, no payment and no booking. If they "
        "believe they have one made elsewhere, ask for the booking reference "
        "and hand over to the team."
    ),
}


@tool(
    name="get_booking_status",
    description=(
        "Where this conversation stands on money: quote, payment and booking. "
        "Call this for ANY question about whether a payment went through, "
        "whether a booking is confirmed, what has been paid, or what is still "
        "owed — including 'payment hogyi', 'have you received it', 'is it "
        "booked'. Never answer those from what the guest tells you; a guest "
        "saying they paid is not the same as the money arriving. Takes no "
        "arguments: it reads this conversation's own booking and nobody else's."
    ),
    input_model=BookingStatusArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="none",
    scoped_to_conversation=True,
)
async def get_booking_status(args: BookingStatusArgs, ctx: AgentContext) -> dict:
    if not ctx.conversation_id:
        # A console turn with no thread. There is no "this conversation's
        # booking" to report, and inventing one from the property would be the
        # cross-guest leak this tool's argument list exists to prevent.
        raise ToolUnavailable(
            "get_booking_status needs a conversation and this run has none",
        )

    state = await run_db(read_booking_state, ctx.conversation_id, ctx.property_id)

    token = state.pop("_publicToken", None)
    if state.get("ref"):
        from app.services.messaging import service as messaging

        state["bookingUrl"] = messaging.booking_url(
            {"ref": state["ref"], "public_token": token},
        )

    state["instruction"] = _GUIDANCE.get(state["state"], _GUIDANCE["NONE"])
    return state
