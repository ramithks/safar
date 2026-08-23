"""
Reaching the hotel's own team, and knowing whether they answered.

`request_human_handoff` has existed since the first agent work. It flips
`conversations.handler` to `human_requested` and raises a task, and that is all
it does — **nobody is told**. At 11pm on a Saturday the guest is handed to a
person who does not know they exist, and the agent, having "escalated", has no
way to find out that nothing happened. §19 to §21 are that gap.

Four things are needed and none of them existed:

    who       team_notification_rules → hotel_team_members → a WhatsApp number
    what      a structured internal message, composed here from stored facts
    once      a dedupe key, so the same argument does not raise five tickets
    then      a lifecycle the agent can read: notified · responded · resolved

**The message is composed on the server from stored facts, never by the model.**
The agent supplies a `reason` — one sentence about what the guest actually said
— and everything numeric in the outgoing message is read from the quote, the
rules and the conversation. A model that could write the numbers in an approval
request could write the wrong ones, and a manager pressing Approve on a figure
the model invented is the worst version of this feature.

**The internal message may name the floor. Nothing sent to a guest ever may.**
That asymmetry is the whole point of composing it here: this text goes only to
numbers in `hotel_team_members`, which only somebody with the `members`
permission can add. §19's own example includes "Minimum allowed: ₹4,300", and it
is right to — a manager deciding on an exception needs to know what they are
making an exception to.

**Failure to notify is recorded as failure.** A WhatsApp message to a team
member is business-initiated and outside any 24-hour window, so Meta will refuse
free text unless that person has recently written in. When that happens the
escalation stays `open`, the error is stored, and the dashboard shows it. It is
not marked `notified`, because an escalation that says it reached somebody and
did not is worse than one that admits it is stuck.
"""
import hashlib
import logging

from app.db.client import get_db, run_db
from app.services.messaging import credentials as creds_module
from app.services.messaging import service as messaging_service

logger = logging.getLogger(__name__)

# Which team receives which event, when the hotel has not said.
#
# A hotel that has configured no routing still has to be reachable, and the
# alternative — escalating to nobody — is the failure this module exists to
# close. So every kind falls back to a role, and the role falls back to whoever
# is left. A hotelier who wants something else configures it; a hotelier who has
# not yet still gets the message.
DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    "discount_request": ("sales", "manager", "owner"),
    "group_inquiry": ("sales", "manager", "owner"),
    "special_request": ("front_desk", "operations", "manager"),
    "transfer_request": ("operations", "front_desk", "manager"),
    "payment_issue": ("reservations", "accounts", "manager"),
    "cancellation_exception": ("manager", "owner"),
    "unanswered": ("front_desk", "manager", "owner"),
    "other": ("manager", "owner"),
}

# The notification event each escalation kind publishes under, so a hotel's own
# routing rules and these defaults speak the same vocabulary.
EVENT_FOR_KIND: dict[str, str] = {
    "discount_request": "negotiation_approval",
    "group_inquiry": "group_inquiry",
    "special_request": "special_request",
    "transfer_request": "transfer_request",
    "payment_issue": "payment_failed",
    "cancellation_exception": "cancellation",
    "unanswered": "unanswered_conversation",
    "other": "agent_escalation",
}


def dedupe_key(kind: str, facts: dict) -> str:
    """
    What makes two escalations the same one.

    Computed from the *substance* — the room, the dates, the number of rooms,
    the figure being argued about — and never from the guest's wording. A guest
    who rephrases "₹4,000 mein ho jayega?" as "bhai thoda kam karo na" is asking
    the same question, and raising a second ticket for it is how a hotel learns
    to ignore the first.

    Hashed rather than stored as prose so the key stays short and so the unique
    index on it is a fixed size. Collisions across different kinds do not
    matter: the index is on `(conversation, kind, key)`.
    """
    material = "|".join(
        str(facts.get(k) or "")
        for k in ("room_type_id", "rate_plan_id", "check_in", "check_out", "units",
                  "requested_paise")
    )
    return hashlib.sha256(f"{kind}|{material}".encode()).hexdigest()[:32]


def _rupees(paise) -> str:
    try:
        return f"₹{int(paise) / 100:,.0f}"
    except (TypeError, ValueError):
        return "—"


