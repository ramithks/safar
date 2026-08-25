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
    "negotiate_rate": "quoted",
    "create_booking_payment_link": "awaiting_payment",
    "create_payment_link": "awaiting_payment",
    "create_booking": "booked",
    "cancel_booking": "cancelled",
}
_ORDER = ("browsing", "quoted", "awaiting_payment", "booked", "cancelled")

# The tools that move the *business*, as opposed to answering a question about
# it. Recorded as `last_business_action` so a manager opening the inbox — and
# the next turn's prompt — can see where the money got to without reading the
# thread, and so the evaluation report can say which turn a conversation
# actually stalled on. §9.
_BUSINESS_TOOLS = frozenset({
    "get_quote", "negotiate_rate", "get_group_rates", "propose_rate_exception",
    "create_booking_payment_link", "create_payment_link", "create_booking",
    "cancel_booking", "escalate_to_team", "request_human_handoff",
})


# ── where a fact came from, and which source wins ───────────────────────────
#
# **The precedence rule was positional, and positional is fragile.** A guest's
# own words beat a tool argument because `stated_facts` happened to be applied
# last in the function, and a forced call filled gaps only because a set of
# names was threaded through the loop by hand. Both are correct today and both
# would break the first time somebody reordered two blocks.
#
# Named instead, so the rule is a table rather than a shape. Every fact the
# session carries arrives from one of four places, and a lower-ranked source
# never overwrites a higher-ranked one *within the same turn*:
#
#   GUEST_SAID     the guest's own sentence, read by `required_actions` and
#                  `date_facts`. Nothing is better evidence of what the guest
#                  wants than what they just said.
#   TOOL_RESULT    an id or a name a tool returned — a quote's room, a rate
#                  plan. The system's own answer about its own records.
#   TOOL_ARGUMENT  what the *model* called a tool with. A reading of the guest,
#                  one remove away.
#   CARRIED        what a *forced* call was invoked with, which the application
#                  built out of the session — so it is the session repeating
#                  itself, not new evidence, and must never overwrite the turn.
GUEST_SAID = "guest_said"
TOOL_RESULT = "tool_result"
TOOL_ARGUMENT = "tool_argument"
CARRIED = "carried"

SOURCE_RANK = {GUEST_SAID: 3, TOOL_RESULT: 2, TOOL_ARGUMENT: 1, CARRIED: 0}


class _Facts:
    """
    One turn's findings, each with where it came from.

    `set` is the whole mechanism: a fact only lands if nothing better-sourced
    has already claimed it this turn. Equal ranks overwrite, which keeps the
    within-rank behaviour that was there before — a model that searches one week
    and then quotes another has changed its mind about the same turn.
    """

    def __init__(self) -> None:
        self.values: dict = {}
        self.sources: dict[str, str] = {}
        # How sure we are of each, in the vocabulary the layer that read it
        # uses: `exact`/`fuzzy` for a room name, `explicit`/`inferred`/`derived`
        # for a date, `stated` for everything a tool simply carried. The brief
        # asks for source, confidence, turn, value and dependencies per fact;
        # this is the third of the five.
        self.confidence: dict[str, str] = {}

    def set(self, field: str, value, source: str, confidence: str = "stated") -> None:
        if value is None:
            return
        held = self.sources.get(field)
        if held is not None and SOURCE_RANK[source] < SOURCE_RANK[held]:
            return
        self.values[field] = value
        self.sources[field] = source
        self.confidence[field] = confidence

    def update(self, values: dict, source: str, confidence: str = "stated") -> None:
        for field, value in (values or {}).items():
            self.set(field, value, source, confidence)


