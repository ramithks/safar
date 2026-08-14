"""
Operations — the first thing an AI should be trusted with.

"My AC isn't working" becomes a ticket on the same board a receptionist raises
tickets on, with the same code, the same categories and the same event trail.
There is no AI task system, because a job the AI raised and a job a human raised
are the same job and a housekeeper should not have to look in two places.

Two details do the heavy lifting:

**The ticket id is generated here, before the call.** `create_task` is a
no-op on replay against the same id, so a model that repeats a tool call — or a
webhook Meta delivers twice — produces one ticket. The idempotency argument is
the same one the offline outbox relies on.

**Assignment is real or absent.** `assign_task` puts a named member on the job
or refuses; it never returns a name it did not write. The failure mode this
avoids is the one that matters most in the whole product: telling a guest
"Rahul is on his way" when nobody was told anything.
"""
import uuid

from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.registry import tool
from app.services.ai.tools import ACTOR_TASK
from app.services.ai.tools import read as read_tools
from app.services.pms import frontdesk as frontdesk_service

CATEGORIES = ("housekeeping", "maintenance", "concierge", "f_and_b", "front_desk", "accounts", "manager")
PRIORITIES = ("low", "normal", "high", "urgent")


class CreateTaskArgs(BaseModel):
    title: str = Field(
        min_length=4, max_length=120,
        description=(
            "What needs doing, in the hotel's words: 'air conditioning not "
            "cooling', 'two extra towels', 'tap dripping in the bathroom'. "
            "Do not put a room number in the title — the room is set from the "
            "guest's booking."
        ),
    )
    category: str = Field(description=f"One of: {', '.join(CATEGORIES)}.")
    priority: str = Field(default="normal", description=f"One of: {', '.join(PRIORITIES)}.")
    room_number: str | None = Field(
        default=None, max_length=12,
        description=(
            "Only for a job that is NOT in this guest's own room — a public "
            "area, or a room they have named explicitly. Leave empty otherwise: "
            "their room comes from their booking."
        ),
    )


class AssignTaskArgs(BaseModel):
    task_id: str
    member_id: str | None = Field(
        default=None,
        description="Leave empty to route by category to whoever covers it.",
    )


class TaskStatusArgs(BaseModel):
    task_id: str


@tool(
    name="create_task",
    description=(
        "Raise a job for the hotel team: a maintenance fault, a housekeeping "
        "request, an errand. Use this whenever a guest asks for something a "
        "person has to do. Returns the ticket code — you may tell the guest it."
    ),
    input_model=CreateTaskArgs,
    permission="tasks",
    # Level 2, the level a Guest agent ships at. Raising a ticket costs nothing,
    # is visible to staff immediately, and is trivially reversible.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="task",
    creates_result=True,
)
async def create_task(args: CreateTaskArgs, ctx: AgentContext) -> tuple[dict, str]:
    if args.category not in CATEGORIES:
        raise OutOfScope(
            f"category must be one of {', '.join(CATEGORIES)}, got {args.category!r}",
            "I've noted that — let me get someone from the team to you.",
        )
    if args.priority not in PRIORITIES:
        raise OutOfScope(
            f"priority must be one of {', '.join(PRIORITIES)}, got {args.priority!r}",
            "I've noted that — let me get someone from the team to you.",
        )

    # The guest's own room wins over anything the model supplied.
    #
    # The reservation is authoritative about which door this guest is behind;
    # a `room_number` argument is a string a language model produced, and the
    # first version of this tool had an example containing a room number, which
    # the model then reused for a guest staying somewhere else. A maintenance
    # job on the wrong door is a stranger being woken up at eleven at night.
    #
    # The argument is still honoured when the conversation has no booking — a
    # walk-in enquiry, or somebody reporting a broken light in the lobby.
    room = None
    if ctx.reservation_id:
        def _reservation():
            rows = (
                get_db()
                .table("reservations")
                .select("room_id")
                .eq("id", ctx.reservation_id)
                .eq("property_id", ctx.property_id)
                .limit(1)
                .execute()
                .data
                or []
            )
            return rows[0] if rows else None

        stay = await run_db(_reservation)
        room = await read_tools.room_number_for(stay) if stay else None

    if not room:
        room = args.room_number

    task_id = str(uuid.uuid4())
    try:
        task = await frontdesk_service.create_task(
            ctx.property_id,
            task_id=task_id,
            title=args.title,
            category=args.category,
            priority=args.priority,
            room_number=room,
            conversation_id=ctx.conversation_id,
            source_channel=ctx.channel if ctx.channel in
                ("whatsapp", "instagram", "website", "phone", "email") else None,
            guest_name=ctx.guest_name,
            created_by=ACTOR_TASK,
            member=ctx.principal,
        )
    except Exception as exc:  # noqa: BLE001
        raise ToolUnavailable(
            f"could not raise the ticket: {type(exc).__name__}: {exc}",
            "I couldn't log that just now — I'm putting you through to the team.",
        ) from exc

    # `tasks.agent_action_id` is filled in by the runtime, not here: the action
    # row is written after the tool returns, so this function does not yet know
    # the id it would need. See `runtime._link_result`.

    return (
        {
            "taskId": task["id"],
            "code": task["code"],
            "title": task["title"],
            "category": task["category"],
            "priority": task["priority"],
            "status": task["status"],
            "roomNumber": task.get("room_number"),
            "assignedTo": task.get("assignee_name"),
            "instruction": (
                "Tell the guest it is logged and give them the code. Do not say "
                "anyone is on their way or that it is fixed — nobody has been "
                "assigned yet, and only assign_task changes that."
            ),
        },
        task["id"],
    )