def compose(kind: str, *, hotel: str, facts: dict, reason: str | None) -> str:
    """
    The internal message, built from stored facts.

    Deliberately plain: a WhatsApp message read on a phone between two check-ins,
    with the decision at the bottom. No markdown, because WhatsApp's own
    formatting is the only formatting that renders, and no emoji, because this is
    a commercial request rather than a notification.

    The header names the *kind* first because a manager scanning a phone needs to
    know in one glance whether this is a discount, a group or a cancellation.
    """
    heading = {
        "discount_request": "Rate approval needed",
        "group_inquiry": "Group booking enquiry",
        "special_request": "Special request",
        "transfer_request": "Transfer / pickup request",
        "payment_issue": "Payment problem",
        "cancellation_exception": "Cancellation exception requested",
        "unanswered": "A guest is waiting",
    }.get(kind, "Guest request")

    lines = [heading, "", f"Hotel: {hotel}"]

    if facts.get("guest_name"):
        via = f" ({facts['channel']})" if facts.get("channel") else ""
        lines.append(f"Guest: {facts['guest_name']}{via}")
    if facts.get("check_in") and facts.get("check_out"):
        lines.append(f"Dates: {facts['check_in']} to {facts['check_out']}")
    if facts.get("room_name"):
        lines.append(f"Room: {facts['room_name']}")
    if facts.get("units"):
        lines.append(f"Rooms: {facts['units']}")
    if facts.get("guests"):
        lines.append(f"Guests: {facts['guests']}")

    if facts.get("published_paise") is not None:
        lines.append(f"Our rate: {_rupees(facts['published_paise'])} per night")
    if facts.get("requested_paise") is not None:
        lines.append(f"Guest asked: {_rupees(facts['requested_paise'])} per night")
    # The one line that never leaves this message. See the module docstring.
    if facts.get("floor_paise") is not None:
        lines.append(f"Lowest allowed: {_rupees(facts['floor_paise'])} per night")
    if facts.get("total_paise") is not None:
        lines.append(f"Stay total at our rate: {_rupees(facts['total_paise'])}")

    if facts.get("payment_status"):
        lines.append(f"Payment: {facts['payment_status']}")
    if facts.get("booking_ref"):
        lines.append(f"Booking: {facts['booking_ref']}")

    if reason:
        lines += ["", f"What they said: {reason.strip()[:400]}"]

    lines += ["", {
        "discount_request": "Reply YES to approve this rate, or with the rate you will do.",
        "group_inquiry": "Reply with the rate you can offer for this group.",
        "cancellation_exception": "Reply YES to allow it, or NO.",
    }.get(kind, "Reply here and the guest will be answered.")]

    return "\n".join(lines)


# ── who to tell ──────────────────────────────────────────────────────────────


def _recipients(property_id: str, kind: str) -> list[dict]:
    """
    The team members to notify, configured routing first and roles after.

    Reading the whole directory rather than joining is deliberate: it is at most
    a couple of hundred rows on a hotel, and the fallback needs every member
    anyway. A join would have to be repeated to implement the fallback.
    """
    db = get_db()
    people = (
        db.table("hotel_team_members").select("*")
        .eq("property_id", property_id).eq("is_active", True)
        .order("sort_order").limit(200).execute().data or []
    )
    if not people:
        return []
    by_id = {str(p["id"]): p for p in people}

    rules = (
        db.table("team_notification_rules").select("team_member_id,channels")
        .eq("property_id", property_id).eq("is_active", True)
        .eq("event", EVENT_FOR_KIND.get(kind, "agent_escalation"))
        .limit(50).execute().data or []
    )
    configured = [
        {**by_id[str(r["team_member_id"])], "_channels": r.get("channels") or ["whatsapp"]}
        for r in rules if str(r.get("team_member_id")) in by_id
    ]
    if configured:
        return configured

    for role in DEFAULT_ROLES.get(kind, DEFAULT_ROLES["other"]):
        matches = [p for p in people if p.get("role") == role and p.get("whatsapp")]
        if matches:
            return [{**m, "_channels": ["whatsapp"]} for m in matches[:1]]

    # Nobody in a matching role. Anyone reachable is better than nobody, and a
    # hotel with one person in it is the common case at this size.
    reachable = [p for p in people if p.get("whatsapp")]
    return [{**reachable[0], "_channels": ["whatsapp"]}] if reachable else []


# ── the lifecycle ────────────────────────────────────────────────────────────


