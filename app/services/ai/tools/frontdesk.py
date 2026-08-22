"""
Front desk tools — the staff-facing half of the registry.

These are the tools a receptionist's assistant needs and a guest's assistant
must never have: the whole property's arrivals, any reservation, every room, the
folio. The difference between the two agents is not the prompt and not the
model. It is which rows exist in `agent_tools`, and the fact that every function
in this module refuses outright when the context belongs to a guest.

Three rules hold across the file.

**Property scope is the only scope, and it is not optional.** A guest-facing
tool takes its subject from the conversation (`get_guest` has no argument to
argue with). A staff-facing tool cannot work that way — "who is arriving today"
is a question about the whole hotel — so the boundary moves up: every query is
`.eq("property_id", ctx.property_id)`, and `ctx.property_id` came from an
authenticated membership, never from the model. There is no argument anywhere in
this module that can name a property, which is why "show me the other hotel's
arrivals" has no failure mode to exploit — it has nothing to fill in.

**`_require_staff` is belt and braces, and worth the braces.** The grant system
already stops a Guest agent calling `list_arrivals`, because `agent_tools` is a
closed list. But the cost of a misconfiguration here is a guest on WhatsApp
being read the arrivals board, so the tools check the actor as well. Two
independent mechanisms have to fail before that happens rather than one.

**A room number is resolved, never carried.** `find_room` returns a row or
nothing, and nothing is a refusal. The PMS's own `create_task` will happily
store a room number that matches no room — correct for a human typing "Lobby",
wrong for a model that heard "two-oh-nine" and wrote "290". So the resolution
happens here, on the agent path, where the risk actually is.

And the rule from `tools/__init__.py`, which this module obeys without
exception: **no business logic**. Check-in is `pms.frontdesk.check_in`. The
balance that refuses a check-out is computed by the same function the desk's own
button calls. If something in here looks like it is deciding, it is a bug.
"""
import hashlib
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import AgentError, OutOfScope, PermissionDenied, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_TASK
from app.services.pms import clock
from app.services.pms import frontdesk as frontdesk_service
from app.services.pms import money
from app.services.pms import workspace as workspace_service

HOUSEKEEPING_STATUSES = ("clean", "dirty", "inspected", "out_of_order")
TASK_STATUSES = ("open", "accepted", "in_progress", "resolved", "cancelled")

# What an agent may put on a folio, and it is one thing.
#
# `pms.frontdesk.post_folio_entry` accepts six kinds. Five of them move money in
# directions nobody should be able to talk a language model into: 'payment' says
# cash was collected, 'refund' says it was handed back, 'adjustment' writes off
# whatever you like, 'tax' and 'room' belong to the night audit. An agent posts
# the guest's laundry to their room and nothing else, and it is enforced by the
# tool taking no `kind` argument at all rather than by validating one.
AGENT_FOLIO_KIND = "addon"


# ── the two guards every tool in this file starts with ───────────────────────

def _require_staff(ctx: AgentContext) -> None:
    """
    Refuse a property-wide tool on a guest conversation.

    The second lock. `permissions.check()` has already refused any tool this
    agent has no grant for, so reaching here on a guest context means somebody
    granted a front desk tool to a guest-facing agent — a configuration mistake,
    not an attack, and one whose blast radius is another guest's data. Refused
    with a reason the audit trail can be searched for.
    """
    if ctx.guest_scoped:
        raise PermissionDenied(
            "front desk tools are staff-only; this run belongs to a guest conversation",
            "Let me get someone from the hotel team to help with that.",
        )


