"""
Admin control over the two things the dashboard could previously only watch:
partner listings and plan coupons.

    listings   Review could approve or reject a listing, but not FIX one. A
               missing map link or a ₹100 typo meant a rejection, a message to
               the partner, and a day of round-trip — for an edit that takes
               ten seconds. These endpoints let an admin correct a listing in
               place, and every field they touch lands in the audit trail.

    coupons    The ₹499 plan discount codes (see the website's
               docs/PLAN_COUPONS.md). They existed only as SQL inserts, which
               is fine for one launch code and hopeless for running campaigns.

Same discipline as the rest of the admin API: passcode, validation, DB write,
audit row, event.
"""
# NOTE: deliberately no `from __future__ import annotations` — see the note in
# app/api/v1/admin/endpoint.py.
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.v1.admin.catalog_schemas import (
    AdminCoupon,
    AdminListing,
    BillingMismatch,
    CouponCreate,
    CouponUpdate,
    ListingPatch,
    ListingPublishRequest,
    ListingPublishResult,
    MandateRealignResult,
    PartnerTerms,
    PartnerTermsUpdate,
)
from app.api.v1.admin.schemas import Ok
# The publish gates live with the partner API and are imported, not restated:
# an admin publishing for a partner must clear the same checklist they would.
from app.api.v1.marketplace.partner_ops import _has_active_stay_plan, _listing_gaps
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.admin import AdminIdentity, require_admin
from app.services import admin_audit
from app.services import partner_terms as terms_service
from app.services import razorpay
from app.services.events import emit
from app.services.marketplace_bookings import set_subscription_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

SITE = "https://safar.example.com"
PUBLIC_PATH = {"hotel": "stays", "experience": "experiences"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── listings ────────────────────────────────────────────────────────────────


def _as_listing(row: dict) -> AdminListing:
    partner = row.get("partner") or {}
    kind = row.get("kind") or "experience"
    return AdminListing(
        id=row["id"],
        kind=kind,
        slug=row.get("slug") or "",
        title=row.get("title") or "Untitled",
        status=row.get("status") or "draft",
        partner_id=row.get("partner_id"),
        brand=partner.get("brand_name") or "—",
        city=row.get("city"),
        destination=row.get("destination"),
        address=row.get("address"),
        map_url=row.get("map_url"),
        summary=row.get("summary"),
        description=row.get("description"),
        price_from=row.get("price_from"),
        price_unit=row.get("price_unit"),
        photos=row.get("photos") or [],
        content=row.get("content") or {},
        views=row.get("views") or 0,
        leads=row.get("leads") or 0,
        rejection_reason=row.get("rejection_reason"),
        updated_at=str(row.get("updated_at") or ""),
        public_url=f"{SITE}/{PUBLIC_PATH.get(kind, 'experiences')}/{row.get('slug') or ''}",
    )


@router.get("/listings", response_model=list[AdminListing])
@limiter.limit("120/minute")
async def list_listings(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
    status: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=300, ge=1, le=1000),
) -> list[AdminListing]:
    """Every partner listing, newest first. Filters are optional."""

    def _query():
        q = (
            get_db()
            .table("partner_listings")
            .select("*, partner:partners(brand_name, slug)")
            .order("updated_at", desc=True)
            .limit(limit)
        )
        if status:
            q = q.eq("status", status)
        if kind:
            q = q.eq("kind", kind)
        return q.execute()

    try:
        res = await run_db(_query)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] listing load failed")
        raise HTTPException(502, "Could not load listings.") from exc

    return [_as_listing(r) for r in (res.data or [])]


async def _load_listing(listing_id: str) -> AdminListing:
    """Plain loader, so handlers can reuse it without re-entering a rate limiter."""
    res = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .select("*, partner:partners(brand_name, slug)")
        .eq("id", listing_id)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        raise HTTPException(404, "Listing not found.")
    return _as_listing(res.data)