def patch_from(outcomes: list, message: str | None = None,
               rooms: list[dict] | None = None, today=None,
               known: dict | None = None) -> dict:
    """
    What one turn established. Empty when it established nothing.

    `message` is the guest's own sentence, and what it states outranks
    everything a tool was called with — see the end of this function. Optional
    because several callers legitimately have only outcomes; passing it is what
    makes a correction land on a turn where no tool carried it.

    Empty is the common case and deliberately cheap: most turns are a question
    and an answer, and writing a row for every one of them would put this
    module on the hot path of a conversation it has nothing to say about.
    """
    patch: dict = {}
    stage: str | None = None

    # **A forced call may fill a gap; it may never overwrite.**
    #
    # The bug: a guest three turns into a booking wrote *"actually 4 tareekh se
    # 6 tareekh kar do"*. The model called `get_stay_options` with the new
    # dates, correctly. Then the required-action layer forced `negotiate_rate`,
    # whose arguments it had built from the *stored* session — the old dates —
    # and because this loop takes the last value it sees, the patch carried the
    # September dates the guest had just moved away from. The session went
    # backwards, and every later prompt told the model the guest was staying on
    # nights they had already changed.
    #
    # Ordering is the wrong rule here, and so is ignoring forced calls
    # altogether: `get_group_rates` is forced with `units: 5` read straight out
    # of "5 kamre chahiye", which is the freshest fact on the turn and must
    # land. What separates the two is not the tool but where the argument came
    # from — the guest's sentence, or the session the guest is contradicting.
    #
    # So a forced call's arguments are `CARRIED` — the session repeating itself,
    # not new evidence — and the rank table refuses to let them overwrite
    # anything the turn actually established. `get_group_rates` forced with
    # `units: 5` read out of "5 kamre chahiye" still lands, because nothing else
    # claimed `units` on that turn.
    facts = _Facts()

    for outcome in (o for o in (outcomes or []) if getattr(o, "ok", False)):
        forced = getattr(outcome, "decision_source", "model") == "application"
        argument_source = CARRIED if forced else TOOL_ARGUMENT
        tool = getattr(outcome, "tool", "") or ""

        if tool in _BUSINESS_TOOLS:
            patch["last_business_action"] = tool

        reached = _STAGES.get(tool)
        if reached and (stage is None or _ORDER.index(reached) > _ORDER.index(stage)):
            stage = reached

        args = getattr(outcome, "input", None) or {}
        for field in _STAY_ARGS:
            facts.set(field, args.get(field), argument_source)

        # The room this turn was about, whichever tool was asked.
        #
        # Recorded from any tool's arguments and not only from `get_quote`,
        # because the model naming a room to `search_availability` or
        # `send_room_photos` is the same fact arriving earlier — and the turns
        # between choosing a room and pricing it are exactly where the choice
        # used to be lost. A later `get_quote` overwrites it, which is right:
        # a priced room outranks a discussed one.
        facts.set("room_type_id", args.get("room_type_id"), argument_source)

        output = getattr(outcome, "output", None)
        if not isinstance(output, dict):
            continue

        # Every room the hotel just said was free, so a room that has *stopped*
        # being free can be taken off the session rather than quietly kept.
        #
        # §9: "room becomes unavailable → explicitly invalidate it and ask
        # again". The failure without this is worse than asking twice — the
        # prompt tells the model the guest has settled on the Deluxe and not to
        # ask again, and the model then offers a room that sold an hour ago.
        # Carried under a private key and consumed by `merge`, which is the only
        # place that can see the room the session already holds.
        if tool in ("search_availability", "get_stay_options"):
            listed = output.get("roomTypes") or output.get("options") or []
            if isinstance(listed, list):
                patch["_offered_room_type_ids"] = sorted({
                    str(row.get("roomTypeId")) for row in listed
                    if isinstance(row, dict) and row.get("roomTypeId")
                })
        # The ids the guest has settled on, so a later turn does not have to
        # re-derive them from a room name — which is how the model came to
        # invent `rate_plan_id = "breakfast_included"`.
        for key, field in (("quoteId", "quote_id"), ("roomTypeId", "room_type_id"),
                           ("ratePlanId", "rate_plan_id"),
                           # So a later turn can say "Deluxe Balcony" rather
                           # than reciting a uuid or asking again.
                           ("roomName", "room_name")):
            # A tool's *result* is the system answering about its own records,
            # which outranks a reading of the guest but not the guest.
            facts.set(field, output.get(key),
                      CARRIED if forced else TOOL_RESULT)
        if tool in ("get_quote",) and args.get("room_type_id"):
            # **At argument rank, not result rank.** These are the *arguments*
            # `get_quote` was called with, kept as a fallback for a payload that
            # does not name the room — so they must lose to a payload that
            # does. Ranking them as a result made a quote's own `roomTypeId`
            # lose to the id the model had asked with, which is the system
            # deferring to a reading of the guest over its own record.
            facts.set("room_type_id", args.get("room_type_id"), argument_source)
            facts.set("rate_plan_id", args.get("rate_plan_id"), argument_source)

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

    # **The guest's own words win, over any tool argument on this turn.**
    #
    # Session facts have always been written from tool *arguments* — whatever
    # the model happened to pass — so "Actually 4 log hain" updated the party
    # only if some tool ran carrying it. On a turn where the model answered in
    # prose, or where a forced call was built from the stale session, the
    # correction simply did not land, and the next prompt went on describing the
    # stay the guest had just changed.
    #
    # Last, so it overrides both the model's arguments and a forced call's. A
    # guest who says three rooms has said three rooms, and nothing a tool was
    # invoked with is better evidence of that than the sentence itself.
    if message:
        from app.services.ai import required_actions

        facts.update(required_actions.stated_facts(message), GUEST_SAID)

        # **And the room they named, resolved against this hotel's own list.**
        #
        # The gap a comment in the conversation corpus recorded as an honest
        # boundary: *"nothing in this system resolves a room by name"*, so
        # "Actually Garden Standard chahiye" on a turn where the model answered
        # in prose changed nothing, and the next quote priced the room the guest
        # had just moved away from.
        #
        # `rooms` is passed in rather than read here, so this module keeps no
        # database of its own and a caller with no room list simply gets the
        # behaviour it had before. See services/pms/room_names.
        if rooms:
            from app.services.pms import room_names

            named = room_names.resolve(message, rooms)
            if named.resolved:
                facts.set("room_type_id", named.room_type_id, GUEST_SAID,
                          named.confidence)
                facts.set("room_name", named.name, GUEST_SAID, named.confidence)
                # A room named in prose is a *preference*, not an availability
                # check — the same rule a date change follows. The prompt turns
                # this into an instruction to confirm the room is free before
                # promising it.
                patch["room_needs_recheck"] = True
            elif named.ask:
                # Several rooms plausibly match and guessing between them is the
                # one thing this must not do. Carried to the prompt as a
                # question for the model to ask, not as a state change.
                patch["room_ambiguous"] = named.ask

        # **And the nights, when the sentence settles them beyond doubt.**
        #
        # `today` is the *hotel's* business date and has no default here either:
        # every relative date — "Friday", "the 25th" — is computed from it, and
        # a caller that forgot the timezone would resolve against the server's
        # day. A caller with no clock gets no date reading, which is the
        # behaviour before this existed.
        if today is not None:
            from app.services.pms import date_facts

            # The arrival already agreed, which is the only thing "make it 3
            # nights instead" can be measured from. It comes off the *session*,
            # not the patch: a turn where the guest says only a length has no
            # tool call carrying a date, so the patch is empty and the anchor
            # would be too.
            anchor = facts.values.get("check_in") or (known or {}).get("check_in")
            if isinstance(anchor, str):
                try:
                    from datetime import date as _date

                    anchor = _date.fromisoformat(anchor)
                except ValueError:
                    anchor = None
            nights = date_facts.read(message, today, check_in=anchor)
            if nights.usable:
                facts.set("check_in", nights.check_in.isoformat(), GUEST_SAID,
                          nights.confidence)
                facts.set("check_out", nights.check_out.isoformat(), GUEST_SAID,
                          nights.confidence)
                patch["dates_from"] = nights.basis
                # Private, consumed by `merge`: it is what tells an earlier
                # "which month did you mean" that it has been answered.
                patch["_dates_settled"] = True
                if nights.needs_confirming:
                    # Read from a day of the month or a weekday with no month in
                    # the sentence. Almost certainly right, and "almost" is why
                    # the model is told to say them back rather than book on
                    # them silently.
                    patch["dates_need_confirming"] = True
            elif nights.ask:
                patch["dates_unclear"] = nights.ask
    # The ranked facts, folded in with where each came from.
    #
    # `_sources` is private and consumed by `merge`, which keeps only what
    # the session ends up holding — a per-turn map of every field the turn
    # *tried* to set would grow without bound on a long thread.
    patch.update(facts.values)
    if facts.sources:
        patch["_sources"] = {
            field: {"source": source, "confidence": facts.confidence.get(field)}
            for field, source in facts.sources.items()
        }
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
    "last_business_action", "room_needs_recheck", "room_ambiguous",
    "dates_need_confirming", "dates_unclear", "dates_from",
    "_sources", "_sources_turn",
)

