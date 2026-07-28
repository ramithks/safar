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
    CouponCreate,
    CouponUpdate,
    ListingPatch,
    ListingPublishRequest,
    ListingPublishResult,
)
from app.api.v1.admin.schemas import Ok
# The publish gates live with the partner API and are imported, not restated:
# an admin publishing for a partner must clear the same checklist they would.
from app.api.v1.marketplace.partner_ops import _has_active_stay_plan, _listing_gaps
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.admin import AdminIdentity, require_admin
from app.services import admin_audit
from app.services.events import emit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

SITE = "https://travbuddy.in"
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


# ── plan coupons ────────────────────────────────────────────────────────────


def _as_coupon(row: dict, used: int) -> AdminCoupon:
    return AdminCoupon(
        id=row["id"],
        code=row["code"],
        kind=row["kind"],
        value=row["value"],
        duration_months=row.get("duration_months"),
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
            "in the Razorpay Dashboard first. Free-month coupons need nothing.",
        )

    row = {
        "code": body.code.strip().upper(),
        "kind": body.kind,
        "value": body.value,
        "duration_months": body.duration_months,
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
