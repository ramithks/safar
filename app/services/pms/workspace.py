"""
Who the caller is, what they may open, and the day's numbers.

Everything here answers a question the PWA asks *before* it can show anything:
which properties exist for this person, what may they do in each, and what does
today look like. All three are derived from `auth.uid()` and nothing else — no
endpoint in this module accepts a partner_id or a property list from the client,
because a client that can name its own partner can name someone else's.

`today_stats` deliberately reimplements the arithmetic the PWA's mock used
rather than inventing its own. Those definitions are the ones a hotelier will
argue about, so they are pinned here in one place:

  ADR      revenue per room-night *sold*, not average reservation value
  RevPAR   the same revenue over every room that existed, sold or not
  in-house units, not reservations — a booking of three rooms is three
  occupancy room-nights sold today over total units

Getting ADR wrong is not a rounding error; it is the number a property prices
next season on.
"""
import logging
from datetime import timedelta

from app.db.client import get_db, run_db
from app.deps.property import ROLE_PERMS
from app.services.pms import clock

logger = logging.getLogger(__name__)

# Reservations in these states are not real occupancy and must not count toward
# anything. Kept as one tuple so no query forgets one of them.
LIVE_STATUSES = ("pending", "confirmed", "checked_in", "checked_out")

# ─────────────────────────────────────────────────────────────────────────────
# The front-desk buckets, defined exactly once.
#
# These used to be written twice — a count here and a list in the reservations
# endpoint — with different status sets, so Today said "6 arrivals" while the
# arrivals list showed 4 and neither was wrong on its own terms. A number and
# the list behind it disagreeing is worse than either being wrong, because the
# receptionist has no way to work out which to trust.
#
# `pending` is excluded: an unconfirmed booking is not expected at the desk
# today. `checked_out` is included: a guest who arrived this morning and left
# this afternoon still arrived today, and dropping them would make the count
# fall during the day for no visible reason.
# ─────────────────────────────────────────────────────────────────────────────
ARRIVAL_STATUSES = ("confirmed", "checked_in", "checked_out")
DEPARTURE_STATUSES = ("checked_in", "checked_out")
IN_HOUSE_STATUSES = ("checked_in",)


def is_arrival(row: dict, business_date: str) -> bool:
    return row["check_in"] == business_date and row["status"] in ARRIVAL_STATUSES


def is_departure(row: dict, business_date: str) -> bool:
    return row["check_out"] == business_date and row["status"] in DEPARTURE_STATUSES