@tool(
    name="assign_task",
    description=(
        "Put a named member of staff on a job. Only say a person is handling "
        "something after this has succeeded and returned their name."
    ),
    input_model=AssignTaskArgs,
    permission="tasks",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="task",
)
async def assign_task(args: AssignTaskArgs, ctx: AgentContext) -> tuple[dict, str]:
    await ctx.require_task(args.task_id)

    def _write():
        db = get_db()

        task = (
            db.table("tasks")
            .select("id,category,status,assignee_member,assignee_name")
            .eq("id", args.task_id)
            .eq("property_id", ctx.property_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        if not task:
            return None
        t = task[0]

        # A member's name lives on their profile, not on the membership: one
        # person can be staff at three properties and is not three names.
        def _name_of(profile_id: str | None) -> str | None:
            if not profile_id:
                return None
            rows = (
                db.table("profiles").select("full_name")
                .eq("id", profile_id).limit(1).execute().data or []
            )
            return rows[0].get("full_name") if rows else None

        member_id = args.member_id
        if not member_id:
            # Route by category. The same mapping the desk uses when it drops a
            # ticket on a queue rather than on a person.
            wanted = {
                "housekeeping": "housekeeping",
                "maintenance": "maintenance",
                "concierge": "concierge",
                "f_and_b": "restaurant",
                "front_desk": "receptionist",
                "accounts": "accounts",
                "manager": "manager",
            }.get(t["category"])
            candidates = (
                db.table("partner_members")
                .select("id,profile_id,role,property_ids")
                .eq("partner_id", ctx.partner_id)
                .eq("status", "active")
                .eq("role", wanted)
                .limit(10)
                .execute()
                .data
                or []
            ) if wanted else []
            candidates = [
                c for c in candidates
                if not c.get("property_ids") or ctx.property_id in c["property_ids"]
            ]
            if not candidates:
                return {"assigned": False, "task": t}
            member_id = candidates[0]["id"]
            name = _name_of(candidates[0].get("profile_id"))
        else:
            found = (
                db.table("partner_members")
                .select("id,profile_id,property_ids")
                .eq("id", member_id)
                .eq("partner_id", ctx.partner_id)
                .eq("status", "active")
                .limit(1)
                .execute()
                .data
                or []
            )
            if not found:
                return {"assigned": False, "task": t, "unknown_member": True}
            name = _name_of(found[0].get("profile_id"))

        # Never claim an assignment we cannot name. A ticket on an anonymous
        # member is a ticket the guest gets told a name for that does not exist.
        if not name:
            return {"assigned": False, "task": t}

        updated = (
            db.table("tasks")
            .update({"assignee_member": member_id, "assignee_name": name})
            .eq("id", args.task_id)
            .execute()
            .data[0]
        )
        return {"assigned": True, "task": updated, "name": name}

    result = await run_db(_write)
    if result is None:
        raise OutOfScope(f"task {args.task_id} not found",
                         "I can't find that request — let me check with the team.")

    if not result["assigned"]:
        # Honest, and the guest-facing message says nothing about a person.
        return (
            {
                "assigned": False,
                "instruction": (
                    "Nobody is on this yet. Tell the guest it is logged and the "
                    "team will pick it up. Do not name anyone."
                ),
            },
            args.task_id,
        )

    return (
        {
            "assigned": True,
            "assignedTo": result["name"],
            "taskCode": result["task"].get("code"),
            "instruction": "You may now tell the guest this person is handling it.",
        },
        args.task_id,
    )


@tool(
    name="get_task_status",
    description="Where a job has got to. Use before telling a guest anything about progress.",
    input_model=TaskStatusArgs,
    permission="tasks",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="task",
    scoped_to_conversation=True,
)
async def get_task_status(args: TaskStatusArgs, ctx: AgentContext) -> dict:
    await ctx.require_task(args.task_id)

    def _read():
        rows = (
            get_db()
            .table("tasks")
            .select("id,code,status,assignee_name,priority,resolved_at")
            .eq("id", args.task_id)
            .eq("property_id", ctx.property_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    row = await run_db(_read)
    if not row:
        raise OutOfScope(f"task {args.task_id} not found",
                         "I can't find that request — let me check with the team.")

    return {
        "taskId": row["id"],
        "code": row["code"],
        "status": row["status"],
        "assignedTo": row.get("assignee_name"),
        "resolvedAt": row.get("resolved_at"),
        "instruction": (
            "Report exactly this status. 'resolved' means the team marked it "
            "done; anything else means it is not done yet."
        ),
    }
