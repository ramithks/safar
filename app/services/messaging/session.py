"""
Where a conversation has got to, in one small object.

Three different things get called "memory" and only one of them belongs here.

    conversation history   what the guest actually said        conversation_messages
    session state          where the booking flow has got to   THIS
    database truth         availability, price, payment        Postgres, always

**Derived from tool results, never from what the model wrote.** Every field
below is copied out of a `ToolOutcome` — the arguments a tool was called with
and the payload it returned. A model that says "so that's the 10th to the 14th"
does not move this; a `search_availability` call for those dates does. That is
the difference between a cache of facts and a second, softer copy of the
transcript, and the second one is how a system starts believing its own prose.

**It is a convenience, not an authority.** Nothing reads this to decide a price,
a room or a payment: the quote is authoritative for the first two and the
payment order for the third, and `get_booking_status` composes them from the
tables. What this is for is the two questions that are expensive to answer any
other way — *"has this guest already told us their dates?"*, so the agent does
not ask twice, and *"where is this conversation up to?"*, so a receptionist
opening the inbox does not have to read forty messages to find out.

It lives in `conversations.guest_profile`, which existed and was never written
to, alongside the language preference. One jsonb column, merged rather than
replaced, because a language write that dropped a live quote would be a worse
bug than either one fixes.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db.client import get_db

logger = logging.getLogger(__name__)

# Which tool arguments describe the stay being discussed. Read off the *input*
# rather than the output, because these are what the guest told us and the
# output is what the hotel answered.
_STAY_ARGS = ("check_in", "check_out", "adults", "children", "units")

# Tool → the stage it puts the conversation in, most advanced last. A turn that
# calls two of them lands on the furthest one, which is right: a quote after an
# availability check is a quote.
_STAGES = {
    "search_availability": "browsing",
    "get_stay_options": "browsing",
    "get_quote": "quoted",
    "create_booking_payment_link": "awaiting_payment",
    "create_payment_link": "awaiting_payment",
    "create_booking": "booked",
}
_ORDER = ("browsing", "quoted", "awaiting_payment", "booked")


def patch_from(outcomes: list) -> dict:
    """
    What one turn established. Empty when it established nothing.

    Empty is the common case and deliberately cheap: most turns are a question
    and an answer, and writing a row for every one of them would put this
    module on the hot path of a conversation it has nothing to say about.
    """
    patch: dict = {}
    stage: str | None = None

    for outcome in outcomes or []:
        if not getattr(outcome, "ok", False):
            continue
        tool = getattr(outcome, "tool", "") or ""

        reached = _STAGES.get(tool)
        if reached and (stage is None or _ORDER.index(reached) > _ORDER.index(stage)):
            stage = reached

        args = getattr(outcome, "input", None) or {}
        for field in _STAY_ARGS:
            if args.get(field) is not None:
                patch[field] = args[field]

        # The room this turn was about, whichever tool was asked.
        #
        # Recorded from any tool's arguments and not only from `get_quote`,
        # because the model naming a room to `search_availability` or
        # `send_room_photos` is the same fact arriving earlier — and the turns
        # between choosing a room and pricing it are exactly where the choice
        # used to be lost. A later `get_quote` overwrites it, which is right:
        # a priced room outranks a discussed one.
        if args.get("room_type_id"):
            patch["room_type_id"] = args["room_type_id"]

        output = getattr(outcome, "output", None)
        if not isinstance(output, dict):
            continue
        # The ids the guest has settled on, so a later turn does not have to
        # re-derive them from a room name — which is how the model came to
        # invent `rate_plan_id = "breakfast_included"`.
        for key, field in (("quoteId", "quote_id"), ("roomTypeId", "room_type_id"),
                           ("ratePlanId", "rate_plan_id"),
                           # So a later turn can say "Deluxe Balcony" rather
                           # than reciting a uuid or asking again.
                           ("roomName", "room_name")):
            if output.get(key):
                patch[field] = output[key]
        if tool in ("get_quote",) and args.get("room_type_id"):
            patch["room_type_id"] = args["room_type_id"]
            patch["rate_plan_id"] = args.get("rate_plan_id")

        # Where the price argument has got to.
        #
        # Recorded so the agent does not re-open a negotiation it has already
        # settled — a guest who accepted a counter and then said "ok" should not
        # be countered again — and so the inbox can show a manager that a
        # conversation is mid-negotiation without reading it.
        if tool == "negotiate_rate" and output.get("verdict"):
            patch["negotiation"] = output["verdict"]

        # Whether the hotel has been asked something, and is yet to answer.
        #
        # From the tool's own report rather than from a second read of
        # `agent_escalations`: the escalation was created microseconds ago by
        # this very turn, and querying for what we just wrote is a round trip to
        # learn something we already know. `prompts` reads it back from the
        # session, so the agent on the *next* turn knows it is waiting without
        # any query at all.
        if tool == "escalate_to_team" and output.get("escalationId"):
            patch["awaiting_hotel"] = {
                "id": output["escalationId"],
                "kind": output.get("kind"),
                "notified": bool(output.get("teamNotified")),
            }

        # Money that has actually moved, as the backend reported it. Never as
        # the guest described it — see GROUND_RULES on "payment hogyi".
        if tool == "get_booking_status" and output.get("state"):
            patch["payment"] = output["state"]
            # The hotel has answered, or the stay is done. Either way the agent
            # is no longer waiting on anybody.
            if output["state"] in ("CONFIRMED", "CHECKED_IN", "CHECKED_OUT", "CANCELLED"):
                patch["awaiting_hotel"] = None

    if stage:
        patch["stage"] = stage
    return patch


# The fields that describe *one* stay being discussed, as opposed to what we
# know about the guest. When the guest moves on to different nights, these go
# with the old stay; `language` and anything else on the profile stays.
_TRANSACTIONAL = _STAY_ARGS + (
    "stage", "quote_id", "room_type_id", "rate_plan_id", "room_name",
    # These describe *this* stay too. A guest who comes back in October
    # about different nights is not still mid-negotiation on September's,
    # and an `awaiting_hotel` left behind would have the agent telling them
    # it is waiting on a question nobody asked about this trip.
    "negotiation", "payment", "awaiting_hotel", "obligations",
)

# How long a session may sit untouched before it stops being "where we are" and
# becomes "what we were talking about last month". A guest who comes back after
# this has almost certainly come back about something else, and the dates they
# gave in September are not an answer to a question asked in November.
STALE_AFTER_HOURS = 72


def _superseded(profile: dict, patch: dict) -> bool:
    """
    Has the guest started talking about a different stay?

    **The bug this exists for.** `merge` was `{**profile, **patch}` with no
    eraser, so a session only ever accumulated. A guest who booked 10–14
    September for 2 rooms and 4 adults, and came back in October asking about
    20–22 October for 1 room and 2 adults, kept `units: 2` and `children` from
    the first stay for any field the new tool call did not happen to set — and
    kept `quote_id` and `room_type_id` pointing at a booking that was finished.
    `describe()` then put the September dates into the system prompt of an
    October conversation.

    New dates are the signal, because dates are the one thing a guest always
    restates when they mean a different trip. Changing the party size alone is
    a correction to the stay being discussed, not a new one.
    """
    if not (patch.get("check_in") or patch.get("check_out")):
        return False
    for field in ("check_in", "check_out"):
        old, new = profile.get(field), patch.get(field)
        if old and new and str(old) != str(new):
            return True
    return False


def _stale(profile: dict, now: datetime | None = None) -> bool:
    """Old enough that carrying it forward would be guessing."""
    touched = profile.get("touched_at")
    if not touched:
        return False
    try:
        at = datetime.fromisoformat(str(touched).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (now or datetime.now(timezone.utc)) - at > timedelta(hours=STALE_AFTER_HOURS)


def clear_stay(profile: dict) -> dict:
    """
    Drop the current stay, keep what we know about the guest.

    Deliberately a subtraction of a named list rather than "keep language and
    throw away the rest": the profile is a shared column and the next thing
    somebody stores on it should not be silently deleted by a booking ending.
    """
    return {k: v for k, v in (profile or {}).items() if k not in _TRANSACTIONAL}


def merge(conversation_id: str, patch: dict) -> None:
    """
    Fold one turn's findings into the stored session. Best effort, never fatal.

    Read-modify-write rather than a jsonb merge in SQL, because the column is
    shared with the language preference and PostgREST has no partial-object
    update. The race — two turns on one conversation writing at once — cannot
    happen: `pipeline.conversation_lock` is held for the whole drain.

    **Merging is not always the right verb.** See `_superseded` and `_stale`:
    a session that has moved to different nights, or that has been sitting for
    three days, is replaced rather than added to.
    """
    if not patch:
        return
    try:
        db = get_db()
        rows = (
            db.table("conversations").select("guest_profile")
            .eq("id", conversation_id).limit(1).execute().data or []
        )
        profile = (rows[0].get("guest_profile") if rows else None) or {}
        if not isinstance(profile, dict):
            profile = {}

        if _superseded(profile, patch) or _stale(profile):
            profile = clear_stay(profile)

        merged = {**profile, **patch}
        if merged == profile:
            # Nothing new. Returning here rather than stamping `touched_at`
            # anyway is what keeps the staleness clock honest: it should measure
            # when the *stay* last moved, not when a turn last happened to run.
            return
        merged["touched_at"] = datetime.now(timezone.utc).isoformat()
        db.table("conversations").update({"guest_profile": merged}).eq(
            "id", conversation_id
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("could not store session state for conversation %s",
                       conversation_id, exc_info=True)


def close(conversation_id: str) -> None:
    """
    The stay under discussion is finished. Stop describing it as current.

    Called when a booking reaches a terminal state — confirmed and announced,
    or cancelled. Without this, `stage: "booked"` and a spent `quote_id` stay
    on the thread forever, and a guest writing back next month starts inside
    last month's transaction. What the guest is *actually* owed after a
    confirmation is `get_booking_status`, which reads the reservation and does
    not need any of this.

    Never fatal: a session that failed to close is a stale sentence in a
    prompt, and losing the guest's message over it would be worse.
    """
    try:
        db = get_db()
        rows = (
            db.table("conversations").select("guest_profile")
            .eq("id", conversation_id).limit(1).execute().data or []
        )
        profile = (rows[0].get("guest_profile") if rows else None) or {}
        if not isinstance(profile, dict) or not profile:
            return
        remaining = clear_stay(profile)
        if remaining == profile:
            return
        db.table("conversations").update({"guest_profile": remaining}).eq(
            "id", conversation_id
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("could not close the session on conversation %s",
                       conversation_id, exc_info=True)


def describe(conversation: dict | None) -> str | None:
    """
    The session as a sentence for the system prompt, or None when there is
    nothing worth spending tokens on.

    Deliberately not the whole object. `quote_id` is already in the prompt via
    `prompts._live_quote`, which reads the quote row itself and knows whether it
    has expired — a cached id that has quietly gone stale is worse than no id.
    What is here is only what the guest told us and nothing else can recall.
    """
    profile = (conversation or {}).get("guest_profile") or {}
    if not isinstance(profile, dict):
        return None

    check_in, check_out = profile.get("check_in"), profile.get("check_out")
    if not (check_in and check_out):
        return None

    party = f"{profile.get('adults') or 1} adults"
    if profile.get("children"):
        party += f" and {profile['children']} children"
    rooms = profile.get("units") or 1

    lines = [
        "Earlier in this conversation the guest gave these dates. Do not ask for "
        "them again unless they change the subject to different nights:",
        f"  {check_in} to {check_out}, {rooms} room(s), {party}",
    ]

    # The room they have already settled on.
    #
    # It was on the session and never in the prompt, which meant a guest who
    # picked a room and then changed the number of rooms was asked which room
    # they wanted — a question they had answered two turns earlier. The id is
    # here as well as the name because the id is what a tool takes: without it
    # the model either asks again or re-derives one from a room name, which is
    # how it once invented `rate_plan_id = "breakfast_included"`.
    if profile.get("room_type_id"):
        named = profile.get("room_name") or "the room they chose"
        lines.append(
            f"  They have already settled on {named} — do not ask which room "
            f"again. room_type_id: {profile['room_type_id']}"
            + (f", rate_plan_id: {profile['rate_plan_id']}"
               if profile.get("rate_plan_id") else "")
        )

    return "\n".join(lines)
