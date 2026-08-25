"""
A hotel's own address.

    app.hostos.in/stay/anandi-house-udaipur            always, free, instant
    www.anandihouse.com                                theirs, once DNS points here

**Why a hotel wants the second one.** Direct booking is the whole argument for
this product: a stay sold on the hotel's own site costs no commission. A hotel
that has spent years putting `anandihouse.com` on its signage, its cards and
its Instagram bio cannot move that traffic to a path on somebody else's brand,
and telling them to is telling them to start again.

**Why it is a state machine and not a text box.** It used to be a text box.
`website.py`'s `_normalise` allow-list did not include `domain`, so the value
was accepted by the form, dropped by the server, and gone on the next reload —
a capability the product claimed and did not have. Pointing a domain at us is
not one act anyway; it is a claim, then two DNS records the owner has to add at
their registrar, then a check, and every one of those can fail on its own.

    none ──set──► pending ──verify──► verified ──►(records pulled)──► error
      ▲                                   │                             │
      └────────────────── clear ──────────┴─────────────────────────────┘

**The TXT record is the security control, and it is not optional.** Without it
this endpoint would take any string an authenticated hotelier typed and start
serving their booking page on it. Typing `booking.com` would be the least of
it: the real risk is that a domain's *owner* has never heard of us, and DNS
alone cannot tell "this hotel controls this name" from "this hotel typed this
name". Only something we generated, appearing in a zone only its owner can
edit, can.

**We check DNS over HTTPS rather than through the system resolver.** A resolver
inside a container answers from whatever the host was given, caches negatively,
and cannot be asked "what does the rest of the world see?" — which is the only
question that matters when the answer decides whether a stranger's browser will
reach this hotel. Cloudflare first, Google second, because a verification that
fails when one DNS provider has a bad minute is a support ticket.
"""
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException, status

from app.config import settings
from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# Where a hotel's domain has to point. A CNAME rather than an A record: the
# address behind it changes when the host moves and nobody has to tell nine
# hotels to edit their DNS.
CNAME_TARGET = "cname.vercel-dns.com"

# The name of the ownership record. Underscore-prefixed by convention — it is
# not a hostname anybody visits, and registrars sort it away from the records a
# person edits by hand.
#
# Still `_travbuddy` after the rename to HostOS, deliberately: this string is
# already in the zone file of every hotel that has verified a domain, and
# renaming it would un-verify all of them at once for a cosmetic gain nobody
# but us can see. A future rename has to write both names and accept either.
TXT_NAME = "_travbuddy"

DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)

# Hostnames nobody may claim, whatever their DNS says.
#
# `hostos.in` and its subdomains are ours: a hotel that claimed `app.hostos.in`
# would be claiming the PMS itself, and host resolution would then hand the
# staff app to a booking page. `travbuddy.in` stays on the list even though the
# PMS no longer lives there — the marketplace does, and a hotel that claimed a
# name on it would be claiming a site we run. The rest are the accidents —
# `localhost` and bare IPs are not domains and reach nothing.
RESERVED_SUFFIXES = ("hostos.in", "travbuddy.in", "vercel.app", "localhost")

_HOST = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


class DomainError(HTTPException):
    """A refusal already written for a hotel owner to read."""

    def __init__(self, message: str, code: int = status.HTTP_422_UNPROCESSABLE_ENTITY):
        super().__init__(status_code=code, detail=message)


@dataclass(frozen=True)
class DnsRecord:
    """One row of the table an owner copies into their registrar."""

    type: str
    name: str
    value: str
    found: bool


def normalise(raw: str) -> str:
    """
    What the owner typed, as a hostname.

    People paste `https://www.hotel.com/`, and `https://www.hotel.com/` is not
    a DNS name. Refusing it would be technically defensible and would read, to
    the person who runs the hotel, as the box being broken.
    """
    value = (raw or "").strip().lower()
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    value = value.split(":", 1)[0].strip(".")
    return value


