"""
The operator's side of the money: hotels, credits and coupons.

Three things a founder needs and cannot get from the database console without
learning the schema:

    GET  /admin/hotels                  who is on what plan, and what they have left
    GET  /admin/hotels/{id}/ledger      why a balance is what it is
    POST /admin/credits/adjust          add or take away, with a reason
    …/admin/plan-coupons                create, edit, disable, see redemptions

**Nothing here mutates a balance directly.** Every credit movement goes through
`grant_credits`, the same append-only path a subscription uses, so a manual
adjustment is a ledger row with an actor on it rather than an UPDATE nobody can
account for later. Taking credits away is a negative grant through the same
door, which is why `adjust` is one endpoint and not two.

**Every write is audited.** `admin_audit.record` gets the actor, the reason and
the outcome — including whether an idempotency key made the call a no-op, which
is the difference between "we granted twice" and "somebody double-clicked".
"""
# NOTE: no `from __future__ import annotations` — see api/v1/admin/endpoint.py.
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.admin import AdminIdentity, require_admin
from app.services import admin_audit
from app.services.billing import service as billing_service
from app.api.v1.admin.billing_schemas import (
    AdminCouponOut,
    AdminHotelOut,
    AuditEntryOut,
    CouponUpsertRequest,
    CreditAdjustRequest,
    LedgerEntryOut,
    Ok,
    RedemptionOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

_CODE_RE = re.compile(r"^[A-Z0-9]{3,20}$")


# ── hotels ───────────────────────────────────────────────────────────────────


def _hotels(search: str | None, limit: int) -> list[dict]:
    db = get_db()
    q = db.table("properties").select(
        "id,name,slug,city,status,partner_id,billing_account_id,created_at"
    ).order("created_at", desc=True).limit(limit)
    if search:
        # `or` across name and slug. Escaped by PostgREST; the pattern itself is
        # bounded by the route's max_length so a wildcard-only search cannot
        # ask for the whole table by accident.
        q = q.or_(f"name.ilike.%{search}%,slug.ilike.%{search}%")
    rows = q.execute().data or []
    if not rows:
        return []

    ids = [r["id"] for r in rows]
    accounts = [r["billing_account_id"] for r in rows if r.get("billing_account_id")]

    wallets = {}
    if accounts:
        for w in (db.table("credit_wallets").select("billing_account_id,balance_credits")
                  .in_("billing_account_id", accounts).execute().data or []):
            wallets[w["billing_account_id"]] = int(w.get("balance_credits") or 0)

    subs = {}
    for srow in (db.table("subscriptions")
                 .select("property_id,status,current_period_end,razorpay_subscription_id,"
                         "coupon_code,started_at,razorpay_payment_id,grace_until,"
                         "cancel_at_period_end")
                 .in_("property_id", ids).execute().data or []):
        if srow.get("property_id"):
            subs[srow["property_id"]] = srow

    # When money last arrived. Read from the ledger rather than from the
    # subscription row, because the row holds only the *latest* payment id and
    # an operator asking "when did they last pay" means the date.
    last_paid: dict[str, str] = {}
    if accounts:
        for entry in (db.table("credit_ledger")
                      .select("billing_account_id,created_at,reference_type")
                      .in_("billing_account_id", accounts)
                      .eq("reference_type", "subscription")
                      .order("created_at", desc=True).limit(500).execute().data or []):
            last_paid.setdefault(entry["billing_account_id"], entry["created_at"])

    # Usage this month, by conversation type. One query for every hotel on the
    # page rather than one per hotel: this list is the screen an operator keeps
    # open, and N+1 on it is a slow page every time.
    since = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    usage: dict[str, dict] = {}
    for e in (db.table("usage_events")
              .select("property_id,usage_type,credits_charged")
              .in_("property_id", ids).gte("created_at", since)
              .eq("status", "charged").execute().data or []):
        bucket = usage.setdefault(e["property_id"], {"website": 0, "instagram": 0,
                                                     "whatsapp": 0, "credits": 0})
        bucket["credits"] += int(e.get("credits_charged") or 0)
        for channel in ("website", "instagram", "whatsapp"):
            if e["usage_type"] == f"ai.conversation.{channel}":
                bucket[channel] += 1

    out = []
    for r in rows:
        sub = subs.get(r["id"]) or {}
        u = usage.get(r["id"]) or {}
        out.append({
            "propertyId": r["id"], "name": r["name"], "slug": r["slug"],
            "city": r.get("city"), "propertyStatus": r["status"],
            "partnerId": r["partner_id"],
            "billingAccountId": r.get("billing_account_id"),
            "planStatus": sub.get("status") or "none",
            "hasMandate": bool(sub.get("razorpay_subscription_id")),
            "couponCode": sub.get("coupon_code"),
            "currentPeriodEnd": sub.get("current_period_end"),
            "startedAt": sub.get("started_at"),
            "lastPaidAt": last_paid.get(r.get("billing_account_id")),
            "graceUntil": sub.get("grace_until"),
            "cancelAtPeriodEnd": bool(sub.get("cancel_at_period_end")),
            "credits": wallets.get(r.get("billing_account_id"), 0),
            "websiteConversations": u.get("website", 0),
            "instagramConversations": u.get("instagram", 0),
            "whatsappConversations": u.get("whatsapp", 0),
            "creditsUsedThisMonth": u.get("credits", 0),
            "createdAt": r["created_at"],
        })
    return out


@router.get("/hotels", response_model=list[AdminHotelOut])
@limiter.limit("60/minute")
async def list_hotels(
    request: Request,
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=200),
    admin: AdminIdentity = Depends(require_admin),
):
    """Every hotel, with its plan, its balance and what it used this month."""
    return await run_db(_hotels, q.strip() or None, limit)


