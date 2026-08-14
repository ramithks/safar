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
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_RESERVATION
from app.services.pms import booking as booking_service


class BookingArgs(BaseModel):
    quote_id: str = Field(description="From get_quote. The price and dates come from it.")
    guest_name: str = Field(min_length=2, max_length=120)
    guest_phone: str | None = Field(default=None, max_length=20)
    guest_email: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=500)


class HoldArgs(BaseModel):
    hold_id: str = Field(description="From get_quote.")


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