def validate(domain: str) -> str:
    domain = normalise(domain)
    if not domain:
        raise DomainError("Enter the web address you want guests to use, like grandhotel.com.")
    if len(domain) > 253 or not _HOST.match(domain):
        raise DomainError(
            f"“{domain}” doesn't look like a web address. Enter it the way it appears "
            "in a browser, without http:// — for example grandhotel.com."
        )
    if any(domain == s or domain.endswith(f".{s}") for s in RESERVED_SUFFIXES):
        raise DomainError(
            "That address belongs to TravBuddy. Use a domain you own — your hotel "
            "already has a free TravBuddy address."
        )
    return domain


def _new_token() -> str:
    return f"travbuddy-verify={secrets.token_urlsafe(24)}"


# ── looking it up ────────────────────────────────────────────────────────────


async def _query(name: str, rtype: str) -> list[str]:
    """Every answer of one type for one name, as plain strings."""
    for endpoint in DOH_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=6.0) as client:
                res = await client.get(
                    endpoint,
                    params={"name": name, "type": rtype},
                    headers={"accept": "application/dns-json"},
                )
            if res.status_code != 200:
                continue
            answers = (res.json() or {}).get("Answer") or []
            out = []
            for a in answers:
                data = str(a.get("data") or "").strip()
                # TXT answers arrive quoted, and a long one arrives as several
                # quoted chunks that the zone file concatenated.
                if rtype == "TXT":
                    data = "".join(re.findall(r'"([^"]*)"', data)) or data
                out.append(data.strip('"').rstrip("."))
            return out
        except Exception:  # noqa: BLE001 — try the next resolver
            logger.info("DoH lookup failed at %s for %s/%s", endpoint, name, rtype)
    return []


async def records_for(domain: str, token: str | None) -> list[DnsRecord]:
    """
    The two records, and whether each one is visible from the public internet.

    Both are reported whatever their state, because the screen this feeds is
    the instructions *and* the progress: an owner who has added one of two
    needs to see which one is still missing, not a single red "not verified".
    """
    txt_name = f"{TXT_NAME}.{domain}"
    txt_values = await _query(txt_name, "TXT") if token else []
    cname_values = await _query(domain, "CNAME")

    # An apex domain cannot carry a CNAME, so registrars that support one at
    # all flatten it to A records pointing at the same place. Accepting either
    # is the difference between working for `grandhotel.com` and only working
    # for `www.grandhotel.com`.
    if not cname_values:
        cname_values = await _query(domain, "A")
    target_found = any(
        v == CNAME_TARGET or v.endswith(".vercel-dns.com") or v.startswith("76.76.21.")
        for v in cname_values
    )

    return [
        DnsRecord("TXT", txt_name, token or "", bool(token) and token in txt_values),
        DnsRecord("CNAME", domain, CNAME_TARGET, target_found),
    ]


# ── the row ──────────────────────────────────────────────────────────────────


def _row(property_id: str) -> dict:
    rows = (
        get_db().table("hotel_websites").select("*")
        .eq("property_id", property_id).limit(1).execute().data or []
    )
    if not rows:
        raise DomainError(
            "Set your website up first — then you can point your own address at it.",
            status.HTTP_404_NOT_FOUND,
        )
    return rows[0]


def _claimed_elsewhere(domain: str, property_id: str) -> bool:
    rows = (
        get_db().table("hotel_websites").select("property_id,domain")
        .neq("property_id", property_id).execute().data or []
    )
    return any((r.get("domain") or "").lower() == domain for r in rows)


def _write(property_id: str, patch: dict) -> dict:
    updated = (
        get_db().table("hotel_websites").update(patch)
        .eq("property_id", property_id).execute().data or []
    )
    return updated[0] if updated else _row(property_id)