def _ledger(billing_account_id: str, limit: int) -> list[dict]:
    db = get_db()
    rows = (
        db.table("credit_ledger")
        .select("id,entry_type,credits,balance_after,reference_type,reference_id,"
                "actor_type,actor_id,metadata,created_at,usage_event_id,property_id")
        .eq("billing_account_id", billing_account_id)
        .order("created_at", desc=True).limit(limit).execute().data or []
    )
    # What the debits were *for*. The ledger points at a usage event; the usage
    # event knows the channel. Without this join an operator reading a hotel's
    # history sees "-8" forty times and cannot tell Instagram from the website,
    # which is the first question they will have.
    event_ids = [r["usage_event_id"] for r in rows if r.get("usage_event_id")]
    events = {}
    if event_ids:
        for e in (db.table("usage_events").select("id,usage_type,conversation_id")
                  .in_("id", event_ids).execute().data or []):
            events[e["id"]] = e

    out = []
    for r in rows:
        event = events.get(r.get("usage_event_id")) or {}
        meta = r.get("metadata") or {}
        out.append({
            "id": r["id"],
            "entryType": r["entry_type"],
            "credits": int(r["credits"]),
            "balanceAfter": int(r["balance_after"]),
            "usageType": event.get("usage_type"),
            "conversationId": event.get("conversation_id"),
            "referenceType": r.get("reference_type"),
            "referenceId": r.get("reference_id"),
            "actorType": r.get("actor_type"),
            "actor": meta.get("by") or r.get("actor_id"),
            "reason": meta.get("reason") or meta.get("note"),
            "createdAt": r["created_at"],
        })
    return out


@router.get("/hotels/{billing_account_id}/ledger", response_model=list[LedgerEntryOut])
@limiter.limit("60/minute")
async def hotel_ledger(
    request: Request,
    billing_account_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    admin: AdminIdentity = Depends(require_admin),
):
    """Why the balance is what it is, newest first, with what each debit bought."""
    return await run_db(_ledger, billing_account_id, limit)


# ── credits ──────────────────────────────────────────────────────────────────


def _audit_for(entity_id: str, limit: int) -> list[dict]:
    rows = (
        get_db().table("admin_audit_log")
        .select("id,actor,action,reason,metadata,created_at")
        .eq("entity_id", entity_id)
        .order("created_at", desc=True).limit(limit).execute().data or []
    )
    return [{
        "id": r["id"], "actor": r["actor"], "action": r["action"],
        "reason": r.get("reason"),
        "credits": (r.get("metadata") or {}).get("credits"),
        "createdAt": r["created_at"],
    } for r in rows]


@router.get("/hotels/{billing_account_id}/audit", response_model=list[AuditEntryOut])
@limiter.limit("60/minute")
async def hotel_audit(
    request: Request,
    billing_account_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    admin: AdminIdentity = Depends(require_admin),
):
    """
    Who touched this hotel's money, and why.

    Separate from the ledger on purpose: the ledger is every movement including
    the automatic ones, and this is only the ones a person chose to make. When
    a balance looks wrong the question is almost always "did somebody do this",
    and forty subscription grants in between make that hard to see.
    """
    return await run_db(_audit_for, billing_account_id, limit)