# What a *price* was true of. When the guest moves the dates, these are wrong
# and the room they picked is not — which is the distinction §9 asks for and
# which `clear_stay` on its own could not make.
_PRICED = ("quote_id", "rate_plan_id", "negotiation", "payment", "stage")

# How long a session may sit untouched before it stops being "where we are" and
# becomes "what we were talking about last month". A guest who comes back after
# this has almost certainly come back about something else, and the dates they
# gave in September are not an answer to a question asked in November.
STALE_AFTER_HOURS = 72


# ── what depends on what ─────────────────────────────────────────────────────
#
# **The session is derived state, and derived state goes stale when its inputs
# change.** Until now exactly one case of that was handled: `_superseded` below
# noticed new *dates* and `move_dates` dropped the priced fields. Every other
# correction a guest can make went unhandled, and the failures are the same
# shape each time —
#
#     "actually 3 rooms, not 2"     the quote for two rooms stays on the session
#     "make it 4 adults"            the same quote, priced for two, stays
#     "the Garden Standard then"    the rate plan from the Deluxe stays, and the
#                                   next quote is asked for a plan that belongs
#                                   to a different room
#
# — a `quote_id` or a `rate_plan_id` that is true of a stay the guest has just
# stopped describing. The prompt then tells the model about a price that no
# longer applies, and the model quotes it.
#
# So the relationship is declared once instead of being rediscovered per field.
# `DERIVED_FROM[x]` is the set of facts `x` is only true of: change any of them
# and `x` is no longer known.
#
# Read it as sentences:
#
#   a quote is only true of the exact stay it priced — those nights, that many
#   rooms, that many people, that room type, that rate plan;
#   a rate plan belongs to a room type, so choosing a different room invalidates
#   it;
#   a negotiation and a payment state are about a specific quote;
#   the stage a conversation has reached is about the quote it reached it with.
#
# **`room_type_id` is deliberately absent as a dependent.** A guest who moves
# their dates has not changed their mind about the room, and dropping it would
# make the agent ask a question they answered two turns ago — a real regression
# this codebase already fixed once. It is carried forward *unverified* instead,
# with `room_needs_recheck`, which `describe()` turns into an instruction to
# confirm the room is free for the new nights before promising it.
DERIVED_FROM: dict[str, frozenset[str]] = {
    "quote_id": frozenset({"check_in", "check_out", "units", "adults", "children",
                           "room_type_id", "rate_plan_id"}),
    "rate_plan_id": frozenset({"room_type_id"}),
    "negotiation": frozenset({"quote_id"}),
    "payment": frozenset({"quote_id"}),
    "stage": frozenset({"quote_id"}),
    # What the agent still owes the guest is about the stay being discussed. A
    # guest who changes the stay has not necessarily had their question answered
    # — but the *answer* they were owed was about the old one.
    "obligations": frozenset({"check_in", "check_out", "units", "adults"}),
    "awaiting_hotel": frozenset({"check_in", "check_out", "units", "adults"}),
}