def _pms(exc: HTTPException, doing: str) -> AgentError:
    """
    Turn the PMS's refusal into the agent's refusal, keeping the sentence.

    This is the load-bearing translation in the whole file. `check_out` refuses
    with "₹3,264 is outstanding on the folio and must be settled first" — a
    complete, correct, staff-readable explanation that the PMS computed from the
    folio. Letting that fall through to `registry.execute`'s generic handler
    would turn it into "check_out failed: HTTPException", the agent would report
    a system error, and a receptionist would go looking for a bug instead of
    asking the guest for ₹3,264.

    4xx is a fact about the hotel and goes back as a refusal the model must
    relay. 5xx is a broken system and goes back as unavailable, which routes to
    a human — the two must not be collapsed, because "the guest owes money" and
    "the database is down" call for opposite behaviour.
    """
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    if 400 <= exc.status_code < 500:
        return OutOfScope(f"{doing} refused by the PMS: {detail}", detail)
    return ToolUnavailable(f"{doing} failed: {detail}")


async def _room_or_refuse(ctx: AgentContext, number: str) -> dict:
    room = await workspace_service.find_room(ctx.property_id, number)
    if not room:
        raise OutOfScope(
            f"no room numbered {number!r} at this property",
            f"There's no room {number} here — could you check the number?",
        )
    return room


async def _reservation_or_refuse(ctx: AgentContext, reservation_id: str) -> dict:
    found = await workspace_service.get_reservation(ctx.property_id, reservation_id)
    if not found:
        raise OutOfScope(
            f"reservation {reservation_id} not found at this property",
            "I can't find that booking here.",
        )
    return found[0]


def _stay(row: dict, folio: dict | None = None, room_number: str | None = None) -> dict:
    """
    One stay, as the front desk agent reads it.

    `folio` is a grouped `folio_states` row, not a bare integer. It used to be
    the folio's *sum*, which for a booking that has not arrived is negative —
    the deposit and nothing else — and the agent would read "balancePaise:
    -1075200" off an arrivals list and tell a receptionist the guest was owed
    money. The rule that turns a folio into a number a person can act on lives
    in `services/pms/money` and is the same one the guest's own receipt uses.
    """
    out = {
        "reservationId": row["id"],
        "ref": row["ref"],
        "status": row["status"],
        "guestName": row.get("guest_name"),
        "checkIn": row["check_in"],
        "checkOut": row["check_out"],
        "nights": row.get("nights"),
        "rooms": row.get("units"),
        "roomNumber": room_number,
        "totalPaise": row.get("total_paise"),
        "paidPaise": row.get("paid_paise"),
        "paymentStatus": row.get("payment_status"),
    }
    if folio is not None:
        out["balancePaise"] = money.due_from_state(row, folio)
    return out


# ── arguments ────────────────────────────────────────────────────────────────

class NoArgs(BaseModel):
    pass


class FindReservationArgs(BaseModel):
    query: str = Field(
        min_length=2, max_length=80,
        description=(
            "A booking reference like TB-4K2M9X, a guest's name, or a phone "
            "number. Whatever the person at the desk actually said."
        ),
    )


class ReservationArgs(BaseModel):
    reservation_id: ResourceId = Field(description="The reservation's id, from find_reservation.")


class RoomArgs(BaseModel):
    room_number: str = Field(max_length=12, description="The room number, as it is written on the door.")


class ListRoomsArgs(BaseModel):
    housekeeping_status: str | None = Field(
        default=None,
        description=f"Narrow to one status: {', '.join(HOUSEKEEPING_STATUSES)}.",
    )


class SetRoomStatusArgs(BaseModel):
    room_number: str = Field(max_length=12)
    status: str = Field(description=f"One of: {', '.join(HOUSEKEEPING_STATUSES)}.")


class CheckInArgs(BaseModel):
    reservation_id: ResourceId = Field(description="The reservation's id, from find_reservation.")
    room_number: str | None = Field(
        default=None, max_length=12,
        description=(
            "Which room to put them in. Leave empty to use the room already "
            "assigned to the booking."
        ),
    )


class ListTasksArgs(BaseModel):
    status: str | None = Field(
        default=None, description=f"Narrow to one status: {', '.join(TASK_STATUSES)}."
    )
    category: str | None = Field(default=None, description="Narrow to one category.")


class TaskStatusArgs(BaseModel):
    task_id: ResourceId
    status: str = Field(description=f"One of: {', '.join(TASK_STATUSES)}.")