@router.post("/credits/adjust", response_model=Ok)
@limiter.limit("30/minute")
async def adjust_credits(
    request: Request,
    body: CreditAdjustRequest,
    admin: AdminIdentity = Depends(require_admin),
):
    """
    Add or remove credits. One endpoint, because it is one act with a sign.

    **A reason is required either way**, and it is what appears in the hotel's
    own ledger. "Launch partner bonus" and "duplicate grant reversed" are both
    things somebody will need to read back in six months; a blank is not.

    Removing more than the hotel has is refused rather than clamped. A negative
    balance is a real accounting position in some products and is not one here
    — and silently taking 400 when 500 was asked for is a number the operator
    would never notice was wrong.
    """
    if body.credits == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Enter how many credits to add or remove.")
    if abs(body.credits) > 1_000_000:
        # Checked here rather than as a pydantic bound so the operator reads a
        # sentence instead of `[{"type":"greater_than_equal","loc":…}]`.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That's more than a million credits. Enter a smaller number.",
        )

    state = await billing_service.wallet_state(body.billingAccountId)
    balance = int((state or {}).get("balance") or 0)
    if body.credits < 0 and balance + body.credits < 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"This hotel has {balance:,} credits. You can't remove {abs(body.credits):,}.",
        )

    # Two RPCs, one endpoint. `grant_credits` opens a bucket and `debit_credits`
    # walks them down; both are idempotent on the same key and both append to
    # the ledger with an actor. Which one runs is decided by the sign here and
    # nowhere else, so a caller cannot pick.
    if body.credits > 0:
        result = await billing_service.grant(
            billing_account_id=body.billingAccountId,
            kind=body.kind,
            credits=body.credits,
            idempotency_key=f"admin:{body.reference}",
            source_type="admin",
            actor_type="admin",
            metadata={"reason": body.reason, "by": admin.actor},
        )
    else:
        result = await billing_service.debit(
            billing_account_id=body.billingAccountId,
            credits=abs(body.credits),
            idempotency_key=f"admin:{body.reference}",
            reason=body.reason,
            actor_type="admin",
            metadata={"by": admin.actor},
        )
    audited = await admin_audit.record(
        actor=admin.actor,
        action="credits.adjust",
        entity_type="billing_account",
        entity_id=body.billingAccountId,
        reason=body.reason,
        ip=admin.ip,
        metadata={
            "credits": body.credits, "kind": body.kind,
            "reference": body.reference,
            "duplicate": bool(result.get("duplicate")),
            "balanceBefore": balance,
            "balanceAfter": result.get("balance_after"),
        },
    )
    # Said out loud, to the one person who can do something about it.
    #
    # The movement has already committed — refusing here would tell the
    # operator nothing happened when the credits moved, which is the worse of
    # the two lies. What must not happen is the third thing, which is what
    # happened for a year: reporting plain success while the trail had a hole
    # in it and only a log line knew.
    trail = "" if audited else " (couldn't write the audit trail — tell an engineer)"

    if result.get("duplicate"):
        return Ok(message=f"Already applied — nothing changed.{trail}")
    verb = "added" if body.credits > 0 else "removed"
    return Ok(message=f"{abs(body.credits):,} credits {verb}.{trail}")


# ── coupons ──────────────────────────────────────────────────────────────────


def _coupons() -> list[dict]:
    rows = (
        get_db().table("plan_coupons").select("*")
        .order("created_at", desc=True).limit(200).execute().data or []
    )
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        expires = r.get("expires_at")
        expired = False
        if expires:
            try:
                expired = datetime.fromisoformat(
                    str(expires).replace("Z", "+00:00")) <= now
            except ValueError:
                expired = False
        cap = r.get("max_redemptions")
        out.append({
            **{k: r[k] for k in ("id", "code", "kind", "campaign", "description",
                                 "active", "starts_at", "expires_at",
                                 "max_redemptions", "max_per_partner",
                                 "redeemed_count", "percent_off", "percent_cycles",
                                 "free_days", "razorpay_offer_id", "created_at")},
            # The two facts an operator wants at a glance and would otherwise
            # have to work out from three columns.
            "live": bool(r.get("active")) and not expired
                    and not (cap is not None and r["redeemed_count"] >= cap),
            "exhausted": cap is not None and r["redeemed_count"] >= cap,
        })
    return out


# `plan-coupons`, not `coupons`. `admin/catalog.py` already owns `/admin/coupons`
# for the marketplace's *listing* discounts, in a different Supabase project, and
# both routers are mounted on the same app — the first one registered would have
# silently shadowed the other. Two products, two prefixes.
@router.get("/plan-coupons", response_model=list[AdminCouponOut])
@limiter.limit("60/minute")
async def list_coupons(request: Request, admin: AdminIdentity = Depends(require_admin)):
    return await run_db(_coupons)


def _upsert_coupon(payload: dict, coupon_id: str | None) -> dict:
    db = get_db()
    if coupon_id:
        rows = db.table("plan_coupons").update(payload).eq("id", coupon_id) \
                 .execute().data or []
        if not rows:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No such coupon.")
        return rows[0]
    return db.table("plan_coupons").insert(payload).execute().data[0]