async def claim(property_id: str, raw: str) -> dict:
    """
    Register the intent, and hand back the records to add.

    Nothing is served on this domain yet and the owner is not told it is. The
    token is minted once and kept across re-checks: regenerating it on every
    save would silently invalidate a record the owner had already added, and
    they would be looking at a value that no longer matches with no idea why.
    """
    domain = validate(raw)
    row = await run_db(_row, property_id)

    if await run_db(_claimed_elsewhere, domain, property_id):
        raise DomainError(
            f"“{domain}” is already connected to another hotel on TravBuddy. If that "
            "is yours too, disconnect it there first."
        )

    token = row.get("domain_token") if row.get("domain") == domain else None
    token = token or _new_token()

    return await run_db(_write, property_id, {
        "domain": domain,
        "domain_token": token,
        "domain_status": "pending",
        "domain_error": None,
        "domain_verified_at": None,
    })


async def release(property_id: str) -> dict:
    """
    Disconnect. The TravBuddy address keeps working, which is the point of
    having one — a hotel is never off the internet because of a DNS mistake.
    """
    return await run_db(_write, property_id, {
        "domain": None,
        "domain_token": None,
        "domain_status": "none",
        "domain_error": None,
        "domain_verified_at": None,
    })


async def verify(property_id: str) -> dict:
    """
    Look at the real DNS and write down what is actually there.

    Called by the owner pressing "Check now", so it answers the question they
    asked — including "still not there", which is a legitimate answer and not
    an error. A domain that *was* verified and no longer resolves goes to
    `error` rather than silently back to pending: it is live traffic that has
    stopped, and it should read as something that broke.
    """
    row = await run_db(_row, property_id)
    domain = (row.get("domain") or "").lower()
    if not domain:
        raise DomainError("There's no address to check yet.")

    records = await records_for(domain, row.get("domain_token"))
    missing = [r for r in records if not r.found]
    now = datetime.now(timezone.utc).isoformat()

    if missing:
        was_live = row.get("domain_status") == "verified"
        patch = {
            "domain_status": "error" if was_live else "pending",
            "domain_checked_at": now,
            "domain_error": (
                "Your hotel's address has stopped pointing at us."
                if was_live
                else None
            ),
        }
        if was_live:
            patch["domain_verified_at"] = None
        return await run_db(_write, property_id, patch)

    return await run_db(_write, property_id, {
        "domain_status": "verified",
        "domain_verified_at": now,
        "domain_checked_at": now,
        "domain_error": None,
    })


# ── the public direction: a host arrives, which hotel is it? ─────────────────


def _by_host(host: str) -> dict | None:
    rows = (
        get_db().table("hotel_websites")
        .select("property_id,status,domain,domain_status")
        .eq("domain_status", "verified").execute().data or []
    )
    match = next((r for r in rows if (r.get("domain") or "").lower() == host), None)
    if not match or match.get("status") != "published":
        return None
    prop = (
        get_db().table("properties").select("slug,name")
        .eq("id", match["property_id"]).limit(1).execute().data or []
    )
    return prop[0] if prop else None


async def hotel_for_host(raw_host: str) -> dict | None:
    """
    Which hotel a hostname belongs to, or nothing.

    **Only a verified domain on a published site resolves.** A `pending` one
    must not, or a hotel could serve its page on a domain it has not proved it
    owns, which is the whole thing the token exists to prevent. An unpublished
    one must not either — the owner has taken their site down and the custom
    address has to go down with it, not keep serving the last copy.
    """
    host = normalise(raw_host)
    if not host or any(host == s or host.endswith(f".{s}") for s in RESERVED_SUFFIXES):
        return None
    return await run_db(_by_host, host)


def project(row: dict) -> dict:
    """The shape the builder renders. No token unless there is a domain."""
    domain = row.get("domain")
    return {
        "domain": domain,
        "status": row.get("domain_status") or "none",
        "verifiedAt": row.get("domain_verified_at"),
        "checkedAt": row.get("domain_checked_at"),
        "error": row.get("domain_error"),
        "token": row.get("domain_token") if domain else None,
    }
