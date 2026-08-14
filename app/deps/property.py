"""
Resolve the caller's membership and property from the JWT.

    endpoint ──► require_user            (verifies the Supabase JWT)
             ──► require_member(property) (resolves membership server-side)

⚠ The partner and the member are ALWAYS derived from `auth.uid()`. No PMS
endpoint accepts a partner_id, and the property id in the path is checked
against the caller's membership *here*, before any handler runs — a guessed
property id 404s rather than reading someone else's arrivals list.

This mirrors `_require_partner` in api/v1/marketplace/partner_ops.py. The
difference is that a hotel has staff: the same partner has a receptionist who
may check guests in and a housekeeper who may not, so this returns a role and a
permission check rather than a bare partner id.

The permission table is duplicated from `has_partner_perm()` in
20260813_hotel_os_1_members.sql. That duplication is deliberate and worth
stating: the database copy is the authoritative one — it runs inside RLS whether
or not this service is in the path — and this copy exists so an endpoint can
return a clean 403 instead of letting a policy turn a forbidden write into an
empty result set that looks like a missing row.
"""
import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status

from app.db.client import get_db, run_db
from app.deps.auth import require_user

logger = logging.getLogger(__name__)

# Mirrors public.has_partner_perm(). Keep in sync with the migration; the DB
# copy wins if they ever disagree.
ROLE_PERMS: dict[str, set[str]] = {
    "owner": {"*"},
    "manager": {
        "rates", "billing", "members", "agents", "reports",
        "frontdesk", "folio", "housekeeping", "tasks", "messaging",
    },
    "accounts": {"billing", "payouts", "reports", "folio", "tasks"},
    "receptionist": {"frontdesk", "folio", "housekeeping", "tasks", "messaging"},
    "housekeeping": {"housekeeping", "tasks"},
    "maintenance": {"tasks"},
    "concierge": {"tasks", "messaging"},
    "restaurant": {"tasks"},
    "driver": {"tasks"},
}


@dataclass(frozen=True)
class Member:
    """The caller, resolved against one property."""

    profile_id: str
    member_id: str | None
    partner_id: str
    property_id: str
    role: str
    display_name: str | None
    # Per-member grants on top of the role. Can only ever add.
    extra_perms: frozenset[str]

    def can(self, perm: str) -> bool:
        if self.role == "owner":
            return True
        if perm in self.extra_perms:
            return True
        return perm in ROLE_PERMS.get(self.role, set())

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Your role ({self.role}) cannot {perm.replace('_', ' ')}.",
            )


def _display_name(db, profile_id: str) -> str | None:
    """
    The person's name, from their profile.

    Filled in because several things record *who did this* — a staff reply in
    the inbox, a task event, a folio line — and every one of them was writing a
    null. "Somebody changed this" is not an audit trail.
    """
    rows = (
        db.table("profiles").select("full_name")
        .eq("id", profile_id).limit(1).execute().data or []
    )
    return rows[0].get("full_name") if rows else None


def _load(property_id: str, profile_id: str) -> Member | None:
    db = get_db()

    prop = (
        db.table("properties")
        .select("id,partner_id,status")
        .eq("id", property_id)
        .limit(1)
        .execute()
    )
    if not prop.data:
        return None
    partner_id = prop.data[0]["partner_id"]

    member = (
        db.table("partner_members")
        .select("id,role,status,property_ids,permissions,profile_id")
        .eq("partner_id", partner_id)
        .eq("profile_id", profile_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )

    if member.data:
        row = member.data[0]
        scoped = row.get("property_ids") or []
        # An empty property_ids means the whole org; a non-empty one that omits
        # this property means this receptionist works a different front desk.
        if scoped and property_id not in scoped:
            return None
        perms = row.get("permissions") or {}
        return Member(
            profile_id=profile_id,
            member_id=row["id"],
            partner_id=partner_id,
            property_id=property_id,
            role=row["role"],
            display_name=_display_name(db, profile_id),
            extra_perms=frozenset(k for k, v in perms.items() if v is True),
        )

    # Fall back to ownership. The backfill in migration 1 makes this redundant
    # for existing partners, but a partner created between the backfill and its
    # first member row would otherwise be locked out of their own hotel.
    owner = (
        db.table("partners")
        .select("id")
        .eq("id", partner_id)
        .eq("owner_id", profile_id)
        .limit(1)
        .execute()
    )
    if owner.data:
        return Member(
            profile_id=profile_id,
            member_id=None,
            partner_id=partner_id,
            property_id=property_id,
            role="owner",
            display_name=_display_name(db, profile_id),
            extra_perms=frozenset(),
        )

    return None


async def resolve_member(property_id: str, user) -> Member:
    member = await run_db(_load, property_id, str(user.id))
    if member is None:
        # 404, not 403. Telling a stranger that a property id is real but not
        # theirs is an enumeration oracle over every hotel on the platform.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    return member


def require_member(perm: str | None = None):
    """
    Dependency factory: resolve membership for the `property_id` path parameter,
    optionally requiring a permission.

        @router.get("/properties/{property_id}/reservations")
        async def list_reservations(member = Depends(require_member("frontdesk"))):
            ...
    """

    async def _dep(property_id: str, user=Depends(require_user)) -> Member:
        member = await resolve_member(property_id, user)
        if perm:
            member.require(perm)
        return member

    return _dep
