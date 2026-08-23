"""
The guest nobody came back to.

**The failure this exists for.** Three separate code paths hand a conversation
to a person, and not one of them tells a person:

    request_human_handoff      the model decided it had reached its limit
    runtime._ensure_human      the turn failed, or a guardrail refused the reply
    messaging_tasks            the conversation was locked for two minutes

All three flip `conversations.handler` to `human_requested`, and two of the
three first tell the guest *"someone from the hotel will be with you shortly"*.
`services/pms/escalation` was written for exactly this — its own docstring says
so — and nothing was ever wired to it. So the sentence was true about the
column and false about the world: at 11pm on a Saturday the guest was handed to
a person who did not know they existed.

**Why a sweep and not a notification at the moment of handoff.** "A guest is
waiting" is a statement about *time*, not about an event. A model call that
fails once and succeeds on the guest's next message thirty seconds later
produced no waiting guest and should produce no WhatsApp to a manager's phone;
a handoff at 11pm that nobody touches for twenty minutes did, and no event at
the moment of the handoff can tell those apart. Only the clock can, so the
clock is what asks.

It also catches a case no handoff path covers at all: the guest whose message
was never answered by anyone, because the webhook arrived and the task was lost.
There is no code path to hang a notification on there — the defining feature of
that failure is that no code ran.

**Two ways to be waiting, and the second is the non-obvious one.**

    nobody replied      nothing has been sent since the guest's message
    parked              the AI said a person was coming, and none has

The second looks answered by every cheap test — there *is* an outbound message
newer than the guest's — which is why it needs `handler` to be read as well as
the timestamps. It is also the more urgent of the two: the guest has been given
a promise.

**Nothing here decides anything twice.** `raise_escalation` dedupes on
`(conversation, kind, dedupe_key)` for as long as the escalation is open or
notified, so a sweep every five minutes over the same waiting guest raises one
ticket and then keeps finding it. When somebody from the hotel replies in the
thread, `escalation.note_hotel_replied` closes it, and a *later* silence on the
same conversation raises a new one — which is right: it is a new wait.
"""
import logging
from datetime import datetime, timedelta, timezone

from app.db.client import get_db, run_db
from app.services.marketing import consent
from app.services.messaging import obligations
from app.services.pms import escalation as escalation_service

logger = logging.getLogger(__name__)

# How long a guest may go unanswered before the hotel is told.
#
# Long enough that an ordinary turn — classify, three or four tool calls, a
# model that occasionally takes ninety seconds — is never mistaken for silence,
# and that a receptionist who is already typing is not interrupted by a message
# about the person they are answering. Short enough to still be the same
# conversation when somebody picks it up: past about twenty minutes an
# Instagram guest has usually left.
WAITING_AFTER_MINUTES = 10

# How far back a sweep looks.
#
# A guest who wrote yesterday and was never answered is not waiting; they are
# gone, and a WhatsApp about them tomorrow morning teaches the hotel that these
# messages are history rather than something to act on. It also bounds the
# first run after a deploy, which would otherwise raise a ticket for every
# unanswered message the inbox has ever held.
LOOK_BACK_HOURS = 12

# Conversations examined per sweep. Well past any real minute of traffic; here
# so a database problem cannot turn one scheduled task into an unbounded read.
MAX_PER_SWEEP = 500


def _candidates(since: str, until: str) -> list[dict]:
    """
    Conversations whose last inbound message is old enough to be a wait.

    The window is closed at both ends deliberately: `until` is what makes it a
    wait rather than a message in flight, and `since` is what stops the sweep
    reading the archive.
    """
    return (
        get_db().table("conversations")
        .select("id,property_id,partner_id,guest_id,channel,handler,status,"
                "last_inbound_at,last_message_at,last_message,guest_profile,ai_enabled")
        .eq("status", "open")
        .gte("last_inbound_at", since)
        .lte("last_inbound_at", until)
        .order("last_inbound_at")
        .limit(MAX_PER_SWEEP)
        .execute().data or []
    )