# Facts a guest can correct. Everything else on the profile is ours — a stage we
# derived, a room we recorded — and a guest does not "correct" those.
CORRECTABLE = frozenset({"check_in", "check_out", "units", "adults", "children",
                         "room_type_id", "rate_plan_id"})


def corrections(profile: dict, patch: dict) -> set[str]:
    """
    Facts this turn *changed*, as opposed to facts it filled in.

    A blank becoming a value is the conversation progressing — the guest naming
    their dates for the first time invalidates nothing. A value becoming a
    *different* value is a correction, and everything computed from it is now
    describing a stay nobody is asking about.
    """
    changed = set()
    for key in CORRECTABLE:
        if key not in patch:
            continue
        before, after = profile.get(key), patch.get(key)
        if before in (None, "") or after in (None, ""):
            continue
        if str(before) != str(after):
            changed.add(key)
    return changed


def invalidated_by(changed: set[str]) -> set[str]:
    """
    Everything that stops being known when `changed` changes, transitively.

    Transitive because the graph has depth: correcting the room invalidates the
    rate plan, and the rate plan invalidates the quote, and the quote
    invalidates the negotiation and the payment state. Doing one hop would leave
    a quote on the session that was priced against a rate plan we had just
    dropped.
    """
    dead: set[str] = set()
    frontier = set(changed)
    while frontier:
        nxt = set()
        for field, sources in DERIVED_FROM.items():
            if field in dead or field in frontier:
                continue
            if sources & frontier:
                nxt.add(field)
        dead |= nxt
        frontier = nxt
    return dead


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