def _memberships_for(profile_id: str) -> list[dict]:
    """
    Every partner this profile can act for, with the role resolved.

    Ownership is checked separately from membership for the same reason
    `deps/property._load` does: migration 1 backfills owners as members, but a
    partner created after that backfill would otherwise be locked out of their
    own hotel.
    """
    db = get_db()

    rows = (
        db.table("partner_members")
        .select("id,partner_id,role,property_ids,permissions")
        .eq("profile_id", profile_id)
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    by_partner = {r["partner_id"]: r for r in rows}

    owned = (
        db.table("partners")
        .select("id")
        .eq("owner_id", profile_id)
        .execute()
        .data
        or []
    )
    for p in owned:
        by_partner.setdefault(p["id"], {
            "id": None, "partner_id": p["id"], "role": "owner",
            "property_ids": [], "permissions": {},
        })
        # An owner who also holds a member row keeps 'owner': the fallback must
        # never demote them below the role they actually have.
        by_partner[p["id"]]["role"] = "owner"

    out = []
    for r in by_partner.values():
        role = r["role"]
        extra = {k for k, v in (r.get("permissions") or {}).items() if v is True}
        perms = ROLE_PERMS.get(role, set()) | extra
        out.append({
            "partnerId": r["partner_id"],
            "memberId": r["id"],
            "role": role,
            "permissions": sorted(perms),
            "propertyIds": r.get("property_ids") or [],
        })
    return out


async def me(profile_id: str) -> dict:
    def _read():
        profile = (
            get_db().table("profiles")
            .select("id,email,full_name")
            .eq("id", profile_id)
            .limit(1)
            .execute()
            .data
        )
        return (profile[0] if profile else {}), _memberships_for(profile_id)

    profile, memberships = await run_db(_read)
    return {
        "profileId": profile_id,
        "email": profile.get("email"),
        "fullName": profile.get("full_name"),
        "memberships": memberships,
    }


async def list_properties(profile_id: str) -> list[dict]:
    """
    The properties this person may open, and only those.

    A member scoped to specific properties (`property_ids`) sees exactly those;
    an empty list means the whole partner. Filtering here rather than in the
    client is what makes `require_member` on every other route a second lock
    rather than the only one.
    """
    def _read():
        memberships = _memberships_for(profile_id)
        if not memberships:
            return []

        partner_ids = [m["partnerId"] for m in memberships]
        rows = (
            get_db().table("properties")
            .select("*")
            .in_("partner_id", partner_ids)
            .order("name")
            .execute()
            .data
            or []
        )

        scope = {m["partnerId"]: set(m["propertyIds"]) for m in memberships}
        return [
            r for r in rows
            if not scope.get(r["partner_id"]) or r["id"] in scope[r["partner_id"]]
        ]

    return await run_db(_read)


async def list_room_types(property_id: str) -> list[dict]:
    def _read():
        return (
            get_db().table("room_types")
            .select("*")
            .eq("property_id", property_id)
            .eq("is_active", True)
            .order("sort_order")
            .execute()
            .data
            or []
        )
    return await run_db(_read)


async def list_rate_plans(property_id: str) -> list[dict]:
    def _read():
        return (
            get_db().table("rate_plans")
            .select("*")
            .eq("property_id", property_id)
            .eq("is_active", True)
            .order("sort_order")
            .execute()
            .data
            or []
        )
    return await run_db(_read)


async def list_tasks(property_id: str, limit: int = 200) -> list[dict]:
    def _read():
        return (
            get_db().table("tasks")
            .select("*")
            .eq("property_id", property_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
    return await run_db(_read)


async def list_reservations(
    property_id: str,
    *,
    bucket: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
) -> tuple[list[dict], dict[str, int]]:
    """
    The reservation list, the three front-desk buckets, and every folio balance.

    Lifted out of the API endpoint when the Front Desk agent needed the same
    three buckets. It had to move rather than be copied: `is_arrival` and
    `ARRIVAL_STATUSES` are already defined once precisely so the Today count and
    the arrivals list cannot disagree, and a third caller reimplementing the
    predicate would undo that on the first status somebody forgot.

    Returns the rows and a balance map rather than merging them, because the
    endpoint and the tool project them differently and neither should have to
    take the other's shape.
    """
    today = await clock.abusiness_date_iso(property_id)

    def _read():
        db = get_db()
        q = db.table("reservations").select("*").eq("property_id", property_id)

        if bucket == "arrivals":
            q = q.eq("check_in", today).in_("status", list(ARRIVAL_STATUSES))
        elif bucket == "departures":
            q = q.eq("check_out", today).in_("status", list(DEPARTURE_STATUSES))
        elif bucket == "inhouse":
            q = q.in_("status", list(IN_HOUSE_STATUSES))

        # Half-open overlap: a stay is in the window if it has not ended before
        # it and has not started after it.
        if date_from:
            q = q.gte("check_out", date_from)
        if date_to:
            q = q.lte("check_in", date_to)
        if search:
            escaped = search.replace("%", r"\%").replace(",", " ")
            q = q.or_(
                f"guest_name.ilike.%{escaped}%,ref.ilike.%{escaped}%,"
                f"guest_phone.ilike.%{escaped}%"
            )

        rows = q.order("check_in").limit(limit).execute().data or []
        # One grouped query for the whole property rather than one per row: the
        # arrivals list is the screen a receptionist reloads all shift, and
        # N+1 against the folio is how it gets slow enough to be worked around.
        #
        # `folio_states` rather than `folio_balances`, because a sum on its own
        # cannot tell a pre-arrival deposit (negative, and the guest owes the
        # rest) from a settled in-house guest (zero, and they owe nothing). That
        # is what put "₹-10,752" on the dashboard for a booking with ₹43,008
        # still to pay. See `services/pms/money`.
        folios = {
            b["reservation_id"]: b
            for b in (db.rpc("folio_states", {"p_property": property_id}).execute().data or [])
        }
        return rows, folios

    return await run_db(_read)


async def list_rooms(property_id: str) -> tuple[list[dict], dict[str, dict]]:
    """
    Every room, and whoever is currently in it.

    Read by three callers that want opposite things: housekeeping wants the
    status board, check-in wants the rooms it may actually assign, and the Front
    Desk agent wants to answer "is 102 ready". All three need the occupant,
    because a room can be clean and still unavailable.
    """
    def _read():
        db = get_db()
        rooms = (
            db.table("rooms").select("*")
            .eq("property_id", property_id).order("number").execute().data or []
        )
        if not rooms:
            return [], {}
        # One query for the whole floor rather than one per room.
        stays = (
            db.table("reservations")
            .select("id,ref,room_id,guest_name,check_in,check_out")
            .eq("property_id", property_id)
            .eq("status", "checked_in")
            .not_.is_("room_id", "null")
            .execute()
            .data
            or []
        )
        return rooms, {s["room_id"]: s for s in stays}

    return await run_db(_read)


async def find_room(property_id: str, number: str) -> dict | None:
    """
    A room by the number a human said, or nothing.

    Case-insensitive and exact — `ilike` without wildcards, so "10" never
    matches "102". The caller is expected to treat None as a refusal rather than
    carrying the string forward: a room number that resolves to no room is a
    room number somebody misheard, and a ticket raised against it is a job
    nobody can find.
    """
    def _read():
        rows = (
            get_db().table("rooms")
            .select("*")
            .eq("property_id", property_id)
            .ilike("number", number)
            .limit(1)
            .execute()
            .data
            or []
        )
        return rows[0] if rows else None

    return await run_db(_read)


async def get_reservation(property_id: str, reservation_id: str) -> tuple[dict, int] | None:
    """The reservation and what is currently on its folio, as one answer."""
    def _read():
        db = get_db()
        rows = (
            db.table("reservations")
            .select("*")
            .eq("property_id", property_id)
            .eq("id", reservation_id)
            .limit(1)
            .execute()
            .data
        )
        if not rows:
            return None
        # The same shape the list returns, so the drawer and the row behind it
        # cannot disagree about one booking's balance.
        folio = next(
            (b for b in (db.rpc("folio_states", {"p_property": property_id}).execute().data or [])
             if b["reservation_id"] == reservation_id),
            None,
        )
        return rows[0], folio
    return await run_db(_read)


async def today_stats(property_id: str) -> dict:
    """
    The Today screen, computed server-side.

    Deliberately one round trip's worth of queries rather than a view: these
    numbers change on every check-in, and a materialised view that is correct
    only until the next one is worse than no view.

    "Today" is the property's, not the server's — a desk in Goa opening the
    screen at 00:15 is looking at the day that has just started.

    **The money comes from `pms.analytics`, not from here.** It used to be
    computed inline as `round((gross − discount) / nights) × units`, and
    `gross_paise` is already Σ(tariff × units) — so every multi-room booking
    reported its revenue once per room it contained, and ADR with it. One
    definition of room revenue now serves this screen, the Reports page and the
    Owner Copilot, which is the only arrangement in which they cannot disagree.
    """
    from app.services.pms import analytics  # local: analytics imports this module

    today = await clock.abusiness_date(property_id)
    today_s = today.isoformat()
    money = await analytics.day_money(property_id, today)

    def _read():
        db = get_db()

        stays = (
            db.table("reservations")
            .select("id,status,check_in,check_out,units,nights,gross_paise,"
                    "discount_paise,total_paise,source,created_at")
            .eq("property_id", property_id)
            .in_("status", LIVE_STATUSES)
            .execute()
            .data
            or []
        )
        room_types = (
            db.table("room_types")
            .select("total_units")
            .eq("property_id", property_id)
            .eq("is_active", True)
            .execute()
            .data
            or []
        )
        open_tasks = (
            db.table("tasks")
            .select("id", count="exact")
            .eq("property_id", property_id)
            .not_.in_("status", ["resolved", "cancelled"])
            .execute()
        )
        unread = (
            db.table("conversations")
            .select("id", count="exact")
            .eq("property_id", property_id)
            .gt("unread_count", 0)
            .execute()
        )
        return stays, room_types, (open_tasks.count or 0), (unread.count or 0)

    stays, room_types, open_tasks, unread = await run_db(_read)

    # Occupancy and money for tonight, from the one place that computes them.
    # Half-open, so a stay checking out this morning has already released the
    # room — the rule lives in `analytics`, applied identically for one day and
    # for ninety.
    rooms_sold = money["roomsSold"]
    rooms_available = money["roomsAvailable"] or sum(rt["total_units"] for rt in room_types)
    room_revenue = money["roomRevenuePaise"]

    # Same predicate the arrivals *list* is built from, so the number on Today
    # and the rows behind it cannot disagree.
    arrivals = [s for s in stays if is_arrival(s, today_s)]
    departures = [s for s in stays if is_departure(s, today_s)]

    cutoff = (today - timedelta(days=30)).isoformat()
    by_source: dict[str, dict] = {}
    for s in stays:
        if str(s["created_at"])[:10] < cutoff:
            continue
        cur = by_source.setdefault(s["source"], {"bookings": 0, "revenuePaise": 0})
        cur["bookings"] += 1
        cur["revenuePaise"] += s["total_paise"]

    return {
        "date": today,
        "occupancyPct": (rooms_sold / rooms_available * 100) if rooms_available else 0.0,
        # Needs yesterday's snapshot to be real. Reported as 0 rather than an
        # invented number: a delta nobody computed is worse than no delta.
        "occupancyDeltaPct": 0.0,
        "adrPaise": round(room_revenue / rooms_sold) if rooms_sold else 0,
        "revparPaise": round(room_revenue / rooms_available) if rooms_available else 0,
        "roomsSold": rooms_sold,
        "roomsAvailable": rooms_available,
        "arrivals": len(arrivals),
        "arrivalsPending": sum(1 for s in arrivals if s["status"] == "confirmed"),
        "departures": len(departures),
        "departuresPending": sum(1 for s in departures if s["status"] == "checked_in"),
        "inHouse": sum(s["units"] for s in stays if s["status"] == "checked_in"),
        "revenueTodayPaise": room_revenue,
        "openTasks": open_tasks,
        "unreadConversations": unread,
        "bySource": [
            {"source": k, **v}
            for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]["revenuePaise"])
        ],
    }
