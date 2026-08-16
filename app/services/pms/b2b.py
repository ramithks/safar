"""
The B2B agent boundary.

    /b2b  ──►  this module  ──►  availability.stay_options(audience='b2b')
                            ──►  booking.create_quote(audience='b2b')
                            ──►  b2b_commit_credit()
                            ──►  booking.create_reservation

**There is no pricing in this file, and there must never be.** Every number an
agency is shown comes from `stay_options` and `create_quote` — the same two
functions the front desk, the Sales agent and the public booking engine call.
A B2B rate is a rate plan, not a parallel pricing system: one ARI calendar, one
availability answer, different plans visible to different audiences. A second
inventory system for agencies is how a hotel oversells one channel to fill
another.

What this module *does* add is the three things an agency needs that a member of
staff does not:

**An identity that is not membership.** An agency signs in through the same
Supabase auth as a receptionist and is told apart by what it holds: a row in
`b2b_agents`, not a row in `partner_members`. `require_agency` resolves that
from the JWT, and the property id in a path is checked against the contract
exactly as `require_member` checks it against a membership.

**An audience.** `stay_options(audience='b2b')` is what stops an agency being
quoted the public rate — and, far more importantly, what stops the reverse:
`create_quote(audience='b2b')` refuses a rate plan id from any other audience,
so an agency cannot price itself onto a plan it was not offered by guessing a
uuid.

**A credit line.** An agency does not pay a token; it books against a limit and
settles on invoice. What it owes is derived — see `b2b_committed_paise()` in
migration 22 — and committing against it happens at *quote* time, under a row
lock, because that is the only moment at which the answer can be made to hold
for two simultaneous bookings.
"""
import logging
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contract:
    """One agency's agreement with one hotel group."""

    id: str
    partner_id: str
    code: str
    company_name: str
    contact_name: str | None
    city: str | None
    phone: str | None
    email: str | None
    commission_pct: float
    credit_limit_paise: int
    property_ids: tuple[str, ...]
    status: str

    def covers(self, property_id: str) -> bool:
        """
        Empty `property_ids` means the whole partner.

        The same convention as `partner_members.property_ids`, and reused rather
        than reinvented: a second way of expressing "which hotels may this
        principal see" is a second way to get it wrong.
        """
        return not self.property_ids or property_id in self.property_ids

    def require_active(self) -> None:
        if self.status != "active":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "This account is suspended. Please contact the hotel to settle "
                "your outstanding invoices.",
            )


@dataclass(frozen=True)
class Agency:
    """The caller, resolved from their JWT. One profile, one or more contracts."""

    profile_id: str
    full_name: str | None
    email: str | None
    contracts: tuple[Contract, ...]

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.contracts]


def _partner_and_status(property_id: str) -> dict | None:
    rows = (
        get_db().table("properties").select("id,partner_id,status")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


async def contract_for(agency: Agency, property_id: str) -> Contract:
    """
    The contract covering this hotel, or 404.

    404 and not 403, the same answer `require_member` gives a member for a
    property that is not theirs: telling an agency that a property id is real
    but uncontracted is an enumeration oracle over every hotel on the platform.

    A suspended *property* answers the same way. Whether a hotel is taking
    bookings is the hotel's business, and an agency does not need to be told the
    difference between "not yours" and "not trading".
    """
    prop = await run_db(_partner_and_status, property_id)
    if prop and prop.get("status") == "active":
        for contract in agency.contracts:
            if contract.partner_id == prop["partner_id"] and contract.covers(property_id):
                return contract
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found")


def _contract(row: dict) -> Contract:
    return Contract(
        id=row["id"],
        partner_id=row["partner_id"],
        code=row["code"],
        company_name=row["company_name"],
        contact_name=row.get("contact_name"),
        city=row.get("city"),
        phone=row.get("phone"),
        email=row.get("email"),
        commission_pct=float(row.get("commission_pct") or 0),
        credit_limit_paise=int(row.get("credit_limit_paise") or 0),
        property_ids=tuple(row.get("property_ids") or ()),
        status=row.get("status") or "active",
    )


def _load(profile_id: str) -> tuple[list[dict], dict | None]:
    db = get_db()
    rows = (
        db.table("b2b_agents").select("*")
        .eq("profile_id", profile_id)
        .order("company_name")
        .execute().data or []
    )
    profile = (
        db.table("profiles").select("id,full_name,email")
        .eq("id", profile_id).limit(1).execute().data or []
    )
    return rows, (profile[0] if profile else None)


async def resolve(profile_id: str) -> Agency:
    """
    Who this token belongs to, as an agency.

    A suspended contract is still returned. An agency that has stopped paying
    must still be able to see what it booked — hiding the list is not a
    sanction, it is an obstacle to the settlement that would lift the
    suspension. `require_active()` is what refuses the *writes*.
    """
    rows, profile = await run_db(_load, profile_id)
    if not rows:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is not registered as a travel agency.",
        )
    return Agency(
        profile_id=profile_id,
        full_name=(profile or {}).get("full_name"),
        email=(profile or {}).get("email"),
        contracts=tuple(_contract(r) for r in rows),
    )