def move_dates(profile: dict, patch: dict | None = None) -> dict:
    """
    The guest is talking about different nights. Keep the room, drop the price.

    **The bug this exists for.** `_superseded` cleared the whole stay, room
    included, so a guest who chose the Deluxe Balcony and then said "actually
    12th to 14th" was asked which room they wanted — a question they had
    answered two turns earlier, and the same question the prompt fix earlier in
    this audit had just stopped the agent asking. One correct rule (a price is
    only true of the dates it was quoted for) was deleting a fact it had no
    business touching.

    The room is carried forward *unverified*: `room_needs_recheck` is what
    `describe` reads to tell the model it must confirm the room is free for the
    new nights before promising it. A preference is not availability, and the
    difference between them is the whole reason this is two keys and not one.
    """
    moved = {k: v for k, v in (profile or {}).items() if k not in _PRICED}

    # **The party goes only when the guest is re-describing the stay.**
    #
    # This dropped `units`, `adults` and `children` on *any* date change, and
    # the reasoning was sound for the case it was written for: a guest who booked
    # September for four adults and comes back about October has not implicitly
    # kept the four.
    #
    # It is wrong for the case this pass added. A guest three turns into a
    # conversation who says "actually 3 rooms" and then "and the 2nd to the 5th"
    # has not changed their mind about the rooms — and dropping the count meant
    # the next quote priced one room, then the agent asked how many they wanted,
    # which is the question they had just answered. Found by an eleven-turn
    # journey doing exactly that.
    #
    # The two cases are told apart by the patch rather than by a clock: a guest
    # describing a *different* stay states its size, and a guest correcting
    # their nights states only nights. When they do restate it, the old party is
    # dropped whole — otherwise a September "1 child" survives into an October
    # request that mentioned only adults.
    restating = bool(patch) and any(
        patch.get(field) is not None for field in ("units", "adults", "children"))
    if restating:
        for field in _STAY_ARGS:
            moved.pop(field, None)
    else:
        moved.pop("check_in", None)
        moved.pop("check_out", None)

    moved.pop("awaiting_hotel", None)
    moved.pop("obligations", None)
    if moved.get("room_type_id"):
        moved["room_needs_recheck"] = True
    return moved


def clear_stay(profile: dict) -> dict:
    """
    Drop the current stay, keep what we know about the guest.

    Deliberately a subtraction of a named list rather than "keep language and
    throw away the rest": the profile is a shared column and the next thing
    somebody stores on it should not be silently deleted by a booking ending.
    """
    return {k: v for k, v in (profile or {}).items() if k not in _TRANSACTIONAL}