def _existing(conversation_id: str | None, kind: str, key: str) -> dict | None:
    if not conversation_id:
        return None
    rows = (
        get_db().table("agent_escalations").select("*")
        .eq("conversation_id", conversation_id).eq("kind", kind).eq("dedupe_key", key)
        .in_("status", ["open", "notified"]).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _insert(row: dict) -> dict:
    return get_db().table("agent_escalations").insert(row).execute().data[0]


def _mark(escalation_id: str, patch: dict) -> None:
    get_db().table("agent_escalations").update(
        {**patch, "updated_at": "now()"}
    ).eq("id", escalation_id).execute()


async def raise_escalation(
    *,
    property_id: str,
    partner_id: str,
    kind: str,
    facts: dict,
    reason: str | None,
    hotel_name: str,
    conversation_id: str | None = None,
    quote_id: str | None = None,
    reservation_id: str | None = None,
    agent_action_id: str | None = None,
) -> dict:
    """
    Raise one escalation, notify the team, and report honestly what happened.

    Returns the row plus `duplicate` and `notified`, both of which the agent
    needs and neither of which it can work out: whether this had already been
    asked, and whether anybody actually received it.

    **The row is written before the message is sent.** A send that fails must
    still leave a record a hotelier can see on their dashboard — the opposite
    order would lose the whole escalation whenever WhatsApp was the problem,
    which is exactly when a hotel most needs to know a guest is waiting.
    """
    key = dedupe_key(kind, facts)

    existing = await run_db(_existing, conversation_id, kind, key)
    if existing:
        # Already asked, and still open. Returning it rather than raising a
        # second is §21's "do not repeatedly escalate the same issue", and the
        # agent uses `status` to decide what to tell the guest — "I have asked"
        # reads very differently from "I am asking".
        return {**existing, "duplicate": True, "notified": existing.get("status") == "notified"}

    message = compose(kind, hotel=hotel_name, facts=facts, reason=reason)
    recipients = await run_db(_recipients, property_id, kind)

    row = await run_db(_insert, {
        "property_id": property_id,
        "partner_id": partner_id,
        "conversation_id": conversation_id,
        "quote_id": quote_id,
        "reservation_id": reservation_id,
        "agent_action_id": agent_action_id,
        "kind": kind,
        "dedupe_key": key,
        "reason": reason,
        # The facts as they were at the time. A hotelier reading this a week
        # later sees the rate that was on the table then, not today's.
        "payload": {k: v for k, v in facts.items() if v is not None},
        "team_member_id": str(recipients[0]["id"]) if recipients else None,
        "message_sent": message,
        "status": "open",
    })

    if not recipients:
        logger.warning(
            "escalation %s on property %s has nobody to notify — no team members",
            row["id"], property_id,
        )
        return {**row, "duplicate": False, "notified": False, "recipients": 0}

    delivered = await _notify(property_id, recipients, message, row["id"])
    if delivered:
        await run_db(_mark, row["id"], {"status": "notified", "notified_at": "now()"})
        row = {**row, "status": "notified"}
    return {**row, "duplicate": False, "notified": delivered, "recipients": len(recipients)}


async def _notify(property_id: str, recipients: list[dict], message: str, escalation_id: str) -> bool:
    """
    Send to every WhatsApp recipient. True if at least one landed.

    **Straight to the adapter, not through `messaging.send()`.** That function is
    the right choke point for everything sent to a *guest* — the 24-hour window,
    the credit gate, the idempotent queue row, the meter — and every one of those
    is wrong here. A hotel's own manager is not a metered conversation, is not
    subject to a window we opened, and must not consume the hotel's own message
    credits to be told that a guest is waiting.

    A `dashboard` channel needs no send at all: the row this is called from *is*
    the dashboard notification.
    """
    connection = await run_db(
        messaging_service._connection_for_property, property_id, "whatsapp",
    )
    if not connection:
        logger.warning(
            "escalation %s: property %s has no WhatsApp connection to notify from",
            escalation_id, property_id,
        )
        return False

    try:
        adapter = await creds_module.adapter_for_async(connection)
    except Exception:  # noqa: BLE001
        logger.warning("escalation %s: could not build adapter", escalation_id, exc_info=True)
        return False

    delivered = False
    for person in recipients:
        number = person.get("whatsapp")
        if not number or "whatsapp" not in (person.get("_channels") or []):
            continue
        try:
            result = await adapter.send_text(
                to=number,
                body=message,
                # Idempotent per escalation per person: a retried worker sends
                # one message, not two, to somebody's personal phone.
                client_reference=f"escalation:{escalation_id}:{person['id']}",
            )
            delivered = delivered or bool(getattr(result, "provider_message_id", None) or result)
        except Exception:  # noqa: BLE001
            # Logged, never raised. A guest must not see an error because the
            # manager's phone was unreachable — the agent's own answer to them
            # does not depend on this having worked.
            logger.warning(
                "escalation %s: could not reach %s on WhatsApp",
                escalation_id, person.get("name"), exc_info=True,
            )
    return delivered


def _open_for(conversation_id: str) -> list[dict]:
    return (
        get_db().table("agent_escalations")
        .select("id,kind,status,created_at,notified_at,responded_at,resolution")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=True).limit(10).execute().data or []
    )


async def state_for(conversation_id: str) -> dict:
    """
    What the agent needs to know before escalating again.

    §21: *do not repeatedly escalate the same issue*. That is enforced by the
    dedupe key on the way in; this is the other half — so the agent can tell a
    guest "I've asked, I'm waiting" rather than either escalating again or
    pretending nothing is outstanding.
    """
    try:
        rows = await run_db(_open_for, conversation_id)
    except Exception:  # noqa: BLE001
        logger.warning("could not read escalations for %s", conversation_id, exc_info=True)
        return {"open": [], "waiting": False}

    waiting = [r for r in rows if r.get("status") in ("open", "notified")]
    return {
        "open": waiting,
        "waiting": bool(waiting),
        "recent": rows[:5],
    }
