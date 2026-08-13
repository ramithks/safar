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
from datetime import date, timedelta

from app.db.client import get_db, run_db
from app.deps.property import ROLE_PERMS

logger = logging.getLogger(__name__)

# Reservations in these states are not real occupancy and must not count toward
# anything. Kept as one tuple so no query forgets one of them.
LIVE_STATUSES = ("pending", "confirmed", "checked_in", "checked_out")


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


async def get_reservation(property_id: str, reservation_id: str) -> dict | None:
    def _read():
        rows = (
            get_db().table("reservations")
            .select("*")
            .eq("property_id", property_id)
            .eq("id", reservation_id)
            .limit(1)
            .execute()
            .data
        )
        return rows[0] if rows else None
    return await run_db(_read)


async def today_stats(property_id: str) -> dict:
    """
    The Today screen, computed server-side.

    Deliberately one round trip's worth of queries rather than a view: these
    numbers change on every check-in, and a materialised view that is correct
    only until the next one is worse than no view.
    """
    today = date.today()
    today_s = today.isoformat()

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

    # Occupying today: half-open, so a stay checking out this morning has
    # already released the room for tonight.
    occupying = [s for s in stays if s["check_in"] <= today_s < s["check_out"]]

    rooms_sold = sum(s["units"] for s in occupying)
    rooms_available = sum(rt["total_units"] for rt in room_types)

    room_revenue = sum(
        round((s["gross_paise"] - s["discount_paise"]) / s["nights"]) * s["units"]
        for s in occupying
        if s["nights"]
    )

    arrivals = [s for s in stays if s["check_in"] == today_s]
    departures = [s for s in stays if s["check_out"] == today_s]

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