def _record_sources(merged: dict, profile: dict, patch: dict) -> dict:
    """
    Where each fact the session now holds came from, and on which turn.

    **Kept for the facts that survived, not for the ones a turn tried to set.**
    A per-turn map of every attempt would grow without bound on a long thread;
    what is useful six weeks later is "the guest told us three rooms, on turn
    four" — one entry per canonical fact, replaced when the fact is.

    Read by nothing in the hot path today, which is stated plainly rather than
    dressed up: this is a debugging record and an inspectable form of the
    precedence rule, and the rule itself is enforced by `_Facts` and tested
    directly.
    """
    incoming = patch.pop("_sources", None) or {}
    if not incoming:
        return merged

    turn = int((profile.get("_sources_turn") or 0)) + 1
    history = dict(profile.get("_sources") or {})
    for field, entry in incoming.items():
        if field in merged:
            history[field] = {**entry, "turn": turn}
    # Facts that have since gone — dropped by a correction, or by a date move —
    # take their provenance with them.
    history = {k: v for k, v in history.items() if k in merged}
    merged = dict(merged)
    merged["_sources"] = history
    merged["_sources_turn"] = turn
    return merged


def _clear_answered_ambiguity(merged: dict) -> dict:
    """
    A question that has been answered stops being asked.

    Without this the prompt would go on asking a guest to choose between two
    rooms one turn after they chose — which reads, correctly, as not listening.
    The same for dates: once a sentence settles them beyond doubt, the earlier
    "which month did you mean" is answered.
    """
    merged = dict(merged)
    if merged.get("room_ambiguous") and merged.get("room_type_id"):
        merged.pop("room_ambiguous", None)
    if merged.get("dates_unclear") and merged.get("_dates_settled"):
        # **Only when *this* turn settled them.** Keyed on the patch rather than
        # on "the session has dates", because a guest asking to change dates
        # already has dates — the old ones — and clearing the question because
        # they exist would drop it on the very turn it was raised.
        merged.pop("dates_unclear", None)
    merged.pop("_dates_settled", None)
    if merged.get("dates_need_confirming") and \
            merged.get("dates_from", "").startswith(("two dates", "a date")):
        # Spelled out this time. Nothing left to confirm.
        merged.pop("dates_need_confirming", None)
    return merged