class StaffTaskArgs(BaseModel):
    title: str = Field(
        min_length=4, max_length=120,
        description=(
            "What needs doing: 'air conditioning not cooling', 'two extra "
            "towels'. Do not put the room number in the title — it is its own "
            "field."
        ),
    )
    category: str = Field(
        description=(
            "housekeeping, maintenance, concierge, f_and_b, front_desk, "
            "accounts or manager."
        ),
    )
    priority: str = Field(default="normal", description="low, normal, high or urgent.")
    room_number: str | None = Field(
        default=None, max_length=12,
        description=(
            "The room this is about, if it is about a room. Leave empty for a "
            "public area. Must be a room that exists."
        ),
    )


class FolioChargeArgs(BaseModel):
    reservation_id: ResourceId = Field(description="Whose folio, from find_reservation.")
    description: str = Field(
        min_length=3, max_length=120,
        description="What the charge is for, as it should read on the guest's bill.",
    )
    amount_paise: int = Field(
        gt=0, le=50_000_000,
        description="The amount in paise. ₹1,200 is 120000. Always positive.",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Reads
# ═════════════════════════════════════════════════════════════════════════════

@tool(
    name="list_arrivals",
    description=(
        "Everyone due to arrive today, with their room and whether they have "
        "already checked in. The authoritative arrivals list — never answer "
        "'who is arriving' from anything else."
    ),
    input_model=NoArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_arrivals(_args: NoArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    rows, folios = await workspace_service.list_reservations(ctx.property_id, bucket="arrivals")
    numbers = await _room_numbers(ctx, rows)

    return {
        "businessDate": clock.business_date_iso(ctx.property_id),
        "count": len(rows),
        "arrivals": [
            _stay(r, folios.get(r["id"]) or {}, numbers.get(r.get("room_id"))) for r in rows
        ],
        "instruction": (
            "This is the complete list. If a guest is not on it they are not "
            "arriving today — say so rather than looking for a reason they might "
            "be. 'confirmed' means still expected; 'checked_in' means already here."
        ) if rows else (
            "Nobody is arriving today. Say exactly that."
        ),
    }


@tool(
    name="list_departures",
    description=(
        "Everyone due to check out today, with what is still owed on each folio. "
        "The authoritative departures list."
    ),
    input_model=NoArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_departures(_args: NoArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    rows, folios = await workspace_service.list_reservations(ctx.property_id, bucket="departures")
    numbers = await _room_numbers(ctx, rows)

    return {
        "businessDate": clock.business_date_iso(ctx.property_id),
        "count": len(rows),
        "departures": [
            _stay(r, folios.get(r["id"]) or {}, numbers.get(r.get("room_id"))) for r in rows
        ],
        "instruction": (
            "balancePaise above zero means the guest owes that much and cannot "
            "check out until it is settled. Report the figure; do not compute "
            "your own."
        ),
    }


@tool(
    name="list_in_house",
    description="Every guest currently resident, with their room. Use for 'who is staying tonight'.",
    input_model=NoArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_in_house(_args: NoArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    rows, folios = await workspace_service.list_reservations(ctx.property_id, bucket="inhouse")
    numbers = await _room_numbers(ctx, rows)
    return {
        "count": len(rows),
        "inHouse": [
            _stay(r, folios.get(r["id"]) or {}, numbers.get(r.get("room_id"))) for r in rows
        ],
    }


@tool(
    name="find_reservation",
    description=(
        "Look up a booking by its reference, the guest's name, or their phone "
        "number. Use this first whenever someone names a guest or a booking — "
        "every other tool wants the reservation id this returns."
    ),
    input_model=FindReservationArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def find_reservation(args: FindReservationArgs, ctx: AgentContext) -> dict:
    """
    Search, scoped to the property and nothing else.

    There is no `property_id` argument, so the only hotel this can search is the
    one the authenticated member opened. A reference from another property
    returns no matches — not because the reference was checked and rejected, but
    because the query never looked anywhere it could have been found.
    """
    _require_staff(ctx)
    rows, folios = await workspace_service.list_reservations(
        ctx.property_id, search=args.query, limit=10
    )
    numbers = await _room_numbers(ctx, rows)

    return {
        "query": args.query,
        "count": len(rows),
        "matches": [
            _stay(r, folios.get(r["id"]) or {}, numbers.get(r.get("room_id"))) for r in rows
        ],
        "instruction": (
            "No booking here matches that. Say so plainly — do not offer a "
            "similar name as though it were the one asked for."
            if not rows else
            "Use reservationId for any follow-up action. If more than one "
            "matched, ask which before doing anything."
        ),
    }


@tool(
    name="get_stay",
    description=(
        "One booking in full: status, dates, room, what has been paid and what "
        "is outstanding. Use before checking anyone in or out."
    ),
    input_model=ReservationArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="reservation",
)
async def get_stay(args: ReservationArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    found = await workspace_service.get_reservation(ctx.property_id, args.reservation_id)
    if not found:
        raise OutOfScope(
            f"reservation {args.reservation_id} not found at this property",
            "I can't find that booking here.",
        )
    row, balance = found
    numbers = await _room_numbers(ctx, [row])
    return _stay(row, balance, numbers.get(row.get("room_id")))


@tool(
    name="list_rooms",
    description=(
        "Every room, its housekeeping status and who is in it. The only source "
        "for whether a room is ready."
    ),
    input_model=ListRoomsArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_rooms(args: ListRoomsArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    if args.housekeeping_status and args.housekeeping_status not in HOUSEKEEPING_STATUSES:
        raise OutOfScope(
            f"housekeeping_status must be one of {', '.join(HOUSEKEEPING_STATUSES)}",
            "I don't recognise that room status.",
        )

    rooms, occupants = await workspace_service.list_rooms(ctx.property_id)
    if args.housekeeping_status:
        rooms = [r for r in rooms if r["housekeeping_status"] == args.housekeeping_status]

    return {
        "count": len(rooms),
        "rooms": [_room(r, occupants.get(r["id"])) for r in rooms],
    }


@tool(
    name="get_room",
    description=(
        "One room: is it clean, is it occupied, is it out of order. Use before "
        "saying a room is ready for anyone."
    ),
    input_model=RoomArgs,
    permission="frontdesk",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="room",
)
async def get_room(args: RoomArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    room = await _room_or_refuse(ctx, args.room_number)
    _, occupants = await workspace_service.list_rooms(ctx.property_id)
    return _room(room, occupants.get(room["id"]))


@tool(
    name="get_folio",
    description=(
        "A guest's account: every line, and the balance. The balance is computed "
        "by the system — report it, never add the lines up yourself."
    ),
    input_model=ReservationArgs,
    permission="folio",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="reservation",
)
async def get_folio(args: ReservationArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    stay = await _reservation_or_refuse(ctx, args.reservation_id)
    folio = await frontdesk_service.get_folio(ctx.property_id, args.reservation_id)

    return {
        "reservationId": args.reservation_id,
        # What the guest owes, not what the ledger sums to. Before arrival those
        # are different numbers with opposite signs: the folio holds the deposit
        # and nothing else, so its sum is negative, and a receptionist reading
        # this tool was being told a guest with ₹43,008 outstanding was owed
        # ₹10,752. `ledgerBalancePaise` keeps the accounting figure beside it,
        # because a folio screen that cannot show its own running total is not
        # a folio screen.
        "balancePaise": money.amount_due(stay, folio["entries"]),
        "ledgerBalancePaise": folio["balance_paise"],
        "entries": [
            {
                "kind": e["kind"],
                "description": e["description"],
                "amountPaise": e["amount_paise"],
                "method": e.get("method"),
                "postedAt": e.get("posted_at"),
                "postedBy": e.get("posted_by_name"),
            }
            for e in folio["entries"]
        ],
        "instruction": (
            "balancePaise is the answer to 'how much do they owe', in paise. "
            "Divide by 100 for rupees. Above zero means owing; zero means "
            "settled; below zero means the hotel owes them. Do not re-derive it."
        ),
    }


@tool(
    name="list_open_tasks",
    description="Jobs on the board: what is open, who has it, which room it is for.",
    input_model=ListTasksArgs,
    permission="tasks",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_open_tasks(args: ListTasksArgs, ctx: AgentContext) -> dict:
    _require_staff(ctx)
    rows = await workspace_service.list_tasks(ctx.property_id, limit=200)

    if args.status:
        if args.status not in TASK_STATUSES:
            raise OutOfScope(
                f"status must be one of {', '.join(TASK_STATUSES)}",
                "I don't recognise that job status.",
            )
        rows = [r for r in rows if r["status"] == args.status]
    else:
        rows = [r for r in rows if r["status"] not in ("resolved", "cancelled")]
    if args.category:
        rows = [r for r in rows if r["category"] == args.category]

    return {
        "count": len(rows),
        "tasks": [
            {
                "taskId": r["id"],
                "code": r["code"],
                "title": r["title"],
                "category": r["category"],
                "priority": r["priority"],
                "status": r["status"],
                "roomNumber": r.get("room_number"),
                "assignedTo": r.get("assignee_name"),
                "raisedAt": r.get("created_at"),
            }
            for r in rows[:60]
        ],
    }


# ═════════════════════════════════════════════════════════════════════════════
# Writes
# ═════════════════════════════════════════════════════════════════════════════

async def _revalidate_check_in(args: CheckInArgs, ctx: AgentContext) -> None:
    """
    Between the proposal and the approval, somebody may have walked in.

    The PMS refuses a check-in into an occupied room regardless, so this is not
    the safety net — it is the difference between the manager reading "room 102
    is already occupied" and reading "check_in failed". A proposal that has gone
    stale should say what changed.
    """
    stay = await _reservation_or_refuse(ctx, args.reservation_id)
    if stay["status"] == "checked_in":
        raise OutOfScope(
            f"reservation {stay['ref']} was already checked in while this waited for approval",
            f"{stay['ref']} has already been checked in.",
        )
    if stay["status"] not in ("confirmed", "pending"):
        raise OutOfScope(
            f"reservation {stay['ref']} is now {stay['status']} and cannot be checked in",
            f"{stay['ref']} is {stay['status'].replace('_', ' ')} — it can't be checked in.",
        )


@tool(
    name="check_in",
    description=(
        "Check a guest in and put them in a room. Look the booking up first; the "
        "system assigns the room and refuses if it is taken."
    ),
    input_model=CheckInArgs,
    permission="frontdesk",
    # Level 3. A check-in puts a named person behind a specific door, starts the
    # folio and takes the room off sale. It is reversible on paper and not in
    # the lobby, which is the distinction level 3 draws.
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="reservation",
    revalidate=_revalidate_check_in,
)
async def check_in(args: CheckInArgs, ctx: AgentContext) -> tuple[dict, str]:
    """
    The model names a booking and optionally a room. It cannot name a hotel.

    Both identifiers are resolved against `ctx.property_id` before the service
    is called, and the service checks the same thing again on both rows. So a
    reservation id belonging to another property resolves to nothing here, and a
    room id belonging to another property could not be produced in the first
    place — the model supplies a *number*, and numbers are looked up per
    property. There is no argument on this tool that crosses a hotel boundary.
    """
    _require_staff(ctx)
    stay = await _reservation_or_refuse(ctx, args.reservation_id)

    if args.room_number:
        room = await _room_or_refuse(ctx, args.room_number)
    elif stay.get("room_id"):
        room = await run_db(_room_by_id, ctx.property_id, stay["room_id"])
        if not room:
            raise OutOfScope(
                f"reservation {stay['ref']} points at a room that no longer exists",
                "The room on that booking isn't there any more — which room should they go in?",
            )
    else:
        raise OutOfScope(
            f"reservation {stay['ref']} has no room assigned and none was given",
            f"Which room should {stay.get('guest_name') or 'they'} go in?",
        )

    try:
        row = await frontdesk_service.check_in(
            ctx.property_id, args.reservation_id, room["id"], ctx.principal
        )
    except HTTPException as exc:
        raise _pms(exc, "check-in") from exc

    return (
        {
            "checkedIn": True,
            "reservationId": row["id"],
            "ref": stay["ref"],
            "guestName": stay.get("guest_name"),
            "roomNumber": room["number"],
            "status": row["status"],
            "instruction": (
                "Confirm the guest is in and give the room number. The first "
                "night has been posted to their folio automatically."
            ),
        },
        row["id"],
    )


@tool(
    name="check_out",
    description=(
        "Check a guest out. The system refuses while anything is outstanding on "
        "the folio — check get_folio first so you can say what is owed."
    ),
    input_model=ReservationArgs,
    permission="frontdesk",
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="reservation",
)
async def check_out(args: ReservationArgs, ctx: AgentContext) -> tuple[dict, str]:
    """
    The refusal is the feature.

    `pms.frontdesk.check_out` posts every outstanding night, sums the folio and
    refuses above zero. That check is not repeated here and must not be: a
    second implementation of "is this settled" is a second answer, and the one
    that matters is the one the desk's own button gets. The agent's job is to
    relay the number the PMS computed, which `_pms` carries through verbatim.
    """
    _require_staff(ctx)
    stay = await _reservation_or_refuse(ctx, args.reservation_id)

    try:
        row = await frontdesk_service.check_out(
            ctx.property_id, args.reservation_id, ctx.principal
        )
    except HTTPException as exc:
        raise _pms(exc, "check-out") from exc

    return (
        {
            "checkedOut": True,
            "reservationId": row["id"],
            "ref": stay["ref"],
            "guestName": stay.get("guest_name"),
            "status": row["status"],
            "instruction": (
                "The guest is checked out and their room is now marked dirty for "
                "housekeeping. Nothing further is needed."
            ),
        },
        row["id"],
    )


@tool(
    name="set_room_status",
    description=(
        "Change a room's housekeeping status: clean, dirty, inspected or out of "
        "order. Use when housekeeping reports a room done, or a room has to come "
        "off sale."
    ),
    input_model=SetRoomStatusArgs,
    permission="housekeeping",
    # Level 2. Housekeeping statuses change a dozen times a day, every change is
    # visible on the board, and the correction for a wrong one is another one.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="room",
)
async def set_room_status(args: SetRoomStatusArgs, ctx: AgentContext) -> tuple[dict, str]:
    _require_staff(ctx)
    if args.status not in HOUSEKEEPING_STATUSES:
        raise OutOfScope(
            f"status must be one of {', '.join(HOUSEKEEPING_STATUSES)}, got {args.status!r}",
            "I don't recognise that room status.",
        )

    room = await _room_or_refuse(ctx, args.room_number)
    try:
        row = await frontdesk_service.set_room_status(ctx.property_id, room["id"], args.status)
    except HTTPException as exc:
        raise _pms(exc, "the room status update") from exc

    return (
        {
            "roomNumber": row["number"],
            "housekeepingStatus": row["housekeeping_status"],
            "instruction": "Confirm the new status in a few words.",
        },
        row["id"],
    )


@tool(
    name="update_task_status",
    description=(
        "Move a job along: accepted, in progress, resolved, cancelled. Only say "
        "a job is done after this has succeeded."
    ),
    input_model=TaskStatusArgs,
    permission="tasks",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="task",
)
async def update_task_status(args: TaskStatusArgs, ctx: AgentContext) -> tuple[dict, str]:
    _require_staff(ctx)
    if args.status not in TASK_STATUSES:
        raise OutOfScope(
            f"status must be one of {', '.join(TASK_STATUSES)}, got {args.status!r}",
            "I don't recognise that job status.",
        )

    try:
        row = await frontdesk_service.set_task_status(
            ctx.property_id, args.task_id, args.status, ctx.principal
        )
    except HTTPException as exc:
        raise _pms(exc, "the job update") from exc

    return (
        {
            "taskId": row["id"],
            "code": row["code"],
            "status": row["status"],
            "instruction": "Confirm the job's new status. Do not embellish it.",
        },
        row["id"],
    )


@tool(
    name="post_folio_charge",
    description=(
        "Put a charge on a guest's bill — a laundry, a minibar, a late checkout. "
        "Amount in paise, always positive. This is the only way to add to a "
        "folio, and it cannot take payments or give refunds."
    ),
    input_model=FolioChargeArgs,
    permission="folio",
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="folio_entry",
    creates_result=True,
    # Independent of the autonomy dial, and not switchable off.
    #
    # A hotel that raises its front desk agent to level 3 wants it to check
    # guests in without being asked twice. It does not thereby want money
    # appearing on bills unwatched, and the two decisions arrive together unless
    # one of them is nailed down. This is the one that gets nailed down: a
    # charge is the only thing here a guest disputes at the desk two days later.
    always_requires_approval=True,
    when_deferred=(
        "The charge is NOT on the bill and the guest owes nothing extra yet. "
        "The hotel has to agree it first. Tell the guest you have asked the "
        "team to add it, and carry on with whatever else they need — do not "
        "apologise, and never read the new total back to them as though it "
        "had gone on."
    ),
)
async def post_folio_charge(args: FolioChargeArgs, ctx: AgentContext) -> tuple[dict, str]:
    """
    One kind, one sign, one idempotency key.

    The key is derived from the run and the charge itself, never from the clock.
    An approval that is retried, a webhook redelivered, a model that repeats the
    same call inside one turn — all three resolve to the same folio line, which
    is the same guarantee the offline outbox relies on and the reason
    `post_folio_entry` takes a key at all.
    """
    _require_staff(ctx)
    stay = await _reservation_or_refuse(ctx, args.reservation_id)

    if stay["status"] in ("cancelled", "no_show", "checked_out"):
        raise OutOfScope(
            f"reservation {stay['ref']} is {stay['status']} — nothing more can be posted to it",
            f"{stay['ref']} is already {stay['status'].replace('_', ' ')}, "
            "so I can't add to that bill.",
        )

    fingerprint = hashlib.sha256(
        f"{args.reservation_id}|{args.description}|{args.amount_paise}".encode()
    ).hexdigest()[:16]
    key = f"agent:{ctx.run_id or ctx.principal.agent_id}:{fingerprint}"

    try:
        entry = await frontdesk_service.post_folio_entry(
            ctx.property_id,
            args.reservation_id,
            kind=AGENT_FOLIO_KIND,
            description=args.description,
            amount_paise=args.amount_paise,
            method=None,
            idempotency_key=key,
            member=ctx.principal,
        )
    except HTTPException as exc:
        raise _pms(exc, "the folio charge") from exc

    folio = await frontdesk_service.get_folio(ctx.property_id, args.reservation_id)

    return (
        {
            "posted": True,
            "entryId": entry["id"],
            "description": entry["description"],
            "amountPaise": entry["amount_paise"],
            "balancePaise": money.amount_due(stay, folio["entries"]),
            "instruction": (
                "The charge is on the bill. Quote the new balance from "
                "balancePaise — it is the system's figure, not a sum you should "
                "check."
            ),
        },
        entry["id"],
    )


@tool(
    name="create_staff_task",
    description=(
        "Raise a job for the team, for a specific room or a public area. Give "
        "the room number the person at the desk said; it is checked against the "
        "actual rooms and refused if there is no such room."
    ),
    input_model=StaffTaskArgs,
    permission="tasks",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="task",
    creates_result=True,
)
async def create_staff_task(args: StaffTaskArgs, ctx: AgentContext) -> tuple[dict, str]:
    """
    The Guest agent's `create_task` with the room resolution turned around.

    On a guest conversation the reservation is authoritative and a room number
    from the model is overridden — a guest does not need to tell us which door
    they are behind, and the first version of that tool put a maintenance job on
    the wrong one. At the desk the opposite holds: the receptionist is looking at
    the board and says "209", and there is no conversation-level reservation to
    override it with.

    So the room is taken from the argument *and verified to exist*. A number that
    matches no room is refused rather than stored as text, which is what the PMS
    would otherwise do — correct for a human typing "Lobby", wrong for a model
    that misheard a number. When the room has a guest in it, the ticket is linked
    to their stay, so whoever picks the job up knows whose room it is without
    anybody having typed a name.
    """
    _require_staff(ctx)

    from app.services.ai.tools.operations import CATEGORIES, PRIORITIES

    if args.category not in CATEGORIES:
        raise OutOfScope(
            f"category must be one of {', '.join(CATEGORIES)}, got {args.category!r}",
            "I don't recognise that job category.",
        )
    if args.priority not in PRIORITIES:
        raise OutOfScope(
            f"priority must be one of {', '.join(PRIORITIES)}, got {args.priority!r}",
            "I don't recognise that priority.",
        )

    room = None
    occupant = None
    if args.room_number:
        room = await _room_or_refuse(ctx, args.room_number)
        _, occupants = await workspace_service.list_rooms(ctx.property_id)
        occupant = occupants.get(room["id"])

    task_id = str(uuid.uuid4())
    try:
        task = await frontdesk_service.create_task(
            ctx.property_id,
            task_id=task_id,
            title=args.title,
            category=args.category,
            priority=args.priority,
            room_number=room["number"] if room else None,
            conversation_id=ctx.conversation_id,
            source_channel=None,
            guest_name=(occupant or {}).get("guest_name"),
            created_by=ACTOR_TASK,
            member=ctx.principal,
        )
    except HTTPException as exc:
        raise _pms(exc, "raising the job") from exc
    except Exception as exc:  # noqa: BLE001
        raise ToolUnavailable(
            f"could not raise the ticket: {type(exc).__name__}: {exc}",
            "I couldn't log that just now.",
        ) from exc

    return (
        {
            "taskId": task["id"],
            "code": task["code"],
            "title": task["title"],
            "category": task["category"],
            "priority": task["priority"],
            "status": task["status"],
            "roomNumber": task.get("room_number"),
            "guestName": task.get("guest_name"),
            "assignedTo": task.get("assignee_name"),
            "instruction": (
                "Give the ticket code. Nobody is assigned yet — only assign_task "
                "changes that, and only after it returns a name."
            ),
        },
        task["id"],
    )


# ── helpers the tools share ──────────────────────────────────────────────────

def _room(row: dict, occupant: dict | None) -> dict:
    return {
        "number": row["number"],
        "floor": row.get("floor"),
        "housekeepingStatus": row["housekeeping_status"],
        "outOfOrder": row["housekeeping_status"] == "out_of_order",
        "outOfOrderReason": row.get("out_of_order_reason"),
        "active": row.get("is_active", True),
        "occupied": occupant is not None,
        "occupantName": (occupant or {}).get("guest_name"),
        "occupantRef": (occupant or {}).get("ref"),
        "occupantUntil": (occupant or {}).get("check_out"),
        # Said explicitly, because "is 102 ready" is two questions and answering
        # only the housekeeping half is how a guest is walked into an occupied
        # room.
        "readyForCheckIn": (
            occupant is None
            and row.get("is_active", True)
            and row["housekeeping_status"] in ("clean", "inspected")
        ),
    }


def _room_by_id(property_id: str, room_id: str) -> dict | None:
    rows = (
        get_db().table("rooms").select("*")
        .eq("id", room_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


async def _room_numbers(ctx: AgentContext, rows: list[dict]) -> dict[str, str]:
    """
    Room ids to numbers for a batch of stays, in one query.

    A join per reservation would be twenty round trips on a twenty-arrival
    morning, which is the screen a receptionist reloads all shift.
    """
    ids = sorted({r["room_id"] for r in rows if r.get("room_id")})
    if not ids:
        return {}

    def _read():
        return (
            get_db().table("rooms").select("id,number")
            .eq("property_id", ctx.property_id)
            .in_("id", ids).execute().data or []
        )

    return {r["id"]: r["number"] for r in await run_db(_read)}
