"""
The creator boundary.

    /stay/[slug]?c=CODE  ──►  record_touch()        ──► record_attribution_touch()
    guest messages hotel ──►  bind_conversation()   ──► the same chain
    reservation created  ──►  resolve()             ──► creator_resolve_attribution()
    owner presses approve ─►  approve()             ──► creator_approve_payouts()

**No arithmetic in this file, and there must never be.** Every figure a hotel or
a creator is shown comes out of `creator_payout_recalc` and
`creator_resolve_attribution` in migration 23 — one implementation, in the place
that also holds the row locks. The parallel is exact with `b2b.py`: an amount
shown to a creator that is computed differently from the one an owner approves
is worse than showing no amount at all.

What this module *does* add is the three things the database cannot do for
itself:

**A visitor handle.** A click has to be remembered across weeks and channels,
and the thing that remembers it is a random ref the server mints and the browser
carries. It is not a claim about which creator is owed money — the browser could
not make that claim, because the mapping from ref to creator exists only in
`attribution_touches`. It is a handle on a row we wrote.

**A token the guest re-sends without knowing it.** `touch_ref` goes into the
prefilled text of the hotel's WhatsApp link. When the message arrives we read
our own token out of it and bind the conversation to the chain. That is what
carries attribution across the detour PRD §16 says is the common path, and it
asks the guest for nothing.

**Rolled-up reading.** The card an owner sees — clicks, bookings, room nights,
revenue, what is pending approval — is five counts over the chain. They are
computed here rather than in a view because the PWA wants them per participation
and the underlying rows are already scoped by RLS.

⚠ Nothing here accepts a creator id, a campaign id or an amount from a caller.
The nearest thing is a public *code*, which resolves to a row server-side, and a
participation id in a staff path, which is checked against the property before
anything is read.
"""
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException, status

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# The token that survives a channel change. Deliberately short and unambiguous —
# it is read off a phone screen and travels inside a message a person typed
# around, so no vowels and no characters that look like each other.
_TOUCH_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_TOUCH_RE = re.compile(r"\bCR-([23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6})\b", re.IGNORECASE)


def new_visitor_ref() -> str:
    return "vr_" + secrets.token_urlsafe(18)


def new_touch_ref() -> str:
    return "CR-" + "".join(secrets.choice(_TOUCH_ALPHABET) for _ in range(6))


def find_touch_ref(text: str | None) -> str | None:
    """
    Our own token, read back out of a guest's message.

    Case-insensitive because phone keyboards capitalise the start of a sentence,
    and returned upper-cased because that is how it is stored.
    """
    if not text:
        return None
    found = _TOUCH_RE.search(text)
    return f"CR-{found.group(1).upper()}" if found else None


# ── recording a touch ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Touch:
    """What a public caller is told after clicking a creator's link."""

    ok: bool
    reason: str | None
    duplicate: bool
    touch_id: str | None
    touch_ref: str | None
    visitor_ref: str | None
    creator_name: str | None
    creator_handle: str | None
    campaign_name: str | None
    guest_discount_pct: float


def _record(payload: dict) -> dict:
    return get_db().rpc("record_attribution_touch", payload).execute().data or {}


async def record_touch(
    code: str,
    *,
    kind: str = "click",
    source: str = "website",
    visitor_ref: str | None = None,
    city: str | None = None,
    event_key: str | None = None,
    metadata: dict | None = None,
) -> Touch:
    """
    Record one click on a creator's link.

    **A campaign that is not live records nothing**, and this returns
    `ok=False` rather than raising. The page still renders: a guest who follows
    a link from a post the hotel stopped promoting last week should see the
    hotel, not an error about a campaign they have never heard of. What they do
    not get is an attribution, which is the hotel's decision and not theirs.

    The visitor ref is minted here when the browser has none, and reused when it
    has one — that reuse is what makes a second click on the same day one touch
    instead of two, and it is enforced in the database's event key rather than
    trusted to this function.
    """
    verdict = await run_db(_record, {
        "p_code": code,
        "p_kind": kind,
        "p_source": source,
        "p_visitor_ref": visitor_ref or new_visitor_ref(),
        "p_touch_ref": new_touch_ref(),
        "p_city": city,
        "p_event_key": event_key,
        "p_metadata": metadata or {},
    })
    return Touch(
        ok=bool(verdict.get("ok")),
        reason=verdict.get("reason"),
        duplicate=bool(verdict.get("duplicate")),
        touch_id=verdict.get("touchId"),
        touch_ref=verdict.get("touchRef"),
        visitor_ref=verdict.get("visitorRef"),
        creator_name=verdict.get("creatorName"),
        creator_handle=verdict.get("creatorHandle"),
        campaign_name=verdict.get("campaignName"),
        guest_discount_pct=float(verdict.get("guestDiscountPct") or 0),
    )