# ── the credit line ──────────────────────────────────────────────────────────

def _committed(agency_id: str) -> int:
    return int(
        get_db().rpc("b2b_committed_paise", {"p_agent": agency_id}).execute().data or 0
    )


async def committed_paise(agency_id: str) -> int:
    """
    What this agency has on the line: unsettled bookings plus live quotes.

    Read through the same function the booking path commits against, for the
    same reason there is one analytics implementation — a figure shown to an
    agency that is computed differently from the one that refuses their booking
    is worse than showing no figure at all.
    """
    return await run_db(_committed, agency_id)


def _commit(agency_id: str, quote_id: str) -> dict:
    return (
        get_db().rpc("b2b_commit_credit", {"p_agent": agency_id, "p_quote": quote_id})
        .execute().data or {}
    )


# What each refusal from `b2b_commit_credit` means to the person reading it.
# 'credit' is handled separately because its sentence carries numbers.
_REFUSALS = {
    "suspended": (
        status.HTTP_403_FORBIDDEN,
        "This account is suspended. Please contact the hotel to settle your "
        "outstanding invoices.",
    ),
    "property": (
        status.HTTP_404_NOT_FOUND,
        "Property not found",
    ),
    "quote": (
        status.HTTP_409_CONFLICT,
        "That price is no longer available. Please search again.",
    ),
    "agent": (
        status.HTTP_403_FORBIDDEN,
        "This account is not registered as a travel agency.",
    ),
}


async def commit_credit(agency_id: str, quote_id: str) -> dict:
    """
    Take the agency's credit for one quote, or refuse with a sentence.

    Raises rather than returning a verdict, because every caller does the same
    thing with a "no" — release the hold and tell the agency why — and a
    boolean that three call sites each remember to check is a boolean that one
    of them eventually does not.
    """
    verdict = await run_db(_commit, agency_id, quote_id)
    if verdict.get("ok"):
        return verdict

    reason = verdict.get("reason") or "agent"
    if reason == "credit":
        left = max(
            int(verdict.get("creditLimitPaise") or 0)
            - int(verdict.get("committedPaise") or 0),
            0,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This booking is ₹{int(verdict.get('quoteTotalPaise') or 0) / 100:,.0f} "
            f"and you have ₹{left / 100:,.0f} left on your credit line. Settle an "
            f"outstanding invoice, or ask the hotel to raise your limit.",
        )
    code, detail = _REFUSALS[reason]
    raise HTTPException(code, detail)


# ── what the portal reads ────────────────────────────────────────────────────

def _properties(partner_ids: list[str]) -> list[dict]:
    return (
        get_db().table("properties")
        .select("id,partner_id,name,city,currency,check_in_time,check_out_time")
        .in_("partner_id", partner_ids)
        .eq("status", "active")
        .order("name")
        .execute().data or []
    )


async def sellable_properties(agency: Agency) -> list[dict]:
    """
    Every hotel this agency may sell, each carrying the contract that covers it.

    A suspended contract's hotels are included and the portal marks them: an
    agency that cannot book still needs to see which hotels it is suspended
    *from*, and a list that silently shortens reads as the hotel having left the
    platform.
    """
    by_partner: dict[str, Contract] = {c.partner_id: c for c in agency.contracts}
    rows = await run_db(_properties, list(by_partner))

    out: list[dict] = []
    for row in rows:
        contract = by_partner.get(row["partner_id"])
        if contract is None or not contract.covers(row["id"]):
            continue
        out.append({**row, "agency_id": contract.id})
    return out


def _bookings(agency_ids: list[str], limit: int) -> list[dict]:
    return (
        get_db().table("reservations").select("*")
        .in_("b2b_agent_id", agency_ids)
        .order("created_at", desc=True)
        .limit(limit)
        .execute().data or []
    )


async def list_bookings(agency: Agency, limit: int = 100) -> list[dict]:
    """
    Everything this agency has booked, newest first.

    Scoped by `b2b_agent_id` and by nothing the caller sent. There is no
    `agencyId` parameter on the route for the same reason no PMS route accepts a
    `partnerId`: an id in a request is a suggestion, and this one would be a
    suggestion about whose bookings to show.
    """
    return await run_db(_bookings, agency.ids, limit)
