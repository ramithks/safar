"""
Read tools.

Two kinds of answer live in here and the agent is required to keep them apart:

    **Hotel knowledge** — breakfast is 08:00, dogs are allowed in the garden
    rooms. Written by the hotel, read from `property_knowledge`, changes rarely.

    **Operational truth** — this room is free, this guest owes ₹3,264. Derived
    from the live system, changes every minute, and never guessed.

The separation is why they are different tools reading different tables rather
than one "ask the hotel anything". A wrong breakfast time is an apology; a wrong
availability is a double booking.

`get_property_knowledge` returning nothing is a *correct* answer and the most
important one this module produces. It is what turns "I don't know, let me get
someone" into the designed behaviour instead of the failure case.
"""
from datetime import date

from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.pms import availability as availability_service
from app.services.pms import clock
from app.services.pms import money
from app.services.pms import onboarding
from app.services.pms import policies
from app.services.pms import workspace as workspace_service


# Words that appear in every guest message and carry no subject. Without this,
# "do **you** have a helipad?" matches any answer containing "you" — which is
# most of them, since answers are written for people to read.
STOPWORDS = frozenset("""
    the and but for with from this that these those there they them their
    what how why when who which much many any some about into out over
    are was were can could would will shall may might have has had does did
    you your yours our ours mine his her hers its
    please hello thanks thank hey yeah okay
    tell know want need get got give take make sure still just also very really
    guys hotel place room stay staying booked booking
""".split())

# ── arguments ────────────────────────────────────────────────────────────────

class NoArgs(BaseModel):
    pass


class KnowledgeArgs(BaseModel):
    question: str = Field(
        min_length=2, max_length=300,
        description="The guest's question, in their own words.",
    )
    category: str | None = Field(
        default=None,
        description=(
            "Narrow the search: checkin, checkout, breakfast, dining, parking, "
            "pets, children, wifi, amenities, directions, house_rules, "
            "cancellation, payment, services, accessibility, local."
        ),
    )


class ReservationArgs(BaseModel):
    reservation_id: ResourceId = Field(description="The reservation's id.")


class TaskArgs(BaseModel):
    task_id: ResourceId = Field(description="The task's id.")


class AvailabilityArgs(BaseModel):
    check_in: date = Field(description="Arrival date, YYYY-MM-DD.")
    check_out: date = Field(description="Departure date, YYYY-MM-DD. Must be after check_in.")
    units: int = Field(default=1, ge=1, le=10, description="How many rooms.")
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=10)



async def room_number_for(reservation: dict) -> str | None:
    """
    Which room a stay is in.

    `reservations` carries `room_id`, not a number — the number belongs to the
    room, and denormalising it would leave two answers to "which room is this
    guest in" the first time housekeeping renumbers a floor. So it is a join,
    done here rather than in three tools that would each get it slightly
    different.
    """
    room_id = reservation.get("room_id")
    if not room_id:
        return None

    def _read():
        rows = (
            get_db().table("rooms").select("number")
            .eq("id", room_id).limit(1).execute().data or []
        )
        return rows[0]["number"] if rows else None

    return await run_db(_read)


# ── the property, and what it has written down ───────────────────────────────