def _revalidate_room(merged: dict) -> dict:
    """
    Is the room this session holds still one the hotel is offering?

    Runs only on turns that actually asked — `_offered_room_type_ids` is set by
    `patch_from` from an availability read and is absent otherwise, so a turn
    about breakfast cannot invalidate anything.

    An empty list is not "nothing is free": `search_availability` returns no
    room types when the stay is sold out, and that is precisely the case where
    the selected room must go. A turn that never asked has no key at all, which
    is the distinction between the two.
    """
    offered = merged.pop("_offered_room_type_ids", None)
    chosen = merged.get("room_type_id")

    if offered is None or not chosen:
        # A room chosen after the flag was raised answers it. Otherwise the
        # session would keep telling the prompt the room has gone while holding
        # the id of the one that replaced it.
        if chosen:
            merged.pop("room_unavailable", None)
        return merged

    if str(chosen) in set(offered):
        merged.pop("room_unavailable", None)
        # Confirmed free for these nights, so the doubt raised by a date change
        # is settled and the prompt can stop hedging.
        merged.pop("room_needs_recheck", None)
        return merged

    logger.info("room %s is no longer on offer; clearing it from the session", chosen)
    for field in ("room_type_id", "room_name", "rate_plan_id", "quote_id",
                  "room_needs_recheck"):
        merged.pop(field, None)
    # Named rather than merely absent, so the prompt can say "the room they
    # chose has gone" instead of behaving as though they never chose one.
    merged["room_unavailable"] = True
    return merged


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

        if _stale(profile):
            # Three days untouched. Everything here is what we were talking
            # about last month, the room included.
            profile = clear_stay(profile)
        elif _superseded(profile, patch):
            # New *dates* are the one correction that also means "a different
            # trip", so the whole stay is rebuilt rather than pruned — see
            # `_superseded`.
            profile = move_dates(profile, patch)
        else:
            # **Every other correction, through the declared graph.**
            #
            # A guest who says "actually 3 rooms" or "the Garden Standard then"
            # has invalidated the quote priced for two rooms, or the rate plan
            # that belongs to the Deluxe. Before this only dates were handled,
            # and every other correction left a `quote_id` on the session that
            # was true of a stay the guest had stopped describing — which the
            # prompt then told the model about, and the model quoted.
            #
            # `corrections` is the trigger and it is deliberately narrow: a
            # blank becoming a value is the conversation progressing, not a
            # correction. See DERIVED_FROM.
            changed = corrections(profile, patch)
            if changed:
                dead = invalidated_by(changed)
                if dead:
                    logger.info(
                        "conversation %s corrected %s; dropping %s",
                        conversation_id, sorted(changed), sorted(dead),
                    )
                    profile = {k: v for k, v in profile.items() if k not in dead}
                    if profile.get("room_type_id") and "room_type_id" not in changed:
                        # Carried forward unverified, exactly as a date change
                        # does: a preference is not availability.
                        profile["room_needs_recheck"] = True

        merged = _clear_answered_ambiguity(_revalidate_room({**profile, **patch}))
        merged = _record_sources(merged, profile, patch)
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

    # **Dates are no longer the price of admission.**
    #
    # This returned None unless both dates were known, which meant everything
    # below it — the room the guest chose, the room they half-named, the room
    # that has gone — was invisible on any turn before dates were settled. A
    # guest whose *first* message is "Deluxe Balcony chahiye" had it recorded on
    # the session and never mentioned in the prompt, so the agent asked which
    # room they wanted one turn after they said.
    #
    # The stay line is still only written when there is a stay to describe. What
    # changed is that its absence no longer suppresses the rest.
    check_in, check_out = profile.get("check_in"), profile.get("check_out")
    lines: list[str] = []
    if check_in and check_out:
        party = f"{profile.get('adults') or 1} adults"
        if profile.get("children"):
            party += f" and {profile['children']} children"
        rooms = profile.get("units") or 1
        lines += [
            "Earlier in this conversation the guest gave these dates. Do not ask "
            "for them again unless they change the subject to different nights:",
            f"  {check_in} to {check_out}, {rooms} room(s), {party}",
        ]
    elif (profile.get("room_type_id") or profile.get("room_ambiguous")
            or profile.get("dates_unclear")):
        lines.append("Earlier in this conversation:")

    if profile.get("dates_need_confirming") and check_in and check_out:
        # Read from a day of the month or a weekday, with no month in the
        # sentence — see `date_facts`. Right almost always, and the model says
        # them back rather than booking on them silently.
        lines.append(
            "  Those nights were worked out from what they said rather than "
            "spelled out — say the two dates back to them in your reply so they "
            "can correct you."
        )
    if profile.get("dates_unclear"):
        lines.append(
            f"  Their dates are not clear. Ask them: "
            f"\"{profile['dates_unclear']}\" — do not guess at a month."
        )

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
        # Chosen for different nights, and not yet checked against these. The
        # room survives a date change (see `move_dates`) and its *availability*
        # does not, and the difference has to reach the model or it will promise
        # a room nobody has confirmed.
        if profile.get("room_needs_recheck"):
            lines.append(
                "  They chose that room for different dates. Check it is free "
                "for these nights with search_availability or get_stay_options "
                "before promising it, and re-price it — the old price does not "
                "apply to new dates."
            )
    elif profile.get("room_ambiguous"):
        # Several of this hotel's rooms plausibly match what the guest wrote,
        # and the resolver refused to pick between them — see
        # `services/pms/room_names`. The question is handed to the model
        # already written, because it names this hotel's own rooms and the
        # model would otherwise have to reconstruct the list.
        lines.append(
            f"  They named a room but it is not clear which. Ask them: "
            f"\"{profile['room_ambiguous']}\" — do not choose for them."
        )
    elif profile.get("room_unavailable"):
        # The room they had chosen has gone. Said plainly, because the
        # alternative is an agent that behaves as though they never chose one
        # and offers the same room back a turn later.
        lines.append(
            "  The room they had chosen is no longer free for these dates. Say "
            "so, and offer what search_availability actually returns."
        )

    # Nothing worth spending tokens on. The stay line alone is not enough to
    # keep — a header with no facts under it is noise in every prompt.
    if not lines or (len(lines) == 1 and lines[0].endswith("conversation:")):
        return None
    return "\n".join(lines)
