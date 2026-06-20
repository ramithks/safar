"""
Shared trip-access checks.

A "trip" can be viewed by its creator or by anyone in the trip's group. This is
the single source of truth used by trip-level read endpoints (journey, bookings,
documents, reels) so group members see the squad's shared content instead of an
empty per-user view.
"""
from app.db.client import get_db


def can_access_trip(trip_id: str, user_id: str, db=None) -> bool:
    """True if the user owns the trip or is a member of its group."""
    db = db or get_db()

    owned = (
        db.table("trips").select("id").eq("id", trip_id).eq("user_id", user_id).limit(1).execute()
    )
    if (owned.data if owned else None):
        return True

    groups = db.table("trip_groups").select("id").eq("trip_id", trip_id).execute()
    group_ids = [g["id"] for g in (groups.data or [])]
    if not group_ids:
        return False

    member = (
        db.table("group_members")
        .select("id")
        .in_("group_id", group_ids)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return bool(member.data if member else None)