@tool(
    name="get_property",
    description=(
        "Basic facts about this hotel: name, city, check-in and check-out times, "
        "currency. Use before quoting times or location."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_property(_args: NoArgs, ctx: AgentContext) -> dict:
    def _read():
        rows = (
            get_db()
            .table("properties")
            .select("name,city,state,country,property_type,check_in_time,"
                    "check_out_time,currency,timezone")
            .eq("id", ctx.property_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    row = await run_db(_read)
    if not row:
        raise ToolUnavailable("the property record could not be read")

    return {
        "name": row["name"],
        "city": row.get("city"),
        "state": row.get("state"),
        "country": row.get("country"),
        "type": row.get("property_type"),
        "checkInTime": str(row.get("check_in_time") or ""),
        "checkOutTime": str(row.get("check_out_time") or ""),
        "currency": row.get("currency"),
        "today": clock.business_date_iso(ctx.property_id),
    }


@tool(
    name="get_property_policies",
    description=(
        "The hotel's policies: cancellation, children, pets, identification. "
        "Use before answering any question about what is or is not allowed."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_property_policies(_args: NoArgs, ctx: AgentContext) -> dict:
    def _read():
        rows = (
            get_db()
            .table("properties")
            .select("policies,tax_inclusive,check_in_time,check_out_time")
            .eq("id", ctx.property_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    row = await run_db(_read)
    if not row:
        raise ToolUnavailable("the property record could not be read")

    # House rules only. The same column carries `max_discount_pct` and
    # `token_pct` — the hotel's negotiating ceiling — and this tool puts
    # whatever it returns into the model's context on any house-rules turn,
    # one paraphrase away from a guest reading it.
    policies = onboarding.guest_policies(row.get("policies"))
    return {
        "policies": policies,
        "checkInTime": str(row.get("check_in_time") or ""),
        "checkOutTime": str(row.get("check_out_time") or ""),
        "ratesIncludeTax": bool(row.get("tax_inclusive")),
        # Said plainly, because an empty policies object is the case an agent is
        # most likely to fill in with something plausible.
        "note": (
            "No written policy for this. Say you will check with the team."
            if not policies else None
        ),
    }


@tool(
    name="get_property_knowledge",
    description=(
        "Search what the hotel has written down: breakfast times, parking, WiFi, "
        "pets, directions, house rules. Returns nothing when the hotel has not "
        "recorded an answer — say you do not know and offer a person. Never "
        "answer a hotel-specific question without calling this first."
    ),
    input_model=KnowledgeArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_property_knowledge(args: KnowledgeArgs, ctx: AgentContext) -> dict:
    """
    Category and keywords, scored. Not embeddings.

    A hotel has perhaps forty facts a guest asks about and they have names, so
    the retrieval problem is small enough that exact matching wins on both
    accuracy and honesty. A similarity score cannot say "we have not written
    that down", and that is the answer this tool most needs to be able to give.
    """
    words = {
        w.strip(".,?!'\"").lower() for w in args.question.split() if len(w) > 2
    } - STOPWORDS

    def _read():
        db = get_db()
        q = (
            db.table("property_knowledge")
            .select("category,question,answer,keywords,confidence")
            .eq("property_id", ctx.property_id)
            .eq("is_active", True)
        )
        if args.category:
            q = q.eq("category", args.category)
        return q.order("sort_order").limit(60).execute().data or []

    rows = await run_db(_read)

    # The agent's configuration may narrow which categories it draws on.
    if ctx.knowledge_scope:
        rows = [r for r in rows if r["category"] in ctx.knowledge_scope]

    def score(row: dict) -> int:
        """
        Match on what the fact is *about*, never on the prose of the answer.

        Scoring against the answer text is how "do you have a helipad?" came
        back with the check-in policy: the answer happened to contain the word
        "you". An answer is written for a guest to read, so it is full of
        ordinary words, and matching on it turns every question into a hit —
        which destroys the one thing this tool most needs to be able to say.
        """
        keywords = {k.lower() for k in (row.get("keywords") or [])}
        question = {
            w.strip(".,?!'\"").lower() for w in row["question"].split()
        } - STOPWORDS

        hits = 3 * len(words & keywords) + 2 * len(words & question)
        if args.category and row["category"] == args.category:
            hits += 3
        return hits

    ranked = sorted(((score(r), r) for r in rows), key=lambda p: -p[0])
    # Three is one keyword hit, or an explicit category. A single word shared
    # with a question title is not a match, it is a coincidence.
    matched = [r for s, r in ranked if s >= 3][:4]

    if not matched:
        return {
            "found": False,
            "facts": [],
            "instruction": (
                "The hotel has not written down an answer to this. Tell the guest "
                "you do not have that information and offer to connect them with "
                "the hotel team. Do not guess."
            ),
        }

    return {
        "found": True,
        "facts": [
            {
                "category": r["category"],
                "question": r["question"],
                "answer": r["answer"],
                "confidence": r["confidence"],
            }
            for r in matched
        ],
        "instruction": (
            "Answer from these facts and nothing else. If they do not cover what "
            "was asked, say so and offer the hotel team."
        ),
    }


@tool(
    name="get_room_types",
    description=(
        "The rooms this hotel has, with occupancy. Descriptive only — it says "
        "nothing about whether any are free. Use search_availability for that."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_room_types(_args: NoArgs, ctx: AgentContext) -> list[dict]:
    rows = await workspace_service.list_room_types(ctx.property_id)
    return [
        {
            "id": r["id"],
            "code": r["code"],
            "name": r["name"],
            "maxOccupancy": r.get("max_occupancy"),
            "bedType": r.get("bed_type"),
            # The three the hotel fills in, and the reason this tool exists at
            # all. Without them a guest asking "what's in the Deluxe?" or
            # "photos hain?" got "I can only give you information in text" —
            # which was true and useless, and happened twice in one real
            # conversation while the guest was trying to buy.
            "description": r.get("description") or None,
            "sizeSqft": r.get("size_sqft"),
            "amenities": r.get("amenities") or [],
            # Links the hotel gave us. There is no image hosting here, so these
            # are the hotel's own URLs and may be sent as links.
            "photos": r.get("photos") or [],
        }
        for r in rows
    ]


@tool(
    name="list_services",
    description=(
        "Paid extras the hotel offers: transfers, dinners, late checkout. "
        "Prices are in paise and come from the hotel's own list."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def list_services(_args: NoArgs, ctx: AgentContext) -> list[dict]:
    def _read():
        return (
            get_db()
            .table("services")
            .select("id,name,description,category,price_paise,lead_time_hours")
            .eq("property_id", ctx.property_id)
            .eq("is_active", True)
            .order("sort_order")
            .limit(50)
            .execute()
            .data
            or []
        )

    return [
        {
            "id": r["id"],
            "name": r["name"],
            "description": r.get("description"),
            "category": r["category"],
            "pricePaise": r["price_paise"],
            "leadTimeHours": r["lead_time_hours"],
        }
        for r in await run_db(_read)
    ]


# ── the guest, and what belongs to them ──────────────────────────────────────

@tool(
    name="get_guest",
    description=(
        "Who this conversation is with, if the hotel already knows them. Takes "
        "no arguments — the guest is whoever this chat belongs to."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    scoped_to_conversation=True,
)
async def get_guest(_args: NoArgs, ctx: AgentContext) -> dict:
    """
    Deliberately argument-free.

    A `guest_id` parameter would be a parameter the model can be talked into
    filling with someone else's id. The guest is a property of the conversation,
    so it comes from the context and there is nothing to talk it into.
    """
    if not ctx.guest_id:
        return {
            "known": False,
            "name": ctx.guest_name,
            "instruction": (
                "This person is not in the guest list yet. Do not claim to know "
                "anything about their history."
            ),
        }

    def _read():
        rows = (
            get_db()
            .table("guests")
            .select("name,city,country,stays_count,last_stay_at,vip,preferences")
            .eq("id", ctx.guest_id)
            .eq("property_id", ctx.property_id)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    row = await run_db(_read)
    if not row:
        return {"known": False, "name": ctx.guest_name}

    # No phone, no email, no ID digits. A guest agent has no use for them and a
    # transcript that carries them is a transcript that leaks them.
    return {
        "known": True,
        "name": row["name"],
        "city": row.get("city"),
        "previousStays": row.get("stays_count", 0),
        "lastStay": row.get("last_stay_at"),
        "vip": bool(row.get("vip")),
        "preferences": row.get("preferences") or {},
    }


@tool(
    name="list_guest_reservations",
    description=(
        "Every booking belonging to this conversation's guest. Takes no "
        "arguments. Use this before asking a guest for their booking reference."
    ),
    input_model=NoArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    scoped_to_conversation=True,
)
async def list_guest_reservations(_args: NoArgs, ctx: AgentContext) -> list[dict]:
    allowed = await ctx.allowed_reservations()
    if not allowed:
        return []

    def _read():
        return (
            get_db()
            .table("reservations")
            .select("id,ref,status,check_in,check_out,nights,units,guest_name,room_id")
            .in_("id", sorted(allowed))
            .eq("property_id", ctx.property_id)
            .order("check_in", desc=True)
            .limit(20)
            .execute()
            .data
            or []
        )

    return [
        {
            "id": r["id"],
            "ref": r["ref"],
            "status": r["status"],
            "checkIn": r["check_in"],
            "checkOut": r["check_out"],
            "nights": r["nights"],
            "rooms": r["units"],
        }
        for r in await run_db(_read)
    ]


@tool(
    name="get_reservation",
    description=(
        "One booking in full, including what is owed. Only works for a booking "
        "belonging to this conversation."
    ),
    input_model=ReservationArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="reservation",
    scoped_to_conversation=True,
)
async def get_reservation(args: ReservationArgs, ctx: AgentContext) -> dict:
    # The check that makes "show me reservation TB-4K2M9X" a refusal rather than
    # a disclosure. It runs before the read, not after.
    await ctx.require_reservation(args.reservation_id)

    found = await workspace_service.get_reservation(ctx.property_id, args.reservation_id)
    if not found:
        raise OutOfScope(
            f"reservation {args.reservation_id} not found",
            "I can't find that booking — could you check the reference?",
        )
    row, folio = found
    # `folio` is a grouped `folio_states` row, and the balance it produces is
    # the one the guest's own receipt shows. It used to be the folio's raw sum,
    # which before arrival is the deposit alone and therefore negative.
    purse = money.state(row, money.rows_from_state(folio))

    return {
        "roomNumber": await room_number_for(row),
        "id": row["id"],
        "ref": row["ref"],
        "status": row["status"],
        "guestName": row.get("guest_name"),
        "checkIn": row["check_in"],
        "checkOut": row["check_out"],
        "nights": row["nights"],
        "rooms": row["units"],
        "totalPaise": purse.total_paise,
        "paidPaise": purse.paid_paise,
        "balancePaise": purse.balance_due_paise,
        "paymentStatus": row.get("payment_status"),
        "checkInTimeNote": "Arrival time is the property's, see get_property.",
    }


# ── tickets ──────────────────────────────────────────────────────────────────

@tool(
    name="get_task",
    description="One request the guest has already raised, and where it stands.",
    input_model=TaskArgs,
    permission="tasks",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    result_type="task",
    scoped_to_conversation=True,
)
async def get_task(args: TaskArgs, ctx: AgentContext) -> dict:
    await ctx.require_task(args.task_id)

    def _read():
        rows = (
            get_db()
            .table("tasks")
            .select("id,code,title,category,priority,status,assignee_name,"
                    "room_number,created_at,resolved_at")
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
        "id": row["id"],
        "code": row["code"],
        "title": row["title"],
        "category": row["category"],
        "priority": row["priority"],
        "status": row["status"],
        "assignedTo": row.get("assignee_name"),
        "raisedAt": row.get("created_at"),
        "resolvedAt": row.get("resolved_at"),
    }


@tool(
    name="list_guest_tasks",
    description=(
        "Everything this guest has asked for in this conversation, and where "
        "each stands. Takes no arguments."
    ),
    input_model=NoArgs,
    permission="tasks",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
    scoped_to_conversation=True,
)
async def list_guest_tasks(_args: NoArgs, ctx: AgentContext) -> list[dict]:
    allowed = await ctx.allowed_reservations()

    def _read():
        db = get_db()
        rows: list[dict] = []
        cols = "id,code,title,category,priority,status,assignee_name,created_at"
        if ctx.conversation_id:
            rows += (
                db.table("tasks").select(cols)
                .eq("property_id", ctx.property_id)
                .eq("conversation_id", ctx.conversation_id)
                .order("created_at", desc=True).limit(20).execute().data or []
            )
        if allowed:
            rows += (
                db.table("tasks").select(cols)
                .eq("property_id", ctx.property_id)
                .in_("reservation_id", sorted(allowed))
                .order("created_at", desc=True).limit(20).execute().data or []
            )
        return rows

    seen: set[str] = set()
    out: list[dict] = []
    for r in await run_db(_read):
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({
            "id": r["id"],
            "code": r["code"],
            "title": r["title"],
            "category": r["category"],
            "priority": r["priority"],
            "status": r["status"],
            "assignedTo": r.get("assignee_name"),
            "raisedAt": r.get("created_at"),
        })
    return out


# ── availability, which the model never computes ─────────────────────────────

@tool(
    name="search_availability",
    description=(
        "Whether the hotel has rooms free for a stay, and how many of each room "
        "type are left. Use this only when the guest asks nothing more than "
        "'do you have anything'. If they want to see the room options or a "
        "price, call get_stay_options instead — it returns everything this "
        "does. Never say a room is free without calling one of the two."
    ),
    input_model=AvailabilityArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def search_availability(args: AvailabilityArgs, ctx: AgentContext) -> dict:
    if args.check_out <= args.check_in:
        raise OutOfScope(
            "check_out must be after check_in",
            "Could you tell me the dates again — which night would you arrive?",
        )

    # 'public' audience: a guest on WhatsApp must not be shown agent or
    # corporate rates. The front desk passes None because it books for agents;
    # this decision belongs to the caller, and the caller here is a guest.
    options = await availability_service.stay_options(
        ctx.property_id, args.check_in, args.check_out,
        units=args.units, audience="public",
        # A room this conversation is already holding is still available to
        # it. Without this, quoting takes a hold and the very next
        # availability check reports the rooms it just quoted as gone.
        for_conversation=ctx.conversation_id,
    )

    sellable = [o for o in options if o.units_left >= args.units]
    return {
        "checkIn": str(args.check_in),
        "checkOut": str(args.check_out),
        "nights": (args.check_out - args.check_in).days,
        "anyAvailable": bool(sellable),
        "roomTypes": [
            {
                "roomTypeId": o.room_type_id,
                "code": o.room_type_code,
                "name": o.room_type_name,
                "unitsLeft": o.units_left,
                "maxOccupancy": o.max_occupancy,
                "sleepsRequestedParty": o.max_occupancy >= args.adults + args.children,
            }
            for o in sellable
        ],
        "instruction": (
            "Nothing is free for those dates. Offer to check other dates or to "
            "put the guest through to the team. Do not suggest a room type that "
            "is not listed here." if not sellable else
            "Do not state a price from this result. Prices come from get_quote. "
            "If the guest wants to choose a room, call get_stay_options. "
            "Do not call this again, it has nothing more to add."
        ),
    }


def _meal_options(property_id: str) -> list[dict]:
    """
    What this hotel actually serves.

    One small read, so the answer to "breakfast included hai?" is *in* the
    options a guest is being shown rather than one tool call away. The question
    follows the price closely enough to be worth carrying — and on the run that
    prompted this, the model answered it from the rate plan's free-text
    inclusions instead of asking, which is the failure `get_meal_info` was
    written for arriving through a neighbouring column.
    """
    return (
        get_db().table("meal_options").select("*")
        .eq("property_id", property_id).limit(10).execute().data or []
    )


@tool(
    name="get_stay_options",
    description=(
        "Room types that are free for a stay, each with how many are left, the "
        "rate plans that apply and an indicative total. This already includes "
        "availability — do NOT call search_availability first. For a firm "
        "price, use get_quote."
    ),
    input_model=AvailabilityArgs,
    permission=None,
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def get_stay_options(args: AvailabilityArgs, ctx: AgentContext) -> dict:
    if args.check_out <= args.check_in:
        raise OutOfScope(
            "check_out must be after check_in",
            "Could you tell me the dates again — which night would you arrive?",
        )

    options = await availability_service.stay_options(
        ctx.property_id, args.check_in, args.check_out,
        units=args.units, audience="public",
        # A room this conversation is already holding is still available to
        # it. Without this, quoting takes a hold and the very next
        # availability check reports the rooms it just quoted as gone.
        for_conversation=ctx.conversation_id,
    )
    meal_rows = await run_db(_meal_options, ctx.property_id)

    out = []
    for o in options:
        if o.units_left < args.units:
            continue
        plans = [
            {
                "ratePlanId": p.rate_plan_id,
                "name": p.name,
                "mealPlan": p.meal_plan,
                "refundable": p.refundable,
                # The hotelier's own wording for what this rate covers. Prose
                # typed into a box — not an authority on meals; `mealsIncluded`
                # below is.
                "planNotes": p.inclusions,
                "totalPaise": p.total_paise,
                "perNightPaise": p.per_night_paise,
                # Formatted here, so the model never divides. See
                # `money.display` — a live replay had it divide by a thousand
                # and offer a ₹19,200 stay for ₹1,920.
                "total": money.display(p.total_paise),
                "perNight": money.display(p.per_night_paise),
                # Derived from `meal_plan` against what the hotel actually
                # serves, never read out of the plan's wording. The two can
                # disagree — a plan whose inclusions say "Dinner" at a hotel
                # that serves none — and the guest gets told the wording.
                "mealsIncluded": [
                    m["meal"] for m in policies.meals_for_plan(p.meal_plan, meal_rows)
                    if m["included"]
                ],
                "minStayNights": p.min_stay_violation,
            }
            for p in o.rate_plans
            if not p.closed and p.min_stay_violation is None
        ]
        if plans:
            out.append({
                "roomTypeId": o.room_type_id,
                "name": o.room_type_name,
                "unitsLeft": o.units_left,
                "maxOccupancy": o.max_occupancy,
                "ratePlans": plans,
            })

    return {
        "checkIn": str(args.check_in),
        "checkOut": str(args.check_out),
        "options": out,
        "currency": "INR",
        "instruction": (
            "These totals are indicative and exclude any negotiated discount. "
            "Quote them as 'from', and call get_quote before promising a number. "
            "Use the `total` and `perNight` strings exactly as written — never "
            "convert a paise figure yourself. `mealsIncluded` is what the rate "
            "actually covers; `planNotes` is the hotelier's own wording and is "
            "not an answer about meals. For timings, or the price of a meal "
            "that is not included, call get_meal_info. "
            # **"rate plan" is a word from inside the machine.**
            #
            # Found by `scripts/replay_e2e.py`, whose transcript-quality bar is
            # deliberately higher than the reply guardrail's: the agent said
            # "rate plan" to a guest, and a guest does not have rate plans, they
            # have a room with breakfast or a room without.
            #
            # Fixed here rather than in the guardrail on purpose. A guardrail
            # that refused the phrase would turn a cosmetic problem into a
            # handoff — a refusal *is* a handoff — which is strictly worse than
            # the thing it fixes. And fixed here rather than in the system
            # prompt because this is where the word comes from: the field is
            # called `ratePlans`, the model reads it, and the remedy belongs
            # next to the cause.
            "Say a plan by its `name` — 'Room + Breakfast' — never as a 'rate "
            "plan'; that is our word for it, not the guest's."
        ),
    }