@router.post("/plan-coupons", response_model=AdminCouponOut)
@limiter.limit("30/minute")
async def upsert_coupon(
    request: Request,
    body: CouponUpsertRequest,
    admin: AdminIdentity = Depends(require_admin),
):
    """
    Create or edit a coupon.

    **A percent coupon needs a Razorpay Offer id and is refused without one.**
    Razorpay Offers have no create API — they are built in the Razorpay
    Dashboard — and a percent coupon with no offer behind it validates
    perfectly, shows the owner "50% off", and charges them ₹999. Refusing here
    is the only place that can stop that: the database has the same CHECK, but
    a constraint violation reaching an operator as a 500 teaches nothing.
    """
    code = (body.code or "").strip().upper()
    if not _CODE_RE.match(code):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A code is 3–20 letters and numbers, like LAUNCH50.",
        )

    payload = {
        "code": code,
        "kind": body.kind,
        "campaign": body.campaign,
        "description": body.description,
        "active": body.active,
        "starts_at": body.startsAt.isoformat() if body.startsAt else None,
        "expires_at": body.expiresAt.isoformat() if body.expiresAt else None,
        "max_redemptions": body.maxRedemptions,
        "max_per_partner": body.maxPerPartner,
        "eligible_partners": body.eligiblePartners or [],
        "created_by": admin.actor,
    }

    if body.kind == "percent":
        if not body.percentOff:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "How much off? Enter a percentage between 1 and 90.")
        if not 1 <= body.percentOff <= 90:
            # 90 rather than 100: a code that takes the charge to zero is a
            # mandate Razorpay will not authorise, and the owner would be told
            # by Razorpay that something is wrong with their hotel. A free
            # period is `free_days`, which is a different thing done properly.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{body.percentOff}% is outside what a code can take off. "
                "Enter between 1 and 90 — for a free period, use free days instead.",
            )
        if not (body.razorpayOfferId or "").strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A percentage code needs a matching offer created in Razorpay first — "
                "paste its id here. Without one the code would show a discount and "
                "charge full price.",
            )
        payload |= {
            "percent_off": body.percentOff,
            "percent_cycles": body.percentCycles or 1,
            "razorpay_offer_id": body.razorpayOfferId.strip(),
            "free_days": None,
        }
    else:
        if not body.freeDays:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "How many free days? Enter a number between 1 and 365.")
        payload |= {"free_days": body.freeDays, "percent_off": None,
                    "razorpay_offer_id": None}

    try:
        row = await run_db(_upsert_coupon, payload, body.id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "plan_coupons_code_key" in message or "duplicate key" in message:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"{code} already exists.") from None
        logger.exception("could not save coupon %s", code)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "That coupon couldn't be saved. Check the dates and limits.") \
            from None

    await admin_audit.record(
        actor=admin.actor,
        action="coupon.save",
        entity_type="plan_coupon",
        entity_id=row["id"],
        reason=body.description,
        ip=admin.ip,
        metadata={"code": code, "kind": body.kind, "active": body.active,
                  "new": body.id is None},
    )
    return (await run_db(_coupons_one, row["id"]))


def _coupons_one(coupon_id: str) -> dict:
    for row in _coupons():
        if row["id"] == coupon_id:
            return row
    raise HTTPException(status.HTTP_404_NOT_FOUND, "No such coupon.")


def _redemptions(coupon_id: str) -> list[dict]:
    db = get_db()
    rows = (
        db.table("plan_coupon_redemptions").select("*")
        .eq("coupon_id", coupon_id).order("redeemed_at", desc=True)
        .limit(500).execute().data or []
    )
    names = {}
    ids = [r["property_id"] for r in rows if r.get("property_id")]
    if ids:
        for p in (db.table("properties").select("id,name").in_("id", ids)
                  .execute().data or []):
            names[p["id"]] = p["name"]
    return [{
        "id": r["id"],
        "propertyId": r.get("property_id"),
        "hotelName": names.get(r.get("property_id")),
        "valuePaise": r.get("value_paise") or 0,
        "razorpaySubscriptionId": r.get("razorpay_subscription_id"),
        "redeemedAt": r["redeemed_at"],
    } for r in rows]


@router.get("/plan-coupons/{coupon_id}/redemptions", response_model=list[RedemptionOut])
@limiter.limit("60/minute")
async def coupon_redemptions(
    request: Request, coupon_id: str, admin: AdminIdentity = Depends(require_admin),
):
    """Who used it, and what it cost us."""
    return await run_db(_redemptions, coupon_id)