@router.get("/listings/{listing_id}", response_model=AdminListing)
@limiter.limit("120/minute")
async def get_listing(
    request: Request,
    listing_id: str,
    _admin: AdminIdentity = Depends(require_admin),
) -> AdminListing:
    return await _load_listing(listing_id)


@router.patch("/listings/{listing_id}", response_model=AdminListing)
@limiter.limit("60/minute")
async def patch_listing(
    request: Request,
    listing_id: str,
    body: ListingPatch,
    admin: AdminIdentity = Depends(require_admin),
) -> AdminListing:
    """
    Edit any partner's listing in place.

    Only the fields actually present in the request are written, so an admin
    fixing a price can't blank a description by omission. The audit row records
    exactly which keys changed — this is someone else's business being edited,
    and "who changed my price?" deserves an answer.
    """
    before = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .select("*")
        .eq("id", listing_id)
        .maybe_single()
        .execute()
    )
    if not before or not before.data:
        raise HTTPException(404, "Listing not found.")

    patch = body.model_dump(exclude_unset=True, exclude_none=True)
    # `reason` explains the edit in the audit trail; it is not a column.
    patch.pop("reason", None)
    if not patch:
        raise HTTPException(400, "Nothing to update.")
    patch["updated_at"] = _now()

    try:
        res = await run_db(
            lambda: get_db()
            .table("partner_listings")
            .update(patch)
            .eq("id", listing_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] listing patch failed")
        raise HTTPException(502, "Could not save that listing.") from exc
    if not res.data:
        raise HTTPException(502, "Could not save that listing.")

    changed = [k for k in patch if k != "updated_at"]
    await admin_audit.record(
        actor=admin.actor,
        action="listing.edited",
        entity_type="listing",
        entity_id=listing_id,
        entity_label=before.data.get("title"),
        reason=body.reason,
        ip=admin.ip,
        metadata={"fields": changed},
    )
    await emit(
        "listing.edited",
        {
            "actor": admin.actor,
            "listing_id": listing_id,
            "partner_id": before.data.get("partner_id"),
            "entity_label": before.data.get("title"),
            "fields": changed,
        },
    )
    return await _load_listing(listing_id)


@router.post("/listings/{listing_id}/publish", response_model=ListingPublishResult)
@limiter.limit("60/minute")
async def publish_listing(
    request: Request,
    listing_id: str,
    body: ListingPublishRequest,
    admin: AdminIdentity = Depends(require_admin),
) -> ListingPublishResult:
    """
    Put any partner's listing live, or take it back down.

    `/review` could only act on something already sitting in the queue at
    'pending'. A listing an admin built themselves — onboarding a partner who
    sent their details over WhatsApp — never passes through that state, so it
    could be written and never published. This is that missing verb.

    The two gates are imported from the partner API rather than restated:
    an admin publishing on someone's behalf must clear exactly the checklist
    the partner would have, or the dashboard becomes a way to put half-finished
    pages on the marketplace. The plan gate is overridable (`ignore_plan`) —
    it's a commercial rule, not a quality one, and comping a listing while an
    invoice is sorted out is a legitimate call.
    """
    row = await run_db(
        lambda: get_db()
        .table("partner_listings")
        .select("*")
        .eq("id", listing_id)
        .maybe_single()
        .execute()
    )
    if not row or not row.data:
        raise HTTPException(404, "Listing not found.")
    listing = row.data
    title = listing.get("title")
    kind = listing.get("kind") or "experience"
    public_url = f"{SITE}/{PUBLIC_PATH.get(kind, 'experiences')}/{listing.get('slug') or ''}"

    if not body.live:
        try:
            await run_db(
                lambda: get_db().table("partner_listings")
                .update({"status": "draft", "updated_at": _now()})
                .eq("id", listing_id).execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[admin] unpublish failed")
            raise HTTPException(502, "Could not unpublish that listing.") from exc

        await admin_audit.record(
            actor=admin.actor, action="listing.unpublished",
            entity_type="listing", entity_id=listing_id, entity_label=title,
            reason=body.reason, ip=admin.ip, metadata={"kind": kind},
        )
        await emit("listing.unpublished", {
            "actor": admin.actor, "entity_id": listing_id, "entity_label": title,
            "partner_id": listing.get("partner_id"),
        })
        return ListingPublishResult(
            ok=True, status="draft", message="Taken down. It's a draft again.",
            public_url=public_url,
        )

    gaps = _listing_gaps(listing)
    if gaps:
        return ListingPublishResult(
            ok=False,
            status=listing.get("status") or "draft",
            message="Not ready yet — a few things are still missing.",
            gaps=gaps,
            public_url=public_url,
        )

    partner_id = listing.get("partner_id")
    if partner_id and not body.ignore_plan:
        partner = await run_db(
            lambda: get_db().table("partners")
            .select("stay_plan_status, stay_plan_expires_at, brand_name")
            .eq("id", partner_id).maybe_single().execute()
        )
        if partner and partner.data and not _has_active_stay_plan(partner.data):
            return ListingPublishResult(
                ok=False,
                status=listing.get("status") or "draft",
                message=(
                    "This partner has no active ₹499 plan. Grant them a month first — "
                    "the nightly expiry sweep would pull this back down otherwise."
                ),
                needs_plan=True,
                public_url=public_url,
            )

    patch = {
        "status": "published",
        "published_at": _now(),
        "approved_at": _now(),
        "rejection_reason": None,
        "updated_at": _now(),
    }
    try:
        res = await run_db(
            lambda: get_db().table("partner_listings").update(patch)
            .eq("id", listing_id).execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] publish failed")
        raise HTTPException(502, "Could not publish that listing.") from exc
    if not res.data:
        raise HTTPException(502, "Could not publish that listing.")

    await admin_audit.record(
        actor=admin.actor, action="listing.published",
        entity_type="listing", entity_id=listing_id, entity_label=title,
        reason=body.reason, ip=admin.ip,
        metadata={"kind": kind, "slug": listing.get("slug"), "ignore_plan": body.ignore_plan},
    )
    # Same event the review queue emits on approve, so whatever notifies a
    # partner that they're live keeps working when we publish for them.
    await emit("listing.approved", {
        "actor": admin.actor,
        "kind": "experience",
        "entity_id": listing_id,
        "entity_label": title,
        "partner_id": partner_id,
        "public_url": public_url,
    })

    return ListingPublishResult(
        ok=True, status="published", message="Published — it's live.", public_url=public_url,
    )


# ── partner terms ───────────────────────────────────────────────────────────
#
# The negotiated-deal path. Coupons cover deals you can hand out as a code, but
# B2B is agreed on a call: "₹299/month and 5% and we'll list your other two
# properties". Manufacturing a single-use coupon to express that was the
# alternative, and it puts a fake campaign in the reporting forever.
#
# This is also the only way to change an EXISTING subscriber's terms — a coupon
# only ever applies at subscribe.


async def _partner_or_404(partner_id: str) -> dict:
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select(
            "id, brand_name, subscription_id, subscription_status, "
            "billed_amount_paise, billed_period, stay_plan_expires_at"
        )
        .eq("id", partner_id)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        raise HTTPException(404, "Partner not found.")
    return res.data


async def _terms_payload(
    partner: dict, stale: list[str] | None = None
) -> PartnerTerms:
    """
    A partner's resolved terms, with everything an operator needs to act on.

    The coupon is read only for its CODE, and only as a label. A redeemed
    coupon is not the contract — its terms were materialised into
    `partner_terms` at redemption, and editing, disabling or deleting the code
    afterwards changes nothing here. That is the point of materialising them.
    """
    partner_id = partner["id"]
    terms = await terms_service.resolve(partner_id)

    code = None
    if terms.get("source_coupon_id"):
        found = await run_db(
            lambda: get_db()
            .table("platform_coupons")
            .select("code")
            .eq("id", terms["source_coupon_id"])
            .maybe_single()
            .execute()
        )
        code = (found.data or {}).get("code") if found else None

    mismatch = await terms_service.billing_mismatch(partner, terms)

    return PartnerTerms(
        partner_id=partner_id,
        subscription_fee_paise=int(terms["subscription_fee_paise"]),
        billing_period=terms["billing_period"],
        commission_pct=float(terms["commission_pct"]),
        commission_expires_at=(
            str(terms["commission_expires_at"]) if terms.get("commission_expires_at") else None
        ),
        base_commission_pct=float(terms.get("base_commission_pct") or 0),
        entitlement=terms.get("entitlement") or "subscription",
        entitlement_expires_at=(
            str(terms["entitlement_expires_at"])
            if terms.get("entitlement_expires_at")
            else None
        ),
        effective_at=str(terms["effective_at"]) if terms.get("effective_at") else None,
        lapsed=bool(terms.get("lapsed")),
        intro_ended=bool(terms.get("intro_ended")),
        source=terms.get("source") or "default",
        source_coupon_id=terms.get("source_coupon_id"),
        source_coupon_code=code,
        notes=terms.get("notes"),
        summary=terms_service.describe(terms),
        billing_mismatch=BillingMismatch(**mismatch) if mismatch else None,
        stale_listings=stale or [],
    )


@router.get("/partners/{partner_id}/terms", response_model=PartnerTerms)
@limiter.limit("120/minute")
async def get_partner_terms(
    request: Request,
    partner_id: str,
    _admin: AdminIdentity = Depends(require_admin),
) -> PartnerTerms:
    """
    What this partner is on. A partner with no row is on standard terms, and
    this reports those rather than 404ing — "no custom deal" is an answer.
    """
    partner = await _partner_or_404(partner_id)
    return await _terms_payload(partner)


@router.put("/partners/{partner_id}/terms", response_model=PartnerTerms)
@limiter.limit("60/minute")
async def set_partner_terms(
    request: Request,
    partner_id: str,
    body: PartnerTermsUpdate,
    admin: AdminIdentity = Depends(require_admin),
) -> PartnerTerms:
    """
    Put a partner on a negotiated deal.

    Takes effect on the NEXT booking and the NEXT billing cycle. Nothing already
    sold moves: `bookings.commission_pct` is a snapshot, and a mandate Razorpay
    is already collecting keeps its amount until it is cancelled and reopened.
    Say so out loud when the fee changes, because "I changed it in the dashboard
    and they're still being charged ₹499" is otherwise a bug report.
    """
    partner = await _partner_or_404(partner_id)
    before = await terms_service.resolve(partner_id)

    if body.entitlement == "until" and not body.entitlement_expires_at:
        raise HTTPException(400, "An 'until' entitlement needs an end date.")

    # One timestamp for the whole change: the audit row, the terms row and the
    # intro window all date from the same instant, so "when did this deal start"
    # has exactly one answer.
    effective_at = datetime.now(timezone.utc)

    try:
        applied = await terms_service.apply(
            partner_id,
            fee_paise=body.subscription_fee_paise,
            period=body.billing_period,
            commission=body.commission_pct,
            commission_months=body.commission_months,
            base_commission=body.base_commission_pct,
            entitlement=body.entitlement,
            entitlement_expires_at=body.entitlement_expires_at,
            source="admin",
            notes=body.notes,
            effective_at=effective_at,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] terms update failed")
        raise HTTPException(502, "Could not update those terms.") from exc

    # Recorded on the audit row rather than left for someone to notice: a fee
    # change that leaves the mandate collecting the old amount is the single
    # most likely thing to be misread as "the system did it".
    mismatch = await terms_service.billing_mismatch(partner, applied)

    await admin_audit.record(
        actor=admin.actor,
        action="partner.terms_changed",
        entity_type="partner",
        entity_id=partner_id,
        entity_label=partner.get("brand_name"),
        reason=body.reason,
        ip=admin.ip,
        metadata={
            "effective_at": effective_at.isoformat(),
            "from": {
                "fee_paise": before["subscription_fee_paise"],
                "period": before["billing_period"],
                "commission_pct": before["commission_pct"],
                "commission_expires_at": (
                    str(before["commission_expires_at"])
                    if before.get("commission_expires_at")
                    else None
                ),
                "entitlement": before.get("entitlement"),
            },
            "to": {
                "fee_paise": body.subscription_fee_paise,
                "period": body.billing_period,
                "commission_pct": body.commission_pct,
                "commission_months": body.commission_months,
                "entitlement": applied.get("entitlement"),
                "entitlement_expires_at": body.entitlement_expires_at,
            },
            "notes": body.notes,
            # Set when Razorpay is now collecting something these terms don't
            # describe. Someone has to act on it; the audit says who knew.
            "billing_mismatch": mismatch,
            "stale_listings": applied.get("stale_listings") or None,
        },
    )
    await emit(
        "partner.terms_changed",
        {
            "actor": admin.actor,
            "partner_id": partner_id,
            "entity_label": partner.get("brand_name"),
            "billing_mismatch": bool(mismatch),
        },
    )
    return await _terms_payload(partner, stale=applied.get("stale_listings"))


@router.delete("/partners/{partner_id}/terms", response_model=PartnerTerms)
@limiter.limit("60/minute")
async def reset_partner_terms(
    request: Request,
    partner_id: str,
    admin: AdminIdentity = Depends(require_admin),
) -> PartnerTerms:
    """Put a partner back on standard terms, listings included."""
    partner = await _partner_or_404(partner_id)
    before = await terms_service.resolve(partner_id)

    try:
        await terms_service.clear(partner_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] terms reset failed")
        raise HTTPException(502, "Could not reset those terms.") from exc

    await admin_audit.record(
        actor=admin.actor,
        action="partner.terms_reset",
        entity_type="partner",
        entity_id=partner_id,
        entity_label=partner.get("brand_name"),
        ip=admin.ip,
        metadata={
            "effective_at": datetime.now(timezone.utc).isoformat(),
            "from": terms_service.describe(before),
        },
    )
    return await _terms_payload(partner)


@router.post("/partners/{partner_id}/terms/realign", response_model=MandateRealignResult)
@limiter.limit("30/minute")
async def realign_mandate(
    request: Request,
    partner_id: str,
    admin: AdminIdentity = Depends(require_admin),
) -> MandateRealignResult:
    """
    Cancel a mandate that no longer matches the partner's terms, so they can
    re-subscribe at the agreed price.

    THE RAZORPAY CONSTRAINT this exists for: a subscription is bound to the plan
    it was created against, and there is no API to move it to another one.
    Re-pricing therefore means cancelling and reopening — there is no in-place
    edit to reach for, and pretending otherwise is what leaves a partner told
    they're on ₹299/month while ₹499/year keeps leaving their account.

    Why cancelling immediately does not rob them: `stay_plan_expires_at` is
    untouched, and that — not the mandate — is what keeps their listings up.
    They stay live for the period they already paid for, and re-subscribing
    extends from that date rather than from today (`activate_stay_plan` extends
    from max(now, current expiry)), so no time is lost either.

    Why it cannot double-charge: the old mandate is dead before the partner can
    open Checkout again, and `/subscribe` refuses to start a second one while a
    live mandate exists. There is never a moment with two mandates collecting.
    """
    partner = await _partner_or_404(partner_id)
    subscription_id = partner.get("subscription_id")

    mismatch = await terms_service.billing_mismatch(partner)
    if not mismatch:
        raise HTTPException(
            409,
            "Their mandate already matches their terms — nothing to realign.",
        )

    result = await razorpay.cancel_subscription(subscription_id, at_cycle_end=False)
    if result is None:
        raise HTTPException(502, "Razorpay would not cancel that mandate. Try again.")

    await set_subscription_status(subscription_id, result.get("status") or "cancelled")

    await admin_audit.record(
        actor=admin.actor,
        action="partner.mandate_realigned",
        entity_type="partner",
        entity_id=partner_id,
        entity_label=partner.get("brand_name"),
        ip=admin.ip,
        metadata={
            "effective_at": datetime.now(timezone.utc).isoformat(),
            "cancelled_subscription_id": subscription_id,
            "was_billing": mismatch,
            "paid_through": str(partner.get("stay_plan_expires_at") or ""),
        },
    )
    await emit(
        "partner.mandate_realigned",
        {
            "actor": admin.actor,
            "partner_id": partner_id,
            "entity_label": partner.get("brand_name"),
        },
    )

    fresh = await _partner_or_404(partner_id)
    return MandateRealignResult(
        ok=True,
        message=(
            "Mandate cancelled. Their listings stay live until "
            f"{str(partner.get('stay_plan_expires_at') or 'their paid period ends')[:10]}; "
            "ask them to subscribe again from their Plan page to start the new terms."
        ),
        terms=await _terms_payload(fresh),
    )


# ── plan coupons ────────────────────────────────────────────────────────────


def _as_coupon(row: dict, used: int) -> AdminCoupon:
    return AdminCoupon(
        id=row["id"],
        code=row["code"],
        kind=row["kind"],
        value=row["value"],
        duration_months=row.get("duration_months"),
        plan_amount_paise=row.get("plan_amount_paise"),
        plan_period=row.get("plan_period"),
        commission_pct=(
            float(row["commission_pct"]) if row.get("commission_pct") is not None else None
        ),
        commission_months=row.get("commission_months"),
        razorpay_offer_id=row.get("razorpay_offer_id"),
        description=row.get("description"),
        campaign=row.get("campaign"),
        starts_at=str(row["starts_at"]) if row.get("starts_at") else None,
        ends_at=str(row["ends_at"]) if row.get("ends_at") else None,
        usage_limit=row.get("usage_limit"),
        used_count=row.get("used_count") or 0,
        redemptions=used,
        active=bool(row.get("active")),
        created_at=str(row.get("created_at") or ""),
    )


@router.get("/coupons", response_model=list[AdminCoupon])
@limiter.limit("120/minute")
async def list_coupons(
    request: Request,
    _admin: AdminIdentity = Depends(require_admin),
) -> list[AdminCoupon]:
    """
    Every plan coupon with its real redemption count.

    `used_count` on the row is the counter the redemption function bumps;
    `redemptions` counts actual rows. They should agree, and showing both means
    a drift is visible rather than discovered during a finance question.
    """
    try:
        res = await run_db(
            lambda: get_db()
            .table("platform_coupons")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        red = await run_db(
            lambda: get_db().table("platform_coupon_redemptions").select("coupon_id").execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] coupon load failed")
        raise HTTPException(502, "Could not load coupons.") from exc

    counts: dict[str, int] = {}
    for r in red.data or []:
        cid = r.get("coupon_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1

    return [_as_coupon(r, counts.get(r["id"], 0)) for r in (res.data or [])]


@router.post("/coupons", response_model=AdminCoupon, status_code=201)
@limiter.limit("60/minute")
async def create_coupon(
    request: Request,
    body: CouponCreate,
    admin: AdminIdentity = Depends(require_admin),
) -> AdminCoupon:
    """
    Mint a plan coupon.

    The percent/flat kinds need a Razorpay offer id because Razorpay, not us,
    applies that discount — a coupon without one would validate at checkout and
    then charge full price. The DB has the same constraint; this check exists so
    the admin gets a sentence instead of a Postgres error.
    """
    if body.kind in {"percent", "flat"} and not body.razorpay_offer_id:
        raise HTTPException(
            400,
            "Percent and flat coupons need a Razorpay offer id — create the offer "
            "in the Razorpay Dashboard first. Free-month and deal coupons need nothing.",
        )

    if body.kind == "deal":
        if body.plan_amount_paise is None or not body.plan_period:
            raise HTTPException(
                400, "A deal code needs a price and a billing period."
            )
        # An intro period with no rate to be introductory ABOUT is the one way
        # to build a code that promises a discount and delivers standard terms.
        if body.commission_months and body.commission_pct is None:
            raise HTTPException(
                400,
                "This code sets an intro period but no commission rate. Set the "
                "rate it starts on, or clear the intro months.",
            )

    row = {
        "code": body.code.strip().upper(),
        "kind": body.kind,
        "value": body.value,
        "duration_months": body.duration_months,
        "plan_amount_paise": body.plan_amount_paise,
        "plan_period": body.plan_period,
        "commission_pct": body.commission_pct,
        "commission_months": body.commission_months,
        "razorpay_offer_id": body.razorpay_offer_id,
        "description": body.description,
        "campaign": body.campaign,
        "starts_at": body.starts_at,
        "ends_at": body.ends_at,
        "usage_limit": body.usage_limit,
        "active": body.active,
    }

    try:
        res = await run_db(
            lambda: get_db().table("platform_coupons").insert(row).execute()
        )
    except Exception as exc:  # noqa: BLE001
        # The unique index on upper(code) is the common one by a mile.
        if "duplicate key" in str(exc).lower() or "unique" in str(exc).lower():
            raise HTTPException(409, f"The code {row['code']} already exists.") from exc
        logger.exception("[admin] coupon create failed")
        raise HTTPException(502, "Could not create that coupon.") from exc
    if not res.data:
        raise HTTPException(502, "Could not create that coupon.")

    created = res.data[0]
    await admin_audit.record(
        actor=admin.actor,
        action="coupon.created",
        entity_type="coupon",
        entity_id=created["id"],
        entity_label=created["code"],
        ip=admin.ip,
        metadata={k: v for k, v in row.items() if v is not None},
    )
    await emit("coupon.created", {"actor": admin.actor, "code": created["code"]})
    return _as_coupon(created, 0)


@router.patch("/coupons/{coupon_id}", response_model=AdminCoupon)
@limiter.limit("60/minute")
async def update_coupon(
    request: Request,
    coupon_id: str,
    body: CouponUpdate,
    admin: AdminIdentity = Depends(require_admin),
) -> AdminCoupon:
    """
    Edit a live coupon — in practice, switch it off.

    The code, kind and value are deliberately NOT editable: partners have been
    handed these in a campaign, and quietly turning "3 months free" into "1
    month free" under an unchanged code is worse than issuing a new one.
    """
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, "Nothing to update.")

    before = await run_db(
        lambda: get_db()
        .table("platform_coupons")
        .select("*")
        .eq("id", coupon_id)
        .maybe_single()
        .execute()
    )
    if not before or not before.data:
        raise HTTPException(404, "Coupon not found.")

    try:
        res = await run_db(
            lambda: get_db()
            .table("platform_coupons")
            .update(patch)
            .eq("id", coupon_id)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[admin] coupon update failed")
        raise HTTPException(502, "Could not update that coupon.") from exc
    if not res.data:
        raise HTTPException(502, "Could not update that coupon.")

    await admin_audit.record(
        actor=admin.actor,
        action="coupon.updated",
        entity_type="coupon",
        entity_id=coupon_id,
        entity_label=before.data.get("code"),
        ip=admin.ip,
        metadata={"patch": patch},
    )
    await emit("coupon.updated", {"actor": admin.actor, "code": before.data.get("code")})

    count = await run_db(
        lambda: get_db()
        .table("platform_coupon_redemptions")
        .select("id")
        .eq("coupon_id", coupon_id)
        .execute()
    )
    return _as_coupon(res.data[0], len(count.data or []))
