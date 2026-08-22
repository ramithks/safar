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

        output = getattr(outcome, "output", None)
        if not isinstance(output, dict):
            continue
        # The ids the guest has settled on, so a later turn does not have to
        # re-derive them from a room name — which is how the model came to
        # invent `rate_plan_id = "breakfast_included"`.
        for key, field in (("quoteId", "quote_id"), ("roomTypeId", "room_type_id"),
                           ("ratePlanId", "rate_plan_id")):
            if output.get(key):
                patch[field] = output[key]
        if tool in ("get_quote",) and args.get("room_type_id"):
            patch["room_type_id"] = args["room_type_id"]
            patch["rate_plan_id"] = args.get("rate_plan_id")

    if stage:
        patch["stage"] = stage
    return patch


def merge(conversation_id: str, patch: dict) -> None:
    """
    Fold one turn's findings into the stored session. Best effort, never fatal.

    Read-modify-write rather than a jsonb merge in SQL, because the column is
    shared with the language preference and PostgREST has no partial-object
    update. The race — two turns on one conversation writing at once — cannot
    happen: `pipeline.conversation_lock` is held for the whole drain.
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
        merged = {**profile, **patch}
        if merged == profile:
            return
        db.table("conversations").update({"guest_profile": merged}).eq(
            "id", conversation_id
        ).execute()
    except Exception:  # noqa: BLE001
        logger.warning("could not store session state for conversation %s",
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

    return (
        "Earlier in this conversation the guest gave these dates. Do not ask for "
        "them again unless they change the subject to different nights:\n"
        f"  {check_in} to {check_out}, {rooms} room(s), {party}"
    )