def _parse(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def reason_waiting(conversation: dict) -> str | None:
    """
    Why this guest is waiting, or None if they are not.

    Split out from the sweep so it can be tested on a row rather than on a
    database, and so the two states have names that appear in the notification
    rather than being implied by which branch was taken.
    """
    inbound = _parse(conversation.get("last_inbound_at"))
    if inbound is None:
        return None

    # A guest who asked us to stop is not a guest waiting for an answer, and
    # raising a ticket that sends somebody to reply to them is the one outcome
    # they explicitly asked not to have. `is_stop_request` is the same
    # predicate the marketing consent path uses, bounds and all.
    if consent.is_stop_request(conversation.get("last_message")):
        return None

    handler = conversation.get("handler") or "ai"
    last = _parse(conversation.get("last_message_at"))

    # Nothing has been sent since they wrote. True whoever was meant to answer:
    # the AI never ran, the reply failed to send, or a member had taken the
    # thread over and has not typed.
    if last is None or last <= inbound:
        return "nobody has replied"

    # Something was sent — but if that something was the AI saying a person is
    # coming, the promise is the thing outstanding, not the message.
    if handler in ("human_requested",):
        return "the AI handed this to a person and nobody has taken it"

    return None


def _listed(labels: list[str]) -> str | None:
    """The open topics as one readable phrase. These labels are written to be
    read in a sentence, so they are joined as one."""
    if not labels:
        return None
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " and " + labels[-1]


def _facts(conversation: dict, waiting_minutes: int, why: str) -> dict:
    """
    What the person picking this up needs, and nothing they do not.

    Read from the session the conversation already carries rather than from the
    transcript: the dates and party are structured state written by the tools
    that used them, so this costs no query and cannot paraphrase.
    """
    profile = conversation.get("guest_profile")
    profile = profile if isinstance(profile, dict) else {}
    asked = obligations.topics_open(profile)
    return {
        "channel": conversation.get("channel"),
        "check_in": profile.get("check_in"),
        "check_out": profile.get("check_out"),
        "units": profile.get("units"),
        "waiting_minutes": waiting_minutes,
        "waiting_reason": why,
        "asked_about": _listed(asked),
    }


def _context(property_ids: set[str], guest_ids: set[str]) -> tuple[dict, dict]:
    db = get_db()
    hotels = {
        str(row["id"]): row for row in (
            db.table("properties").select("id,name,partner_id,is_demo")
            .in_("id", list(property_ids)).execute().data or []
        )
    } if property_ids else {}
    guests = {
        str(row["id"]): row for row in (
            db.table("guests").select("id,name")
            .in_("id", list(guest_ids)).execute().data or []
        )
    } if guest_ids else {}
    return hotels, guests


async def sweep(now: datetime | None = None) -> dict:
    """
    Notify the hotel about every guest who is waiting, once each.

    Returns counts rather than rows: this runs on a schedule with nobody
    reading its result, so what it returns is for the log line and the test.
    """
    now = now or datetime.now(timezone.utc)
    until = (now - timedelta(minutes=WAITING_AFTER_MINUTES)).isoformat()
    since = (now - timedelta(hours=LOOK_BACK_HOURS)).isoformat()

    rows = await run_db(_candidates, since, until)
    waiting = [(row, why) for row in rows if (why := reason_waiting(row))]
    if not waiting:
        return {"examined": len(rows), "waiting": 0, "raised": 0,
                "notified": 0, "demo": 0}

    hotels, guests = await run_db(
        _context,
        {str(r["property_id"]) for r, _ in waiting if r.get("property_id")},
        {str(r["guest_id"]) for r, _ in waiting if r.get("guest_id")},
    )

    raised = notified = skipped = 0
    for row, why in waiting:
        hotel = hotels.get(str(row.get("property_id"))) or {}

        # A demo hotel's conversations are the e2e suites', and this is the same
        # gate that keeps them out of the sitemap and out of IndexNow. Nine of
        # them were sitting in `human_requested` the day this was written, every
        # one left by a test run — and Casa Mandala has a real WhatsApp number
        # on its team. A hotel whose first nine notifications are about fake
        # guests has learned to ignore the tenth.
        if hotel.get("is_demo"):
            skipped += 1
            continue

        hotel_name = hotel.get("name") or "Your hotel"
        minutes = int(
            (now - (_parse(row["last_inbound_at"]) or now)).total_seconds() // 60
        )
        facts = _facts(row, minutes, why)
        guest = guests.get(str(row.get("guest_id"))) or {}
        if guest.get("name"):
            facts["guest_name"] = guest["name"]

        try:
            result = await escalation_service.raise_escalation(
                property_id=str(row["property_id"]),
                partner_id=str(row.get("partner_id") or hotel.get("partner_id") or ""),
                kind="unanswered",
                facts=facts,
                # The guest's own last message, so the person picking this up
                # can answer it from the notification without opening anything.
                reason=(row.get("last_message") or "")[:400] or None,
                hotel_name=hotel_name,
                conversation_id=str(row["id"]),
            )
        except Exception:  # noqa: BLE001 — one hotel's failure is not the sweep's
            logger.exception("could not raise a waiting escalation for %s", row["id"])
            continue

        if result.get("duplicate"):
            continue
        raised += 1
        notified += 1 if result.get("notified") else 0

    logger.info(
        "waiting sweep: %s examined, %s waiting, %s raised, %s notified, %s demo",
        len(rows), len(waiting), raised, notified, skipped,
    )
    return {
        "examined": len(rows), "waiting": len(waiting),
        "raised": raised, "notified": notified, "demo": skipped,
    }