def _bind(property_id: str, conversation_id: str, touch_ref: str) -> dict | None:
    db = get_db()
    rows = (
        db.table("attribution_touches").select("*")
        .eq("touch_ref", touch_ref).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    if not rows:
        return None
    touch = rows[0]
    if touch.get("conversation_id"):
        # Already bound. Re-binding is refused by the append-only trigger, and
        # the honest answer to "did this work" is yes — it worked the first time.
        return touch

    db.table("attribution_touches").update(
        {"conversation_id": conversation_id}
    ).eq("id", touch["id"]).execute()

    # A second touch, of its own kind, so the chain reads as what happened
    # rather than as a click that mysteriously acquired a phone number. It
    # carries the original's window: a detour does not extend a campaign.
    db.table("attribution_touches").insert({
        "property_id": touch["property_id"],
        "partner_id": touch["partner_id"],
        "campaign_id": touch["campaign_id"],
        "creator_id": touch["creator_id"],
        "campaign_creator_id": touch["campaign_creator_id"],
        "kind": "conversation",
        "source": "whatsapp",
        "expires_at": touch["expires_at"],
        "visitor_ref": touch.get("visitor_ref"),
        "conversation_id": conversation_id,
        "event_key": f"conversation:{touch['id']}:{conversation_id}",
        "metadata": {"from": touch["id"]},
    }).execute()
    return touch


async def bind_conversation(
    property_id: str, conversation_id: str, text: str | None
) -> str | None:
    """
    Bind a chain to a conversation, from a token the guest sent back.

    Called on every inbound message on every channel. Almost every call finds
    nothing and costs one regex, which is the price of the one call in a hundred
    that carries `CR-7X2K9M` and turns an anonymous WhatsApp thread into an
    attributable one.

    Returns the touch ref it bound, or None. Failures are the caller's to
    swallow: a creator attribution that could discard a guest's message would be
    a very expensive way to track a commission.
    """
    ref = find_touch_ref(text)
    if not ref:
        return None
    touch = await run_db(_bind, property_id, conversation_id, ref)
    return ref if touch else None


# ── resolving a booking ──────────────────────────────────────────────────────

def _resolve(reservation_id: str, visitor_ref: str | None, conversation_id: str | None) -> dict:
    return get_db().rpc("creator_resolve_attribution", {
        "p_reservation": reservation_id,
        "p_visitor_ref": visitor_ref,
        "p_conversation": conversation_id,
    }).execute().data or {}


async def resolve(
    reservation_id: str,
    *,
    visitor_ref: str | None = None,
    conversation_id: str | None = None,
) -> dict:
    """
    Decide which creator, if any, produced a booking. Never raises.

    Called after `create_reservation` on every path that can carry a creator —
    the public booking engine, the front desk, and the AI agents' booking tool.
    It is deliberately best-effort: a guest who has paid must have a booking
    whether or not a commission could be worked out, so a failure here is logged
    and swallowed rather than turning a completed payment into a 500.

    The reservation is safe to re-resolve. `creator_resolve_attribution` is
    idempotent on it, so a retried confirmation and a manual re-run are the same
    call, and neither can produce a second payout — the unique constraint on
    `creator_payouts.reservation_id` is what guarantees that, not this function
    being called carefully.
    """
    try:
        return await run_db(_resolve, reservation_id, visitor_ref, conversation_id)
    except Exception:  # noqa: BLE001 — attribution must never cost a guest a booking
        logger.exception("could not resolve creator attribution for %s", reservation_id)
        return {"ok": False, "reason": "error"}


def _conversation_for(reservation_id: str) -> str | None:
    """
    The thread a booking came out of, if there is one.

    Read rather than passed, because the booking tool the agents call does not
    carry a conversation id and giving it one would put attribution into the
    model's reach. `conversations.reservation_id` is already maintained by the
    messaging service for the inbox.
    """
    rows = (
        get_db().table("conversations").select("id")
        .eq("reservation_id", reservation_id).limit(1).execute().data or []
    )
    return rows[0]["id"] if rows else None


async def resolve_for_reservation(reservation: dict, *, visitor_ref: str | None = None) -> dict:
    """`resolve`, with the conversation looked up rather than supplied."""
    conversation_id = await run_db(_conversation_for, reservation["id"])
    return await resolve(
        reservation["id"], visitor_ref=visitor_ref, conversation_id=conversation_id
    )


# ── what the hotel reads ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Participation:
    """One creator in one campaign, as a hotel sees it."""

    id: str
    campaign_id: str
    campaign_name: str
    property_id: str
    partner_id: str
    creator_id: str
    creator_name: str
    creator_handle: str
    platform: str
    followers: int
    code: str
    status: str
    start_date: date
    end_date: date
    commission_pct: float
    guest_discount_pct: float
    attribution_window_days: int
    min_nights: int
    clicks: int
    conversations: int
    bookings: int
    room_nights: int
    revenue_paise: int
    payout_paise: int
    approved_paise: int
    payout_status: str


# Campaign status and participation status, resolved into the one word a hotel
# is shown. The participation is the narrower of the two and therefore wins: a
# creator paused inside a live campaign is paused.
def _effective_status(campaign_status: str, participation_status: str) -> str:
    if participation_status == "removed" or campaign_status == "ended":
        return "ended"
    if participation_status == "paused":
        return "paused"
    if participation_status == "invited":
        # Invited but not yet started. The card reads "draft" because that is
        # what it is from the hotel's side — a creator who has not begun.
        return "draft"
    return campaign_status


def _read_participations(property_id: str) -> list[dict]:
    db = get_db()
    parts = (
        db.table("campaign_creators").select("*")
        .eq("property_id", property_id)
        .order("created_at", desc=True)
        .limit(200).execute().data or []
    )
    if not parts:
        return []

    ids = [p["id"] for p in parts]
    campaigns = {
        c["id"]: c for c in
        (db.table("creator_campaigns").select("*")
         .in_("id", list({p["campaign_id"] for p in parts}))
         .execute().data or [])
    }
    creators = {
        c["id"]: c for c in
        (db.table("creators").select("*")
         .in_("id", list({p["creator_id"] for p in parts}))
         .execute().data or [])
    }
    touches = (
        db.table("attribution_touches").select("campaign_creator_id,kind")
        .in_("campaign_creator_id", ids).limit(10000).execute().data or []
    )
    payouts = (
        db.table("creator_payouts")
        .select("campaign_creator_id,status,room_nights,net_revenue_paise,amount_paise")
        .in_("campaign_creator_id", ids).limit(5000).execute().data or []
    )
    return {"parts": parts, "campaigns": campaigns, "creators": creators,
            "touches": touches, "payouts": payouts}


def _roll_up(bundle: dict) -> list[Participation]:
    counts: dict[str, dict] = {}
    for t in bundle["touches"]:
        row = counts.setdefault(t["campaign_creator_id"], {"click": 0, "conversation": 0})
        if t["kind"] in row:
            row[t["kind"]] += 1

    money: dict[str, dict] = {}
    for p in bundle["payouts"]:
        row = money.setdefault(p["campaign_creator_id"], {
            "bookings": 0, "nights": 0, "revenue": 0, "pending": 0, "approved": 0,
            "statuses": set(),
        })
        row["bookings"] += 1
        row["nights"] += int(p.get("room_nights") or 0)
        row["revenue"] += int(p.get("net_revenue_paise") or 0)
        row["statuses"].add(p["status"])
        if p["status"] == "pending_approval":
            row["pending"] += int(p.get("amount_paise") or 0)
        elif p["status"] in ("approved", "paid"):
            row["approved"] += int(p.get("amount_paise") or 0)

    out: list[Participation] = []
    for part in bundle["parts"]:
        campaign = bundle["campaigns"].get(part["campaign_id"]) or {}
        creator = bundle["creators"].get(part["creator_id"]) or {}
        touch = counts.get(part["id"], {"click": 0, "conversation": 0})
        cash = money.get(part["id"], {"bookings": 0, "nights": 0, "revenue": 0,
                                      "pending": 0, "approved": 0, "statuses": set()})
        statuses = cash["statuses"]

        # The one word on the card. "Awaiting approval" outranks "approved"
        # because it is the one that needs somebody to do something.
        if "pending_approval" in statuses:
            payout_status = "pending_approval"
        elif statuses & {"approved", "paid"}:
            payout_status = "approved"
        else:
            payout_status = "accruing"

        out.append(Participation(
            id=part["id"],
            campaign_id=part["campaign_id"],
            campaign_name=campaign.get("name") or "",
            property_id=part["property_id"],
            partner_id=part["partner_id"],
            creator_id=part["creator_id"],
            creator_name=creator.get("display_name") or "",
            creator_handle=creator.get("handle") or "",
            platform=creator.get("platform") or "other",
            followers=int(creator.get("followers") or 0),
            code=part["public_code"],
            status=_effective_status(campaign.get("status") or "draft", part["status"]),
            start_date=campaign.get("start_date"),
            end_date=campaign.get("end_date"),
            commission_pct=float(campaign.get("commission_pct") or 0),
            guest_discount_pct=float(campaign.get("guest_discount_pct") or 0),
            attribution_window_days=int(campaign.get("attribution_window_days") or 30),
            min_nights=int(campaign.get("min_nights") or 1),
            clicks=touch["click"],
            conversations=touch["conversation"],
            bookings=cash["bookings"],
            room_nights=cash["nights"],
            revenue_paise=cash["revenue"],
            payout_paise=cash["pending"],
            approved_paise=cash["approved"],
            payout_status=payout_status,
        ))
    return out


async def list_participations(property_id: str) -> list[Participation]:
    """Every creator this hotel works with, with the numbers rolled up."""
    bundle = await run_db(_read_participations, property_id)
    return _roll_up(bundle) if bundle else []


def _participation(participation_id: str, property_id: str) -> dict | None:
    rows = (
        get_db().table("campaign_creators").select("*")
        .eq("id", participation_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


async def require_participation(participation_id: str, property_id: str) -> dict:
    """
    One participation, scoped to the property in the path, or 404.

    404 and not 403, the same answer `require_member` gives for a property that
    is not yours. A campaign id that exists at another hotel must not be
    distinguishable from one that does not exist.
    """
    row = await run_db(_participation, participation_id, property_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return row


def _read_touches(participation_id: str, limit: int) -> dict:
    db = get_db()
    touches = (
        db.table("attribution_touches").select("*")
        .eq("campaign_creator_id", participation_id)
        .order("occurred_at", desc=True)
        .limit(limit).execute().data or []
    )
    reservation_ids = [t["reservation_id"] for t in touches if t.get("reservation_id")]
    reservations = {
        r["id"]: r for r in
        (db.table("reservations")
         .select("id,ref,nights,units,total_paise,status,check_in,check_out")
         .in_("id", reservation_ids).execute().data or [])
    } if reservation_ids else {}
    return {"touches": touches, "reservations": reservations}


async def list_touches(participation_id: str, limit: int = 100) -> list[dict]:
    """
    The evidence chain for one creator, newest first.

    Reservations are joined in because the answer to "why did we pay this" is
    not a uuid — it is `TB-8K3M2Q`, three nights, ₹11,800. A trail an owner
    cannot read is a trail that settles no argument.
    """
    bundle = await run_db(_read_touches, participation_id, limit)
    out = []
    for touch in bundle["touches"]:
        reservation = bundle["reservations"].get(touch.get("reservation_id")) or {}
        out.append({**touch, "reservation": reservation})
    return out


def _read_payouts(participation_id: str) -> dict:
    db = get_db()
    payouts = (
        db.table("creator_payouts").select("*")
        .eq("campaign_creator_id", participation_id)
        .order("created_at", desc=True)
        .limit(500).execute().data or []
    )
    ids = [p["id"] for p in payouts]
    reservations = {
        r["id"]: r for r in
        (db.table("reservations")
         .select("id,ref,guest_name,nights,units,check_in,check_out,status,"
                 "gross_paise,discount_paise,total_paise,paid_paise,source")
         .in_("id", [p["reservation_id"] for p in payouts]).execute().data or [])
    } if payouts else {}
    events = (
        db.table("creator_payout_events").select("*")
        .in_("payout_id", ids).order("at").execute().data or []
    ) if ids else []
    return {"payouts": payouts, "reservations": reservations, "events": events}


async def list_payouts(participation_id: str) -> list[dict]:
    """Every payout under one participation, each carrying its stay and its trail."""
    bundle = await run_db(_read_payouts, participation_id)
    by_payout: dict[str, list[dict]] = {}
    for event in bundle["events"]:
        by_payout.setdefault(event["payout_id"], []).append(event)
    return [
        {
            **payout,
            "reservation": bundle["reservations"].get(payout["reservation_id"]) or {},
            "events": by_payout.get(payout["id"], []),
        }
        for payout in bundle["payouts"]
    ]


# ── what the hotel changes ───────────────────────────────────────────────────

# What the card's two buttons mean, in the participation's own vocabulary. The
# campaign has a lifecycle of its own — draft → live → paused → ended, enforced
# by a trigger — and this is deliberately not it: pausing one creator is a far
# more common act than pausing a programme, and a screen where the pause button
# stops three other creators too would be a screen nobody trusts.
_STATUS_TO_PARTICIPATION = {
    "live": "active",
    "paused": "paused",
    "ended": "removed",
}

_PARTICIPATION_TRANSITIONS = {
    "invited": {"active", "removed"},
    "active": {"paused", "removed"},
    "paused": {"active", "removed"},
    # Removing a creator from a campaign is final. Re-adding them is a new row
    # with a new code, so the touches of the first run stay unambiguous about
    # which run they belonged to.
    "removed": set(),
}


def _set_status(participation_id: str, target: str) -> dict:
    db = get_db()
    row = (
        db.table("campaign_creators").select("*")
        .eq("id", participation_id).limit(1).execute().data[0]
    )
    if row["status"] == target:
        return row
    if target not in _PARTICIPATION_TRANSITIONS.get(row["status"], set()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A creator who has been {row['status']} cannot be moved to {target}.",
        )
    updated = (
        db.table("campaign_creators").update({"status": target})
        .eq("id", participation_id).execute().data[0]
    )

    # Going live on a creator inside a campaign nobody has started is what a
    # hotel means by pressing the button, so the campaign follows. It is the one
    # direction that is safe to infer: the reverse would let one creator's pause
    # silently stop three others.
    campaign = (
        db.table("creator_campaigns").select("*")
        .eq("id", row["campaign_id"]).limit(1).execute().data[0]
    )
    if target == "active" and campaign["status"] == "draft":
        db.table("creator_campaigns").update({"status": "live"}) \
          .eq("id", campaign["id"]).execute()
    return updated


async def set_status(participation_id: str, property_id: str, status_word: str) -> dict:
    """
    Move one creator's participation, or refuse.

    The four words the PWA uses are the campaign's vocabulary; what actually
    moves is the participation. `draft` is not settable at all — a campaign
    cannot be un-launched, and a screen that offered it would be offering to
    make the touches already recorded ambiguous.
    """
    await require_participation(participation_id, property_id)
    target = _STATUS_TO_PARTICIPATION.get(status_word)
    if target is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A campaign can be resumed, paused or ended.",
        )
    return await run_db(_set_status, participation_id, target)


def _approve(participation_id: str, profile_id: str | None,
             member_id: str | None, name: str, note: str | None) -> dict:
    return get_db().rpc("creator_approve_payouts", {
        "p_campaign_creator": participation_id,
        "p_actor_profile": profile_id,
        "p_actor_member": member_id,
        "p_actor_name": name,
        "p_note": note,
    }).execute().data or {}


async def approve(
    participation_id: str,
    property_id: str,
    *,
    profile_id: str | None,
    member_id: str | None,
    name: str,
    note: str | None = None,
) -> dict:
    """
    Approve everything currently payable for one creator.

    The amount is not a parameter and never will be. It is read out of
    `creator_payouts`, where it was written by `creator_payout_recalc` from the
    reservation — so the only way to change what is approved is to change the
    stay it came from, which is exactly the property worth having.

    Approving twice approves nothing the second time and says so. That is not a
    convenience: two owners pressing the button at the same moment serialise on
    the participation's row lock, and the second finds nothing pending.
    """
    await require_participation(participation_id, property_id)
    verdict = await run_db(_approve, participation_id, profile_id, member_id, name, note)

    if not verdict.get("ok"):
        if verdict.get("reason") == "self":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "A creator cannot approve their own payout.",
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
    return verdict


def _recalc(payout_id: str) -> dict:
    return get_db().rpc("creator_payout_recalc", {"p_payout": payout_id}).execute().data or {}


def _payouts_for_reservation(reservation_id: str) -> list[str]:
    return [
        row["id"] for row in
        (get_db().table("creator_payouts").select("id")
         .eq("reservation_id", reservation_id).execute().data or [])
    ]


async def recalculate_for_reservation(reservation_id: str) -> list[dict]:
    """
    Recompute whatever a change to this stay was worth.

    Called when a reservation moves — checked out, cancelled, a payment posted,
    dates changed. Almost every call finds nothing, because almost no booking is
    a creator's. An approved payout is refused by the database rather than
    skipped here, which is the difference between a rule and a habit.
    """
    try:
        ids = await run_db(_payouts_for_reservation, reservation_id)
        return [await run_db(_recalc, payout_id) for payout_id in ids]
    except Exception:  # noqa: BLE001 — a commission must not block a check-out
        logger.exception("could not recalculate creator payouts for %s", reservation_id)
        return []


# ── what the hotel creates ───────────────────────────────────────────────────

_CODE_CLEAN = re.compile(r"[^A-Z0-9]+")


def public_code_for(handle: str) -> str:
    """
    A shareable code derived from the handle, with a short random tail.

    The handle makes it recognisable — a creator posting `MAYAROAMS-K3M2` can
    see it is theirs — and the tail makes it unguessable, which matters because
    the code is the only thing standing between a competitor and a stream of
    attributed clicks they did not earn. Four characters of the same alphabet
    the touch refs use, so nothing here is ambiguous read aloud.
    """
    stem = _CODE_CLEAN.sub("", (handle or "").upper())[:14] or "CREATOR"
    tail = "".join(secrets.choice(_TOUCH_ALPHABET) for _ in range(4))
    return f"{stem}-{tail}"


def _create(property_id: str, partner_id: str, member_id: str | None, body: dict) -> dict:
    db = get_db()

    handle = (body["creator_handle"] or "").strip()
    if not handle.startswith("@"):
        handle = "@" + handle
    platform = body.get("platform") or "instagram"

    # One creator per handle per platform, platform-wide. A hotel adding someone
    # another hotel already works with joins the same person rather than making
    # a second one — which is what stops one creator becoming two payouts.
    existing = (
        db.table("creators").select("*")
        .eq("platform", platform).ilike("handle", handle)
        .limit(1).execute().data or []
    )
    if existing:
        creator = existing[0]
        if int(body.get("followers") or 0) > int(creator.get("followers") or 0):
            db.table("creators").update(
                {"followers": int(body["followers"])}
            ).eq("id", creator["id"]).execute()
    else:
        creator = db.table("creators").insert({
            "display_name": body["creator_name"].strip(),
            "handle": handle,
            "platform": platform,
            "followers": int(body.get("followers") or 0),
            "city": body.get("city"),
        }).execute().data[0]

    campaign_id = body.get("campaign_id")
    if campaign_id:
        rows = (
            db.table("creator_campaigns").select("*")
            .eq("id", campaign_id).eq("property_id", property_id)
            .limit(1).execute().data or []
        )
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")
        campaign = rows[0]
    else:
        campaign = db.table("creator_campaigns").insert({
            "property_id": property_id,
            "partner_id": partner_id,
            "name": body["campaign_name"].strip(),
            # Draft, always. A campaign goes live when somebody presses the
            # button, not as a side effect of being typed in — and the difference
            # matters, because a live campaign records touches.
            "status": "draft",
            "start_date": str(body["start_date"]),
            "end_date": str(body["end_date"]),
            "commission_pct": body["commission_pct"],
            "guest_discount_pct": body.get("guest_discount_pct") or 0,
            "attribution_window_days": int(body.get("attribution_window_days") or 30),
            "min_nights": int(body.get("min_nights") or 1),
            "created_by": member_id,
        }).execute().data[0]

    # The code is generated, never supplied. A hotel that could choose it could
    # choose one already in use at another property, and the unique index would
    # turn a typo into a 500 on a screen with no way to recover.
    for _ in range(5):
        try:
            participation = db.table("campaign_creators").insert({
                "campaign_id": campaign["id"],
                "creator_id": creator["id"],
                "property_id": property_id,
                "partner_id": partner_id,
                "public_code": public_code_for(handle),
                "status": "invited",
            }).execute().data[0]
            return participation
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if "campaign_creators_unique" in message:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "That creator is already on this campaign.",
                ) from exc
            if "campaign_creators_code_idx" not in message:
                raise
    raise HTTPException(
        status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not allocate a creator code"
    )


async def create(property_id: str, partner_id: str, member_id: str | None,
                 body: dict) -> dict:
    """
    Add a creator to a campaign, making both if they are new.

    Returns the participation, which is the row the rest of this module is keyed
    on. It starts `invited` and its campaign starts `draft`: nothing records a
    touch until somebody presses go live, which is the only reading of "draft"
    that means anything once clicks are money.
    """
    return await run_db(_create, property_id, partner_id, member_id, body)
