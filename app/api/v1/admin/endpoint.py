"""
Internal admin API — the single path for privileged marketplace operations.

    dashboard ──► POST /api/v1/admin/*
                      ├── verify passcode        (deps/admin.py)
                      ├── validate request       (pydantic + explicit checks)
                      ├── update database        (service role, server-side)
                      ├── write audit log        (admin_audit.record)
                      ├── emit event             (events.emit → subscribers)
                      └── return response

Everything that used to happen in the dashboard with a service-role key happens
here instead, so the dashboard can be a thin client holding only a passcode.

Endpoints deliberately do NOT send email or push anything themselves — they
emit, and `app.services.admin_subscribers` reacts. That keeps review logic about
reviewing.
"""
# NOTE: deliberately no `from __future__ import annotations`. With PEP 563 the
# body annotation becomes a string, and FastAPI (through slowapi's wrapper)
# fails to resolve it — every POST then reports the Pydantic model as a missing
# QUERY parameter instead of parsing the JSON body.
import logging
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.v1.admin.schemas import (
    AuditEntry, DueGroup, Ok, PartnerActionRequest, PartnerInviteRequest,
    PartnerLoginLink, PartnerLoginLinkRequest, QueueItem, ReviewRequest,
    SettlePayoutRequest, UnpublishRequest,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.admin import AdminIdentity, require_admin
from app.services import admin_audit
from app.services.events import emit

# Importing registers the @on(...) handlers.
from app.services import admin_subscribers  # noqa: F401

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

SITE = "https://travbuddy.in"
BRAND = "partner:partners(brand_name, slug, verified)"

TABLE = {"experience": "partner_listings", "group_trip": "group_trips"}
PUBLIC_PATH = {"experience": "experiences", "group_trip": "trips"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _confirm_url(token_hash: str, otp_type: str, next_path: str) -> str:
    """The website route that trades a one-time token for a session."""
    return (
        f"{SITE}/auth/confirm?token_hash={quote(token_hash, safe='')}"
        f"&type={quote(otp_type, safe='')}&next={quote(next_path, safe='')}"
    )


async def _fetch(kind: str, item_id: str) -> dict:
    """Load a reviewable row, or 404. Also gives us the label for the audit."""
    try:
        res = await run_db(
            lambda: get_db()
            .table(TABLE[kind])
            .select("*")
            .eq("id", item_id)
            .maybe_single()
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] fetch failed: %s", exc)
        raise HTTPException(502, "Could not read that record.") from exc

    if not res or not res.data:
        raise HTTPException(404, "Not found.")
    return res.data


# ── review queue ────────────────────────────────────────────────────────────

def _listing_flags(r: dict) -> list[str]:
    f: list[str] = []
    c = r.get("content") or {}
    photos = len(r.get("photos") or [])
    if photos < 3:
        f.append("Only 1 photo" if photos == 1 else f"Only {photos} photos")
    if not r.get("price_from"):
        f.append("No price")
    if len(r.get("description") or "") < 120:
        f.append("Thin description")
    if not c.get("cancellation_policy"):
        f.append("No cancellation policy")
    if c.get("difficulty") in {"hard", "moderate"} and not c.get("safety"):
        f.append("No safety notes on a non-easy activity")
    if not r.get("partner_id"):
        f.append("No partner attached")
    return f


def _trip_flags(r: dict) -> list[str]:
    f: list[str] = []
    c = r.get("content") or {}
    if not r.get("hero_image"):
        f.append("No cover photo")
    if not r.get("price_from"):
        f.append("No price")
    dep = r.get("departure_date")
    if not dep:
        f.append("No departure date")
    elif str(dep) < datetime.now(timezone.utc).date().isoformat():
        f.append("Departure date is in the past")
    if not r.get("seats_total"):
        f.append("No seat count")
    if not (c.get("days") or []):
        f.append("No day-by-day plan")
    if not (c.get("inclusions") or []):
        f.append("No inclusions")
    if len(r.get("description") or "") < 120:
        f.append("Thin description")
    return f


@router.get("/queue", response_model=list[QueueItem])
@limiter.limit("120/minute")
def queue(request: Request, _admin: AdminIdentity = Depends(require_admin)) -> list[QueueItem]:
    """Everything waiting on a human decision. Oldest first — FIFO is fairer."""
    db = get_db()
    listings = (
        db.table("partner_listings").select(f"*, {BRAND}").eq("status", "pending").execute().data or []
    )
    trips = (
        db.table("group_trips").select(f"*, {BRAND}").eq("status", "pending").execute().data or []
    )

    items: list[QueueItem] = []
    for r in listings:
        items.append(QueueItem(
            id=r["id"], kind="experience", slug=r["slug"], title=r["title"],
            brand=(r.get("partner") or {}).get("brand_name") or "Unknown partner",
            partner_id=r.get("partner_id"),
            destination=r.get("destination") or r.get("city"),
            price=r.get("price_from"),
            photo=(r.get("photos") or [None])[0],
            submitted=r["updated_at"], flags=_listing_flags(r),
            public_url=f"{SITE}/experiences/{r['slug']}",
        ))
    for r in trips:
        items.append(QueueItem(
            id=r["id"], kind="group_trip", slug=r["slug"], title=r["title"],
            brand=(r.get("partner") or {}).get("brand_name") or "TravBuddy",
            partner_id=r.get("partner_id"),
            destination=r.get("destination"),
            price=r.get("price_from"),
            photo=r.get("hero_image"),
            submitted=r["updated_at"], flags=_trip_flags(r),
            public_url=f"{SITE}/trips/{r['slug']}",
        ))

    return sorted(items, key=lambda i: i.submitted)


@router.post("/review", response_model=Ok)
@limiter.limit("120/minute")
async def review(
    request: Request,
    body: ReviewRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> Ok:
    """Approve or reject a pending listing or trip."""
    if body.action == "reject" and len((body.reason or "").strip()) < 5:
        raise HTTPException(422, "Give the partner a reason — they see this and act on it.")

    row = await _fetch(body.kind, body.id)
    if row.get("status") != "pending":
        raise HTTPException(409, f"That item is '{row.get('status')}', not pending.")

    table = TABLE[body.kind]
    if body.action == "approve":
        patch = {"status": "published", "approved_at": _now(), "rejection_reason": None}
        patch |= {"published_at": _now()} if body.kind == "experience" else {"is_active": True}
    else:
        patch = {"status": "rejected", "rejection_reason": body.reason.strip()[:500]}

    try:
        await run_db(
            lambda: get_db().table(table).update(patch)
            .eq("id", body.id).eq("status", "pending").execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] review update failed: %s", exc)
        raise HTTPException(502, "Could not update that record.") from exc

    event = "listing.approved" if body.action == "approve" else "listing.rejected"
    payload = {
        "actor": admin.actor,
        "kind": body.kind,
        "entity_id": body.id,
        "entity_label": row.get("title"),
        "partner_id": row.get("partner_id"),
        "reason": body.reason,
        "public_url": f"{SITE}/{PUBLIC_PATH[body.kind]}/{row.get('slug')}",
    }

    await admin_audit.record(
        actor=admin.actor, action=event,
        entity_type=table, entity_id=body.id, entity_label=row.get("title"),
        reason=body.reason, ip=admin.ip,
        metadata={"kind": body.kind, "slug": row.get("slug")},
    )
    await emit(event, payload)

    return Ok(message="Approved and live." if body.action == "approve" else "Sent back to the partner.")


@router.post("/unpublish", response_model=Ok)
@limiter.limit("60/minute")
async def unpublish(
    request: Request,
    body: UnpublishRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> Ok:
    """Take something live back down to a draft."""
    row = await _fetch(body.kind, body.id)
    table = TABLE[body.kind]
    patch = {"status": "draft"} | ({} if body.kind == "experience" else {"is_active": False})

    try:
        await run_db(lambda: get_db().table(table).update(patch).eq("id", body.id).execute())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, "Could not unpublish that record.") from exc

    await admin_audit.record(
        actor=admin.actor, action="listing.unpublished",
        entity_type=table, entity_id=body.id, entity_label=row.get("title"),
        reason=body.reason, ip=admin.ip, metadata={"kind": body.kind},
    )
    await emit("listing.unpublished", {
        "actor": admin.actor, "entity_id": body.id, "entity_label": row.get("title"),
        "partner_id": row.get("partner_id"), "reason": body.reason,
    })
    return Ok(message="Taken down.")


# ── partners ────────────────────────────────────────────────────────────────

@router.post("/partners", response_model=Ok)
@limiter.limit("60/minute")
async def partner_action(
    request: Request,
    body: PartnerActionRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> Ok:
    res = await run_db(
        lambda: get_db().table("partners").select("*").eq("id", body.id).maybe_single().execute()
    )
    if not res or not res.data:
        raise HTTPException(404, "Partner not found.")
    partner = res.data
    brand = partner.get("brand_name")

    if body.action in {"verify", "unverify"}:
        verified = body.action == "verify"
        patch = {"verified": verified, "kyc_status": "verified" if verified else "pending"}
        event, message = "partner.verified" if verified else "partner.unverified", "Updated."

    elif body.action in {"trust_publishing", "require_review"}:
        trusted = body.action == "trust_publishing"
        patch = {"auto_publish": trusted}
        event = "partner.publishing_trusted" if trusted else "partner.review_required"
        message = (
            "Their submissions now go live instantly."
            if trusted
            else "Their submissions go to the review queue again."
        )

    elif body.action in {"suspend", "reactivate"}:
        status_value = "suspended" if body.action == "suspend" else "active"
        patch = {"status": status_value}
        event = f"partner.{body.action}d"
        message = "Suspended — their listings are hidden." if body.action == "suspend" else "Reactivated."

    else:  # grant_plan — comp a month of the ₹499 stay plan
        current = partner.get("stay_plan_expires_at")
        base = datetime.now(timezone.utc)
        if current:
            try:
                parsed = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
                base = max(base, parsed)
            except ValueError:
                pass
        month = base.month - 1 + body.months
        expires = base.replace(year=base.year + month // 12, month=month % 12 + 1)
        patch = {"stay_plan_status": "active", "stay_plan_expires_at": expires.isoformat()}
        event, message = "partner.plan_granted", f"{body.months} month(s) added."

    try:
        await run_db(lambda: get_db().table("partners").update(patch).eq("id", body.id).execute())
        if body.action == "grant_plan":
            # Bring back anything the nightly expiry sweep paused.
            await run_db(
                lambda: get_db().table("partner_listings")
                .update({"status": "published", "published_at": _now()})
                .eq("partner_id", body.id).eq("kind", "hotel").eq("status", "expired").execute()
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] partner action failed: %s", exc)
        raise HTTPException(502, "Could not update that partner.") from exc

    await admin_audit.record(
        actor=admin.actor, action=event,
        entity_type="partner", entity_id=body.id, entity_label=brand,
        reason=body.reason, ip=admin.ip, metadata={"patch": patch},
    )
    await emit(event, {
        "actor": admin.actor, "partner_id": body.id, "entity_label": brand, "patch": patch,
    })
    return Ok(message=message)


@router.post("/partners/{partner_id}/login-link", response_model=PartnerLoginLink)
@limiter.limit("10/hour")
async def partner_login_link(
    request: Request,
    partner_id: str,
    body: PartnerLoginLinkRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> PartnerLoginLink:
    """
    Mint a one-time sign-in link into a partner's OWN dashboard.

    Why this exists: onboarding a partner means writing their listings the way
    they'd write them — with the AI importer, the photo uploader and the plan
    rules that only exist on the partner side. Rebuilding all of that behind the
    admin passcode would be a second, worse copy of a product that already works.

    What it really is: an account-takeover primitive, so it is treated like one.
    A reason is mandatory, the link is single-use and short-lived, only partners
    with an actual account can be targeted, and every issue lands in the audit
    trail with the operator's name and IP. Anything done after the link is
    redeemed is recorded as the PARTNER, not as us — that's the honest cost of
    reusing their dashboard, and the audit row here is the only bridge back.
    """
    res = await run_db(
        lambda: get_db().table("partners")
        .select("id, brand_name, owner_id, status")
        .eq("id", partner_id).maybe_single().execute()
    )
    if not res or not res.data:
        raise HTTPException(404, "Partner not found.")
    partner = res.data
    brand = partner.get("brand_name")

    owner_id = partner.get("owner_id")
    if not owner_id:
        raise HTTPException(409, "That partner has no account yet — there's nothing to sign in to.")

    try:
        account = await run_db(lambda: get_db().auth.admin.get_user_by_id(owner_id))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] could not load partner account: %s", exc)
        raise HTTPException(502, "Could not read that partner's account.") from exc

    email = getattr(getattr(account, "user", None), "email", None)
    if not email:
        raise HTTPException(
            409,
            "That partner signed up without an email address, so there's no magic link to mint.",
        )

    try:
        link = await run_db(
            lambda: get_db().auth.admin.generate_link({
                "type": "magiclink",
                "email": email,
                # Unused by our flow — the website redeems `hashed_token` at
                # /auth/confirm — but GoTrue stamps it onto the link it builds.
                "options": {"redirect_to": f"{SITE}/auth/confirm"},
            })
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] generate_link failed: %s", exc)
        raise HTTPException(502, "Could not mint a sign-in link for that partner.") from exc

    token_hash = link.properties.hashed_token

    # The token itself never touches the audit row — the row says a link was
    # issued and why, not how to use it.
    await admin_audit.record(
        actor=admin.actor, action="partner.login_link_issued",
        entity_type="partner", entity_id=partner_id, entity_label=brand,
        reason=body.reason, ip=admin.ip, metadata={"email": email},
    )
    await emit("partner.login_link_issued", {
        "actor": admin.actor, "partner_id": partner_id, "entity_label": brand, "email": email,
    })

    return PartnerLoginLink(
        partner_id=partner_id,
        brand=brand,
        email=email,
        token_hash=token_hash,
        type="magiclink",
        next="/partner/dashboard",
        url=_confirm_url(token_hash, "magiclink", "/partner/dashboard"),
    )


@router.post("/partners/invite", response_model=PartnerLoginLink)
@limiter.limit("10/hour")
async def partner_invite(
    request: Request,
    body: PartnerInviteRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> PartnerLoginLink:
    """
    Open a dashboard for a business that hasn't signed up yet.

    A partner row can only be created by the account that owns it, so onboarding
    someone who has never touched the site needs an account first. This creates
    one for their email (no mail is sent — `generate_link` only mints) and hands
    back a link that lands on /partner/start, where their brand details can be
    filled in. If the email already has an account we fall back to a plain
    sign-in link rather than refusing.
    """
    email = str(body.email).strip().lower()

    created = True
    try:
        link = await run_db(
            lambda: get_db().auth.admin.generate_link({
                "type": "invite",
                "email": email,
                "options": {"redirect_to": f"{SITE}/auth/confirm"},
            })
        )
        otp_type = "invite"
    except Exception as invite_exc:  # noqa: BLE001
        # Overwhelmingly "email address already exists" — the useful case, not a
        # failure. A genuine outage surfaces on the retry below.
        logger.info("[admin] invite for %s fell back to magiclink: %s", email, invite_exc)
        created = False
        try:
            link = await run_db(
                lambda: get_db().auth.admin.generate_link({
                    "type": "magiclink",
                    "email": email,
                    "options": {"redirect_to": f"{SITE}/auth/confirm"},
                })
            )
            otp_type = "magiclink"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[admin] generate_link failed for %s: %s", email, exc)
            raise HTTPException(502, "Could not mint a sign-in link for that email.") from exc

    user_id = getattr(getattr(link, "user", None), "id", None)

    # They may already have a partner profile — send them to the dashboard if so,
    # and to /partner/start if not.
    partner_id: str | None = None
    brand: str | None = None
    if user_id:
        res = await run_db(
            lambda: get_db().table("partners").select("id, brand_name")
            .eq("owner_id", user_id).maybe_single().execute()
        )
        if res and res.data:
            partner_id = res.data.get("id")
            brand = res.data.get("brand_name")

    next_path = "/partner/dashboard" if partner_id else "/partner/start"
    token_hash = link.properties.hashed_token

    await admin_audit.record(
        actor=admin.actor, action="partner.login_link_issued",
        entity_type="partner", entity_id=partner_id, entity_label=brand or email,
        reason=body.reason, ip=admin.ip,
        metadata={"email": email, "created_account": created},
    )
    await emit("partner.login_link_issued", {
        "actor": admin.actor, "partner_id": partner_id, "entity_label": brand or email,
        "email": email, "created_account": created,
    })

    return PartnerLoginLink(
        partner_id=partner_id,
        brand=brand,
        email=email,
        token_hash=token_hash,
        type=otp_type,
        next=next_path,
        url=_confirm_url(token_hash, otp_type, next_path),
        created_account=created,
    )


# ── payouts ─────────────────────────────────────────────────────────────────

@router.get("/payouts/due", response_model=list[DueGroup])
@limiter.limit("60/minute")
def payouts_due(request: Request, _admin: AdminIdentity = Depends(require_admin)) -> list[DueGroup]:
    """
    Matured money, grouped by partner.

    `mature_partner_payouts()` (pg_cron) moves a booking to `due` 48h after
    travel; this is what turns those into a payable batch.
    """
    rows = (
        get_db()
        .table("bookings")
        .select("*, partner:partners(brand_name, payout_method)")
        .eq("payout_status", "due")
        .not_.is_("partner_id", "null")
        .order("created_at")
        .execute()
        .data
        or []
    )

    groups: dict[str, DueGroup] = {}
    for r in rows:
        pid = r["partner_id"]
        if pid not in groups:
            method = (r.get("partner") or {}).get("payout_method") or {}
            if method.get("type") == "upi":
                label = f"UPI {method.get('upi', '')}"
            elif method.get("type") == "bank":
                label = f"Bank ••{method.get('last4', '')}"
            else:
                label = "No payout method"
            groups[pid] = DueGroup(
                partner_id=pid,
                brand=(r.get("partner") or {}).get("brand_name") or "Unknown partner",
                payout_method=label, booking_ids=[], refs=[],
                gross_paise=0, commission_paise=0, net_paise=0,
            )
        g = groups[pid]
        g.booking_ids.append(r["id"])
        g.refs.append(r["ref"])
        g.gross_paise += r["amount_paise"]
        g.commission_paise += r["commission_paise"]
        g.net_paise += r["payout_paise"]

    return sorted(groups.values(), key=lambda g: g.net_paise, reverse=True)


@router.post("/payouts/settle", response_model=Ok)
@limiter.limit("30/minute")
async def settle_payout(
    request: Request,
    body: SettlePayoutRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> Ok:
    """
    Record that a partner has been paid.

    Amounts are recomputed here from the bookings themselves — the client sends
    ids, never sums, so a tampered request can't inflate a payout. Bookings are
    flipped FIRST; if that fails we stop before writing the ledger, so the worst
    case is money still marked due rather than a payout record for money that
    never left.
    """
    res = await run_db(
        lambda: get_db().table("bookings")
        .select("id, ref, partner_id, amount_paise, commission_paise, payout_paise, payout_status")
        .in_("id", body.booking_ids)
        .execute()
    )
    rows = (res.data if res else None) or []

    if not rows:
        raise HTTPException(404, "No matching bookings.")

    wrong_partner = [r["ref"] for r in rows if r["partner_id"] != body.partner_id]
    if wrong_partner:
        raise HTTPException(422, f"Bookings belong to another partner: {', '.join(wrong_partner[:5])}")

    not_due = [r["ref"] for r in rows if r["payout_status"] != "due"]
    if not_due:
        raise HTTPException(409, f"Already settled or not yet due: {', '.join(not_due[:5])}")

    gross = sum(r["amount_paise"] for r in rows)
    commission = sum(r["commission_paise"] for r in rows)
    net = sum(r["payout_paise"] for r in rows)
    ids = [r["id"] for r in rows]

    try:
        await run_db(
            lambda: get_db().table("bookings").update({"payout_status": "paid"})
            .in_("id", ids).eq("payout_status", "due").execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] payout booking update failed: %s", exc)
        raise HTTPException(502, "Could not mark those bookings paid — nothing was recorded.") from exc

    try:
        await run_db(lambda: get_db().table("partner_payouts").insert({
            "partner_id": body.partner_id, "booking_ids": ids,
            "gross_paise": gross, "commission_paise": commission, "net_paise": net,
            "status": "paid", "utr": body.utr.strip(), "paid_at": _now(),
            "note": body.note,
        }).execute())
    except Exception as exc:  # noqa: BLE001
        # Bookings are already flipped. Loud log + audit row so the hole is visible.
        logger.error("[admin] payout ledger insert FAILED after marking bookings paid: %s", exc)
        await admin_audit.record(
            actor=admin.actor, action="payout.ledger_failed",
            entity_type="partner", entity_id=body.partner_id,
            reason=str(exc)[:400], ip=admin.ip,
            metadata={"booking_ids": ids, "net_paise": net, "utr": body.utr},
        )
        raise HTTPException(502, "Bookings were marked paid but the ledger row failed — check the audit log.") from exc

    await admin_audit.record(
        actor=admin.actor, action="payout.settled",
        entity_type="partner", entity_id=body.partner_id,
        entity_label=None, reason=body.note, ip=admin.ip,
        metadata={
            "booking_ids": ids, "refs": [r["ref"] for r in rows],
            "gross_paise": gross, "commission_paise": commission,
            "net_paise": net, "utr": body.utr,
        },
    )
    await emit("payout.settled", {
        "actor": admin.actor, "partner_id": body.partner_id,
        "net_paise": net, "utr": body.utr, "count": len(ids),
    })

    return Ok(message=f"Recorded ₹{net // 100:,} paid to the partner.")


# ── audit ───────────────────────────────────────────────────────────────────

@router.get("/audit", response_model=list[AuditEntry])
@limiter.limit("60/minute")
def audit(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
    action: str | None = Query(None, max_length=60),
    _admin: AdminIdentity = Depends(require_admin),
) -> list[AuditEntry]:
    """Who did what, newest first."""
    return [AuditEntry(**row) for row in admin_audit.recent(limit=limit, action=action)]
