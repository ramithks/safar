"""
PMS API — the hotel-facing operational endpoints.

    PWA ──► GET  /api/v1/pms/properties/{id}/availability   what can be sold
        ──► POST /api/v1/pms/properties/{id}/quotes         price it + hold it
        ──► POST /api/v1/pms/properties/{id}/reservations   quote → booking
        ──► POST .../reservations/{id}/check-in             front desk
        ──► POST .../reservations/{id}/folio                append to the account
        ──► POST /api/v1/pms/properties/{id}/tasks          raise a ticket

Every route resolves membership from the JWT before the handler runs
(`deps/property.require_member`). The property id in the path is checked against
the caller's membership there, so a guessed id 404s rather than reading someone
else's arrivals list. No route accepts a partner_id.

These write through the service-role client because several of them call stock
RPCs that are revoked from browser roles by design. RLS remains the backstop for
anything that reaches the database another way — the same posture as
marketplace/partner_ops.py.
"""
# NOTE: deliberately no `from __future__ import annotations` — PEP 563 breaks
# FastAPI body resolution through slowapi's wrapper. Same note as
# api/v1/admin/endpoint.py.
import asyncio
import json
import secrets
import re
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import (
    APIRouter, Depends, File, Form, Header, HTTPException, Query, Request,
    UploadFile, status,
)

from app.api.v1.pms.schemas import (
    PaymentLinkOut,
    PaymentLinkRequest,
    StayPaymentConfirmRequest,
    StayPaymentOut,
    AgencyBookingRequest,
    AgencyContractOut,
    AgencyMeOut,
    AgencyPropertyOut,
    AgencyQuoteOut,
    AgencyQuoteRequest,
    AgentActionOut,
    AgentAutonomyRequest,
    AgentDecisionRequest,
    AgentOut,
    AnalyticsReportOut,
    AriEditOut,
    BillingSummaryOut,
    BookingRequest,
    CheckInRequest,
    AttributionTouchOut,
    CampaignStatusRequest,
    ConsoleActionOut,
    CampaignOut,
    CreatorCampaignCreateRequest,
    CreatorCampaignOut,
    CreatorPayoutOut,
    PayoutApprovalOut,
    PayoutApprovalRequest,
    PayoutEventOut,
    PublicAttributionOut,
    PublicAttributionRequest,
    ConsoleAskOut,
    ConsoleAskRequest,
    ConversationHandlerRequest,
    ConversationOut,
    DailyPointOut,
    GuestOut,
    HotelCreateOut,
    HotelCreateRequest,
    OnboardingOut,
    RatePlanCreateRequest,
    RoomTypeCreateRequest,
    RoomsCreateRequest,
    FolioEntryOut,
    MarginRowOut,
    MemberOut,
    ServiceOut,
    ServiceUpsertRequest,
    GuestRegistrationOut,
    BookingLinkOut,
    BookingPageOut,
    ReceiptLineOut,
    PublicStayOut,
    WebCheckInRequest,
    CreditOrderOut,
    CreditOrderRequest,
    CreditPurchaseOut,
    PaymentOrderOut,
    PublicAvailabilityOut,
    BookingLinkOut,
    PublicBookingOut,
    PublicConfirmRequest,
    PublicOrderOut,
    PublicOrderRequest,
    PublicQuoteOut,
    PublicQuoteRequest,
    PublicRoomOptionOut,
    PublicServiceOut,
    PublicSiteOut,
    QuotedAddonOut,
    WebChatIn,
    WebChatOpenerOut,
    WebChatOut,
    WebsiteConfigOut,
    PlanStateOut,
    CreditsSummaryOut,
    CouponPreviewOut,
    CouponRequest,
    SubscribeRequest,
    CheckoutOut,
    DomainOut,
    DomainRequest,
    DnsRecordOut,
    PublicSiteHostOut,
    WebsiteUpdateRequest,
    FolioOut,
    FolioPostRequest,
    InventoryEditRequest,
    InventoryNightOut,
    MeOut,
    MessageOut,
    NightAuditOut,
    NightAuditRequest,
    NightlyOut,
    OwnerAskOut,
    OwnerAskRequest,
    OwnerOverviewOut,
    PropertyOut,
    QuoteOut,
    QuoteRequest,
    RateEditRequest,
    RateNightOut,
    RatePlanOptionOut,
    RatePlanOut,
    RecommendationOut,
    RecommendationStatusRequest,
    ReservationOut,
    RoomOut,
    RoomStatusRequest,
    ExtractedHotel,
    ExtractedRoomType,
    HotelExtractOut,
    HotelExtractRequest,
    MediaUploadOut,
    PropertyLocationOut,
    PropertyUpdateRequest,
    PublicHotelIndexOut,
    ReadinessOut,
    RoomTypeOut,
    RoomTypeUpdateRequest,
    SendMessageRequest,
    StayOptionOut,
    SyncOut,
    TaskCreateRequest,
    TaskOut,
    TaskStatusRequest,
    TodayStatsOut,
    UsageEventOut,
    UsageGroupOut,
    UsageMeterOut,
)
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.auth import require_user
from app.deps.b2b import require_agency
from app.deps.property import ROLE_PERMS, require_member
from app.services import billing
from app.services.pms import ari as ari_service
from app.services.pms import availability as availability_service
from app.services.pms import b2b as b2b_service
from app.services.pms import clock
from app.services.pms import onboarding as onboarding_service
from app.services.pms import creators as creators_service
from app.services.pms import booking as booking_service
from app.services.pms import frontdesk as frontdesk_service
from app.services.pms import payments as payments_service
from app.services.pms import stay_payment
from app.services.pms import public_booking
from app.services.pms import quote_payment
from app.services.billing import subscription as subscription_service
from app.services.billing import plan_coupons as plan_coupons_service
from app.services.pms import webhook_log
from app.services.messaging import web_chat
from app.services.pms import sync as sync_service
from app.services.pms import website as website_service
from app.services.pms import domains as domains_service
from app.services import razorpay
from app.config import settings
from app.services.pms import extract as pms_extract
from app.services.pms import seo_ping
from app.storage import gcs
from app.services.ai import approvals
from app.services.ai.errors import AgentError
from app.services.ai import console as ai_console
from app.services.ai import model_router
from app.services.messaging import service as messaging_service
from app.services.messaging.channels import MessagingError
from app.services.pms import workspace as workspace_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pms", tags=["pms"])

# What one credit is worth in subscription revenue, in paise.
# ₹2,499/month against a 3,000-credit grant — the anchoring stated in
# migration 13. Named here so the margin route is the only place that
# prices a credit, and never the client.
CREDIT_VALUE_PAISE = 83

# What a hotel pays for one credit, in paise. ₹1.00 — above the ₹0.83 a
# credit is worth inside a subscription, because a top-up is bought outside
# an allowance somebody has already paid for. The one place this conversion
# happens; a rupee amount never arrives from a client.
CREDIT_PRICE_PAISE = 100


# ── workspace ────────────────────────────────────────────────────────────────
# What the PWA asks before it can render anything: who am I, and which hotels
# may I open. Both derive from the JWT alone. Note these two take `require_user`
# rather than `require_member` — there is no property in the path yet, which is
# exactly why they must do their own scoping and never take a hint from the
# client about which partner to look at.

@router.get("/me", response_model=MeOut)
@limiter.limit("120/minute")
async def get_me(request: Request, user=Depends(require_user)):
    """
    The caller, and their role at each partner they belong to.

    `permissions` is flattened from the role table plus per-member grants so the
    client can decide which buttons to draw. It is not a security boundary and
    must never be treated as one — every route re-derives the same answer
    server-side through `require_member`.
    """
    return await workspace_service.me(str(user.id))


@router.get("/properties", response_model=list[PropertyOut])
@limiter.limit("120/minute")
async def list_properties(request: Request, user=Depends(require_user)):
    """Every property this person may open, and only those."""
    rows = await workspace_service.list_properties(str(user.id))
    return [PropertyOut.from_row(r) for r in rows]


@router.get("/properties/{property_id}/today", response_model=TodayStatsOut)
@limiter.limit("120/minute")
async def get_today(request: Request, property_id: str, member=Depends(require_member())):
    """Occupancy, ADR, RevPAR and the arrival/departure counts for today."""
    return await workspace_service.today_stats(property_id)


@router.get("/properties/{property_id}/room-types", response_model=list[RoomTypeOut])
@limiter.limit("120/minute")
async def list_room_types(request: Request, property_id: str, member=Depends(require_member())):
    rows = await workspace_service.list_room_types(property_id)
    return [RoomTypeOut.from_row(r) for r in rows]


@router.get("/properties/{property_id}/rate-plans", response_model=list[RatePlanOut])
@limiter.limit("120/minute")
async def list_rate_plans(request: Request, property_id: str, member=Depends(require_member())):
    rows = await workspace_service.list_rate_plans(property_id)
    return [RatePlanOut.from_row(r) for r in rows]


@router.get("/properties/{property_id}/tasks", response_model=list[TaskOut])
@limiter.limit("120/minute")
async def list_tasks(request: Request, property_id: str, member=Depends(require_member("tasks"))):
    rows = await workspace_service.list_tasks(property_id)
    return [TaskOut.from_row(r) for r in rows]



# ── onboarding ───────────────────────────────────────────────────────────────
# The half of the product that did not exist. Everything below `/properties/{id}`
# assumed a hotel; nothing made one, so the only way onto StayOS was a script
# somebody ran on a laptop.
#
# `require_user`, not `require_member`, on the first route only — there is no
# property to be a member of yet. That is exactly why it takes no partner id
# and no property id from the client: the owner is `auth.uid()` and nothing else.

@router.post("/onboarding/hotel", response_model=HotelCreateOut, status_code=201)
@limiter.limit("20/hour")
async def create_hotel(request: Request, body: HotelCreateRequest, user=Depends(require_user)):
    """
    Create this person's hotel, and hire its AI employee.

    Idempotent: a second call returns the hotel they already have rather than
    a second one, because `partners.owner_id` is unique and a double-tap on a
    slow connection is the ordinary case, not the exceptional one.
    """
    return await onboarding_service.create_hotel(
        profile_id=str(user.id),
        hotel_name=body.hotelName,
        city=body.city,
        owner_name=body.ownerName,
        phone=body.phone,
    )


@router.get("/properties/{property_id}/onboarding", response_model=OnboardingOut)
@limiter.limit("120/minute")
async def get_onboarding(request: Request, property_id: str, member=Depends(require_member())):
    """What is done, what is left, and whether this hotel can go live."""
    return await onboarding_service.checklist(property_id)


@router.post("/properties/{property_id}/room-types", response_model=RoomTypeOut, status_code=201)
@limiter.limit("60/hour")
async def create_room_type(
    request: Request,
    property_id: str,
    body: RoomTypeCreateRequest,
    member=Depends(require_member("rates")),
):
    """
    A kind of room this hotel sells, and optionally what a night in it costs.

    The price is taken here rather than in a second request because a room type
    with no rate plan is not sellable, and a two-step flow is a flow that stops
    between the steps.
    """
    room_type = await onboarding_service.add_room_type_with_price(
        property_id=property_id,
        partner_id=member.partner_id,
        name=body.name,
        total_units=body.totalUnits,
        max_occupancy=body.maxOccupancy,
        code=body.code,
        price_paise=body.priceRupees * 100 if body.priceRupees is not None else None,
        meal_plan=body.mealPlan,
        description=body.description,
        bed_type=body.bedType,
        size_sqft=body.sizeSqft,
        amenities=body.amenities,
        photos=body.photos,
    )
    return RoomTypeOut.from_row(room_type)


@router.patch(
    "/properties/{property_id}/room-types/{room_type_id}", response_model=RoomTypeOut
)
@limiter.limit("120/hour")
async def update_room_type(
    request: Request,
    property_id: str,
    room_type_id: str,
    body: RoomTypeUpdateRequest,
    member=Depends(require_member("rates")),
):
    """
    Change a room that already exists — including adding the photos it never had.

    **There was no route for this**, and that is why every room type in
    production carried a null description, no amenities and no photos: they were
    all created before the form asked, and the only way to "fix" one was to
    delete it and lose its inventory, its rate plans and its history.

    `exclude_unset` is the whole contract. A field the client did not send is
    left alone; a field sent empty is cleared. Without that distinction, a screen
    that renames a room would silently empty its description, which is the exact
    shape of bug that produced the state this route exists to repair.
    """
    row = await onboarding_service.update_room_type(
        property_id=property_id,
        room_type_id=room_type_id,
        fields={
            {
                "totalUnits": "total_units",
                "maxOccupancy": "max_occupancy",
                "bedType": "bed_type",
                "sizeSqft": "size_sqft",
                "isActive": "is_active",
            }.get(key, key): value
            for key, value in body.model_dump(exclude_unset=True).items()
        },
    )
    # A room edit changes the hotel's public page, and adding the fourth
    # photograph is the precise moment a hotel crosses the indexability gate.
    # Best effort: a purge that fails must not fail the edit.
    await seo_ping.hotel_changed(await _slug_of(property_id))
    return RoomTypeOut.from_row(row)


async def _slug_of(property_id: str) -> str | None:
    """This property's public slug, for the SEO purge. None rather than raising."""
    def _read():
        rows = (
            get_db().table("properties").select("slug")
            .eq("id", property_id).limit(1).execute().data or []
        )
        return rows[0].get("slug") if rows else None

    try:
        return await run_db(_read)
    except Exception:  # noqa: BLE001
        return None


@router.patch("/properties/{property_id}", response_model=PropertyOut)
@limiter.limit("120/hour")
async def update_property_details(
    request: Request,
    property_id: str,
    body: PropertyUpdateRequest,
    member=Depends(require_member("rates")),
):
    """
    Where the hotel is, when it opens, and what its rules are.

    **None of this was editable through any route.** Address, coordinates,
    check-in times and the whole policy object were writable only by editing
    Postgres by hand — which is why nine of ten live hotels have no address, no
    map pin and empty policies, and therefore why their AI hands a guest over
    rather than answering "what's your cancellation policy?" and their website
    cannot be submitted for indexing.

    `exclude_unset` again: absent means unchanged, empty means cleared. The
    policy object is merged key by key over a known set — see
    `GUEST_POLICY_KEYS` — because it also carries the commercial settings the
    pricing engine reads, and a policies screen has no business dropping a
    hotel's discount ceiling.
    """
    row = await onboarding_service.update_property(
        property_id=property_id,
        fields={
            {
                "propertyType": "property_type",
                "totalRooms": "total_rooms",
                "checkInTime": "check_in_time",
                "checkOutTime": "check_out_time",
            }.get(key, key): value
            for key, value in body.model_dump(exclude_unset=True).items()
        },
    )
    await seo_ping.hotel_changed(row.get("slug"))
    return PropertyOut.from_row(row)


@router.post("/properties/{property_id}/locate", response_model=PropertyLocationOut)
@limiter.limit("30/hour")
async def locate_property(
    request: Request,
    property_id: str,
    member=Depends(require_member("rates")),
):
    """
    Find this hotel on a map, from the address it has already given us.

    Proposes; does not save. The owner accepts the match through the PATCH
    above, which is the same review-then-approve shape the brochure importer
    uses and for the same reason — one confident geocoding mistake would put a
    hotel in the wrong town on its own website and in every distance its AI
    quotes afterwards.
    """
    return PropertyLocationOut(**await onboarding_service.locate(property_id))


@router.get("/properties/{property_id}/readiness", response_model=ReadinessOut)
@limiter.limit("120/minute")
async def get_readiness(
    request: Request, property_id: str, member=Depends(require_member()),
):
    """
    What this hotel cannot do yet, and why.

    Named gaps rather than a score. "You are at 62%" tells an owner nothing
    they can act on; "your AI can't tell guests where you are" is one tap from
    being fixed.
    """
    return ReadinessOut(**await onboarding_service.readiness(property_id))


# What a hotel's own material may arrive as.
#
# PDFs and images both, because a hotel's rate sheet is whichever of the two
# somebody had to hand — a designed brochure or a photograph of a laminated
# card at the desk. Gemini reads both natively, so neither needs converting.
_EXTRACT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/plain",
}
_EXTRACT_MAX_BYTES = 12 * 1024 * 1024


def _extract_out(draft: dict) -> HotelExtractOut:
    return HotelExtractOut(
        confidence=draft["confidence"],
        hotel=ExtractedHotel(**draft["hotel"]),
        roomTypes=[ExtractedRoomType(**r) for r in draft["roomTypes"]],
        found=draft.get("found") or [],
        droppedRooms=draft.get("droppedRooms") or 0,
    )


@router.post("/properties/{property_id}/extract/text", response_model=HotelExtractOut)
@limiter.limit("30/hour")
async def extract_hotel_from_text(
    request: Request,
    property_id: str,
    body: HotelExtractRequest,
    member=Depends(require_member("rates")),
):
    """
    Read pasted material — a website page, a rate email — into a draft.

    **Writes nothing.** The owner reads what it found, corrects what is wrong,
    and creates the rooms they approve through the ordinary route. That
    separation is the whole safety argument: a model that misreads "₹4,200 for
    two nights" as a nightly rate has proposed a mistake, not made one.
    """
    try:
        draft = await pms_extract.extract_hotel(text=body.text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[pms] extraction failed for %s", property_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "We couldn't read that just now. Please try again, or add the rooms yourself.",
        ) from exc
    return _extract_out(draft)


@router.post("/properties/{property_id}/extract/file", response_model=HotelExtractOut)
@limiter.limit("30/hour")
async def extract_hotel_from_file(
    request: Request,
    property_id: str,
    file: UploadFile = File(...),
    member=Depends(require_member("rates")),
):
    """
    The same, from a brochure or a photograph of a rate card.

    Not stored. The bytes are read into the model call and dropped — a document
    a hotel sends to have read is not a document they asked us to keep, and the
    only thing worth persisting out of it is the draft they approve.
    """
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _EXTRACT_TYPES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Send a PDF, a photo of your rate card, or paste the text.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file received.")
    if len(data) > _EXTRACT_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "That file is over 12 MB. Please send a smaller one.",
        )

    try:
        if mime == "text/plain":
            draft = await pms_extract.extract_hotel(text=data.decode("utf-8", "replace"))
        else:
            draft = await pms_extract.extract_hotel(file_bytes=data, mime_type=mime)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[pms] extraction failed for %s", property_id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "We couldn't read that just now. Please try again, or add the rooms yourself.",
        ) from exc
    return _extract_out(draft)


# What a hotel may upload, and how much of it.
#
# Deliberately the same four types and the same 5 MB ceiling as the marketplace
# uploader: a hotelier photographing a room on a phone produces a 3–4 MB JPEG,
# and anything stricter would reject the ordinary case.
_MEDIA_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/avif": "avif",
}
_MEDIA_MAX_BYTES = 5 * 1024 * 1024
_MEDIA_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.post(
    "/properties/{property_id}/media", response_model=MediaUploadOut, status_code=201
)
@limiter.limit("240/hour")
async def upload_property_media(
    request: Request,
    property_id: str,
    file: UploadFile = File(...),
    folder: str = Form(default="rooms"),
    member=Depends(require_member("rates")),
):
    """
    A photo, stored, and a URL that works.

    **Why this had to exist before the photo tool could be useful.** The room
    form asked for photo *links* — "paste them from your website, or Instagram"
    — because StayOS had no image storage, and every hotel on it responded by
    entering none. Zero photos across ten properties. An AI employee that has
    been taught to send room photos and has none to send is the same product it
    was before, with a better excuse.

    Server-side rather than a browser→bucket upload, for the reason the
    partner-media incident established: Storage did not honour the project's
    ES256 JWT and fell back to `anon`, 403-ing authenticated uploads. Routing
    through here removes that failure mode and keeps the storage credential in
    the backend, which is the point of having one.

    **The path is built here and the property id comes from the verified
    membership path parameter, never from the body.** `folder` is a label
    inside a path we construct, stripped of anything that could add a segment or
    climb out of this hotel's prefix — a hotel cannot write into another hotel's
    photos whatever it sends.
    """
    mime = (file.content_type or "").split(";")[0].strip().lower()
    ext = _MEDIA_TYPES.get(mime)
    if not ext:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Please upload a JPG, PNG, WebP or AVIF image.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file received.")
    if len(data) > _MEDIA_MAX_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "That image is over 5 MB. Please upload a smaller one.",
        )

    # Checked before the file is read rather than discovered inside the client.
    #
    # StayOS is a separate deployment from the marketplace and does not inherit
    # its environment: `GCS_BUCKET_NAME` and the service account are set on one
    # and were not on the other. Without this, a hotelier uploading their first
    # room photo got "that upload did not go through" — which reads as their
    # connection, sends them to try again, and never mentions the one fact that
    # would let anybody fix it.
    if not settings.gcs_bucket_name:
        logger.error(
            "[pms] photo upload attempted with no GCS_BUCKET_NAME configured; "
            "StayOS needs GCS_BUCKET_NAME and GOOGLE_SERVICE_ACCOUNT_JSON in "
            "its environment",
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Photo uploads are not switched on for this hotel yet. "
            "We have been told — please try again shortly.",
        )

    safe_folder = re.sub(r"[^a-z0-9-]", "", folder or "", flags=re.I)[:32] or "rooms"
    path = f"stayos/{property_id}/{safe_folder}/{uuid.uuid4()}.{ext}"

    try:
        # to_thread: the GCS client is blocking and a 5 MB PUT would otherwise
        # sit on the event loop for the duration of the upload.
        public = await asyncio.to_thread(
            gcs.upload_bytes, path, data, mime, True, _MEDIA_CACHE_CONTROL
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[pms] media upload failed for %s", property_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "That upload did not go through. Please try again.",
        ) from exc

    return MediaUploadOut(url=public, path=path)


@router.post("/properties/{property_id}/rate-plans", response_model=RatePlanOut, status_code=201)
@limiter.limit("60/hour")
async def create_rate_plan(
    request: Request,
    property_id: str,
    body: RatePlanCreateRequest,
    member=Depends(require_member("rates")),
):
    """What one night costs. Rupees in, paise stored — the conversion is here."""
    row = await onboarding_service.add_rate_plan(
        property_id=property_id,
        partner_id=member.partner_id,
        room_type_id=body.roomTypeId,
        price_paise=body.priceRupees * 100,
        name=body.name,
        meal_plan=body.mealPlan,
        audience=body.audience,
        code=body.code,
    )
    return RatePlanOut.from_row(row)


@router.post("/properties/{property_id}/rooms", response_model=list[RoomOut], status_code=201)
@limiter.limit("60/hour")
async def create_rooms(
    request: Request,
    property_id: str,
    body: RoomsCreateRequest,
    member=Depends(require_member("rates")),
):
    """The physical rooms, by number. Optional — inventory comes from the type."""
    rows = await onboarding_service.add_rooms(
        property_id=property_id,
        partner_id=member.partner_id,
        room_type_id=body.roomTypeId,
        numbers=body.numbers,
    )
    return [RoomOut.from_row(r) for r in rows]


@router.post("/properties/{property_id}/activate", response_model=OnboardingOut)
@limiter.limit("30/hour")
async def activate_property(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    Go live.

    Refuses while anything required is missing, which is what keeps a hotel with
    no rate out of search results instead of listing it at nothing.
    """
    return await onboarding_service.activate(property_id)


# ── availability ─────────────────────────────────────────────────────────────

def _stay_option_out(option) -> StayOptionOut:
    """
    One room type, priced, on the wire.

    Shared by the staff route and the agent portal because they return the same
    thing — the difference between them is which audience's plans the service
    was asked for, and that decision is made before this is reached. Two copies
    of this projection is how one of them ends up omitting a field the other
    added.
    """
    return StayOptionOut(
        roomTypeId=option.room_type_id,
        roomTypeCode=option.room_type_code,
        roomTypeName=option.room_type_name,
        unitsLeft=option.units_left,
        maxOccupancy=option.max_occupancy,
        ratePlans=[
            RatePlanOptionOut(
                ratePlanId=p.rate_plan_id,
                code=p.code,
                name=p.name,
                mealPlan=p.meal_plan,
                audience=p.audience,
                refundable=p.refundable,
                inclusions=p.inclusions,
                totalPaise=p.total_paise,
                perNightPaise=p.per_night_paise,
                minStayViolation=p.min_stay_violation,
                closed=p.closed,
            )
            for p in option.rate_plans
        ],
    )


@router.get("/properties/{property_id}/availability", response_model=list[StayOptionOut])
@limiter.limit("120/minute")
async def get_availability(
    request: Request,
    property_id: str,
    check_in: date = Query(..., alias="checkIn"),
    check_out: date = Query(..., alias="checkOut"),
    units: int = Query(1, ge=1, le=20),
    audience: str | None = Query(None),
    member=Depends(require_member()),
):
    """
    What this property can sell for these dates.

    `audience` is honoured but not trusted to widen access: the front desk may
    legitimately see agent rates, because it books on behalf of agents. The
    public booking engine and the agent portal are separate endpoints with their
    own audience decided server-side, not this one with a query parameter.
    """
    options = await availability_service.stay_options(
        property_id, check_in, check_out, units=units, audience=audience
    )
    return [_stay_option_out(o) for o in options]


# ── sync ─────────────────────────────────────────────────────────────────────

# Property-scoped in the path, like every other route here, rather than
# `/pms/sync?propertyId=`. `require_member` resolves the property from the path
# parameter, and giving one endpoint a second way to name its property means a
# second way to get that check wrong.
@router.get("/properties/{property_id}/sync", response_model=SyncOut)
@limiter.limit("60/minute")
async def sync(
    request: Request,
    property_id: str,
    since: datetime | None = Query(None),
    member=Depends(require_member()),
):
    """
    Everything that changed for this property since a cursor — including what
    was deleted.

    The deletions are the point. The PWA refreshes IndexedDB by merging, and a
    merge can add or update but never remove, so a reservation deleted on the
    server would stay on an offline arrivals list indefinitely. Absence from a
    normal list response cannot stand in for deletion — that response is
    filtered and windowed, and half the rows are legitimately missing from it.
    Only the server knows the difference, so only the server can say.

    `fullResync` is set when this answer cannot be trusted to be complete: no
    cursor, a truncated page, or a cursor older than the oldest deletion still
    on record. The client's job then is to discard its local copy, not to merge
    what it got. A sync that cannot admit it lost track will eventually hand
    back a confident, wrong answer instead.

    `serverTime` is what to send as the next `since` — the server's clock, so a
    desk PC that is running fast cannot skip a window of changes.
    """
    changes = await sync_service.changes_since(
        property_id, since.isoformat() if since else None
    )
    return SyncOut(
        serverTime=changes["serverTime"],
        fullResync=changes["fullResync"],
        reservations=[ReservationOut.from_row(r) for r in changes["reservations"]],
        rooms=[RoomOut.from_row(r) for r in changes["rooms"]],
        tasks=[TaskOut.from_row(r) for r in changes["tasks"]],
        folioEntries=[FolioEntryOut.from_row(r) for r in changes["folioEntries"]],
        deleted=changes["deleted"],
    )


# ── calendar (ARI) ───────────────────────────────────────────────────────────
#
# Reads are open to any member: a receptionist needs to see what is left tonight.
# Writes need `rates`, which is the permission a manager has and a receptionist
# does not — pricing the property is not a front-desk decision.

@router.get("/properties/{property_id}/inventory", response_model=list[InventoryNightOut])
@limiter.limit("120/minute")
async def get_inventory(
    request: Request,
    property_id: str,
    from_date: date = Query(..., alias="from"),
    days: int = Query(30, ge=1, le=120),
    member=Depends(require_member()),
):
    """Per-night stock for the calendar grid, with sparse inheritance resolved."""
    return await ari_service.get_inventory(property_id, from_date, days)


@router.get("/properties/{property_id}/rates", response_model=list[RateNightOut])
@limiter.limit("120/minute")
async def get_rates(
    request: Request,
    property_id: str,
    from_date: date = Query(..., alias="from"),
    days: int = Query(30, ge=1, le=120),
    member=Depends(require_member()),
):
    """Per-night price and restrictions, per rate plan."""
    return await ari_service.get_rates(property_id, from_date, days)


@router.patch("/properties/{property_id}/inventory", response_model=AriEditOut)
@limiter.limit("60/minute")
async def edit_inventory(
    request: Request,
    property_id: str,
    body: InventoryEditRequest,
    member=Depends(require_member("rates")),
):
    """
    Change stock over a date range.

    One call for the whole range rather than one per night: a half-applied
    "block the last week of December" leaves a property with a hole nobody
    typed, and thirty round trips from a browser is thirty chances to be
    interrupted. Cutting stock below what is already sold is refused with the
    night named, never clamped.
    """
    nights = await ari_service.set_inventory(
        property_id,
        room_type_id=body.roomTypeId,
        from_date=body.fromDate,
        to_date=body.toDate,
        units_total=body.unitsTotal,
        units_blocked=body.unitsBlocked,
        stop_sell=body.stopSell,
        oversell=body.oversellAllowance,
    )
    return AriEditOut(nights=nights)


@router.patch("/properties/{property_id}/rates", response_model=AriEditOut)
@limiter.limit("60/minute")
async def edit_rates(
    request: Request,
    property_id: str,
    body: RateEditRequest,
    member=Depends(require_member("rates")),
):
    """Change price and restrictions over a date range. Null means leave alone."""
    nights = await ari_service.set_rates(
        property_id,
        rate_plan_id=body.ratePlanId,
        from_date=body.fromDate,
        to_date=body.toDate,
        price_paise=body.pricePaise,
        min_stay=body.minStay,
        max_stay=body.maxStay,
        closed_to_arrival=body.closedToArrival,
        closed_to_departure=body.closedToDeparture,
        closed=body.closed,
    )
    return AriEditOut(nights=nights)


# ── quotes ───────────────────────────────────────────────────────────────────

@router.post("/properties/{property_id}/quotes", response_model=QuoteOut)
@limiter.limit("60/minute")
async def create_quote(
    request: Request,
    property_id: str,
    body: QuoteRequest,
    member=Depends(require_member("frontdesk")),
):
    """
    Price a stay and hold the stock.

    The hold comes first and is why this is a POST rather than a GET: asking for
    a price takes a room off sale for fifteen minutes, which is a side effect
    and should look like one.
    """
    result = await booking_service.create_quote(
        property_id=property_id,
        rate_plan_id=body.ratePlanId,
        check_in=body.checkIn,
        check_out=body.checkOut,
        units=body.units,
        adults=body.adults,
        children=body.children,
        requested_discount_pct=body.discountPct,
        discount_reason=body.discountReason,
        issued_by="employee",
        issued_by_member=member.member_id,
        conversation_id=body.conversationId,
    )

    return QuoteOut(
        id=result.id,
        holdId=result.hold_id,
        propertyId=property_id,
        roomTypeId=result.room_type_id,
        ratePlanId=result.rate_plan_id,
        checkIn=result.check_in,
        checkOut=result.check_out,
        units=result.priced.units,
        adults=result.adults,
        children=result.children,
        nightly=[NightlyOut(date=n.date, pricePaise=n.price_paise) for n in result.priced.nightly],
        grossPaise=result.priced.gross_paise,
        discountPaise=result.priced.discount_paise,
        discountReason=body.discountReason if result.priced.discount_paise else None,
        taxPaise=result.priced.tax_paise,
        totalPaise=result.priced.total_paise,
        tokenPaise=result.priced.token_paise,
        balancePaise=result.priced.balance_paise,
        issuedBy="employee",
        expiresAt=result.expires_at,
        clampedFromPct=result.priced.clamped_from_pct,
    )


# ── reservations ─────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/reservations", response_model=list[ReservationOut])
@limiter.limit("120/minute")
async def list_reservations(
    request: Request,
    property_id: str,
    bucket: str | None = Query(None, description="arrivals | departures | inhouse"),
    search: str | None = Query(None, max_length=80),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=500),
    member=Depends(require_member()),
):
    """
    The reservation list, and the three front-desk buckets.

    The buckets, the search and the balance map all come from
    `services/pms/workspace.list_reservations` — the same function the Front
    Desk agent's `list_arrivals` calls, and the same status sets Today's counts
    use. One definition, so the headline number, the list underneath it and
    whatever the AI reads out are the same query with different projections.
    They are mirrored again in the PWA's offline cache (src/data/filters.ts),
    because a list that changes the moment a connection drops leaves the desk
    with no way to tell which version is right.
    """
    rows, balances = await workspace_service.list_reservations(
        property_id,
        bucket=bucket,
        search=search,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        limit=limit,
    )
    return [ReservationOut.from_row(r, folio_balance=balances.get(r["id"], 0)) for r in rows]


@router.post("/properties/{property_id}/reservations", response_model=ReservationOut)
@limiter.limit("60/minute")
async def create_reservation(
    request: Request,
    property_id: str,
    body: BookingRequest,
    member=Depends(require_member("frontdesk")),
):
    """Turn a quote into a booking. The quote is consumed, so a double-submit 409s."""
    row = await booking_service.create_reservation(
        property_id=property_id,
        quote_id=body.quoteId,
        guest_name=body.guestName,
        guest_phone=body.guestPhone,
        guest_email=body.guestEmail,
        source=body.source,
        token_paise=body.tokenPaise,
        payment_method=body.paymentMethod,
        notes=body.notes,
        created_by="employee",
        member_id=member.member_id,
        member_name=member.display_name,
    )
    # A desk booking can still be a creator's: the guest clicked a reel, then
    # rang the hotel. The chain is searched by the thread this reservation is
    # already linked to and by the guest's phone — never by anything the desk
    # typed, because "which creator gets paid" is not a field a receptionist
    # should be able to fill in.
    await creators_service.resolve_for_reservation(row)
    return ReservationOut.from_row(row)


@router.get("/properties/{property_id}/reservations/{reservation_id}",
            response_model=ReservationOut)
@limiter.limit("240/minute")
async def get_reservation(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member()),
):
    """
    One reservation, for the detail drawer.

    Scoped by property in the query rather than fetched by id alone: a
    reservation id from another hotel must 404 here exactly as a property id
    does, or the drawer becomes the enumeration oracle the rest of the API is
    careful not to be.
    """
    found = await workspace_service.get_reservation(property_id, reservation_id)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
    row, balance = found
    return ReservationOut.from_row(row, folio_balance=balance)


@router.post("/properties/{property_id}/reservations/{reservation_id}/check-in",
             response_model=ReservationOut)
@limiter.limit("120/minute")
async def check_in(
    request: Request,
    property_id: str,
    reservation_id: str,
    body: CheckInRequest,
    member=Depends(require_member("frontdesk")),
):
    row = await frontdesk_service.check_in(property_id, reservation_id, body.roomId, member)
    return ReservationOut.from_row(row)


@router.post("/properties/{property_id}/reservations/{reservation_id}/check-out",
             response_model=ReservationOut)
@limiter.limit("120/minute")
async def check_out(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("frontdesk")),
):
    row = await frontdesk_service.check_out(property_id, reservation_id, member)
    # A finished, settled stay is the moment a creator's commission becomes
    # payable. Almost every call finds no payout at all, which is the point:
    # the check is cheap and the alternative is a nightly job that has to
    # rediscover which stays moved.
    await creators_service.recalculate_for_reservation(reservation_id)
    return ReservationOut.from_row(row)


# ── folio ────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/reservations/{reservation_id}/folio",
            response_model=FolioOut)
@limiter.limit("120/minute")
async def get_folio(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("folio")),
):
    folio = await frontdesk_service.get_folio(property_id, reservation_id)
    return FolioOut(
        entries=[FolioEntryOut.from_row(r) for r in folio["entries"]],
        balancePaise=folio["balance_paise"],
    )


@router.post("/properties/{property_id}/reservations/{reservation_id}/folio",
             response_model=FolioEntryOut)
@limiter.limit("120/minute")
async def post_folio(
    request: Request,
    property_id: str,
    reservation_id: str,
    body: FolioPostRequest,
    member=Depends(require_member("folio")),
):
    """
    Append a charge or a payment.

    Idempotent on `idempotencyKey`, which the PMS generates before it queues the
    operation. That is the whole reason a receptionist can take a cash payment
    with the network down and not charge the guest twice when it drains.
    """
    row = await frontdesk_service.post_folio_entry(
        property_id,
        reservation_id,
        kind=body.kind,
        description=body.description,
        amount_paise=body.amountPaise,
        method=body.method,
        idempotency_key=body.idempotencyKey,
        member=member,
    )
    # A payment or a refund moves `paid_paise`, and a stay that is not paid in
    # full is not one a creator is owed for. Recomputed here rather than read
    # lazily so the owner's screen and the approval agree at the moment of
    # approval, not merely eventually.
    if body.kind in ("payment", "refund"):
        await creators_service.recalculate_for_reservation(reservation_id)
    return FolioEntryOut.from_row(row)


@router.post("/properties/{property_id}/reservations/{reservation_id}/payment-link",
             response_model=PaymentLinkOut)
@limiter.limit("30/minute")
async def create_payment_link(
    request: Request,
    property_id: str,
    reservation_id: str,
    body: PaymentLinkRequest,
    member=Depends(require_member("frontdesk")),
):
    """
    A link the desk can send a guest, on WhatsApp or by reading it out.

    'frontdesk' rather than 'folio'. Asking for money that is already owed is
    what a receptionist does all day; `folio` is the scope that *changes* what
    is owed, and a receptionist who can collect a balance still must not be
    able to discount one.

    The AI employee reaches the same service through `create_payment_link`, so
    a link sent by the agent and a link sent by the desk are the same row, the
    same clamp and the same settlement — there is no second way to be owed
    money.
    """
    try:
        link = await stay_payment.create_request(
            property_id=property_id,
            reservation_id=reservation_id,
            amount_paise=body.amountRupees * 100 if body.amountRupees else None,
        )
    except stay_payment.PaymentRequestError as exc:
        # Already a sentence for a person: "that booking is already paid in
        # full" is an answer the desk needs, not a stack trace.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return PaymentLinkOut(**link.as_dict())


@router.post("/properties/{property_id}/reservations/{reservation_id}/rotate-link",
             response_model=BookingLinkOut)
@limiter.limit("12/minute")
async def rotate_booking_link(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("frontdesk")),
):
    """
    Invalidate a booking's public link and issue a new one.

    This is what makes the token *revocable* rather than merely unguessable. A
    confirmation forwarded to the wrong number, a screenshot posted in a group,
    a phone lost between booking and arrival — in every one of those the old URL
    has to stop working, and here it does, from the moment this returns.

    Scoped by property in the update itself: a reservation id from another hotel
    changes nothing and 404s, the same as it does on the read.

    The guest is not told. Whoever rotated the link is the person who knows why,
    and re-sending it is a decision rather than a side effect.
    """
    rotated = await run_db(_rotate_booking_token, property_id, reservation_id)
    if not rotated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
    return BookingLinkOut(ref=rotated["ref"], url=messaging_service.booking_url(rotated))


def _rotate_booking_token(property_id: str, reservation_id: str) -> dict | None:
    rows = (
        get_db().table("reservations")
        .update({"public_token": secrets.token_hex(16)})
        .eq("id", reservation_id).eq("property_id", property_id)
        .execute().data or []
    )
    return rows[0] if rows else None


@router.post("/properties/{property_id}/night-audit", response_model=NightAuditOut)
@limiter.limit("6/minute")
async def run_night_audit(
    request: Request,
    property_id: str,
    body: NightAuditRequest | None = None,
    member=Depends(require_member("folio")),
):
    """
    Bring every in-house folio up to date.

    Normally pg_cron's job. Exposed because a property that was offline for two
    days needs a way to catch up, and because "press this and see" is a far
    better answer than telling a manager to wait until 01:30. Safe to press
    repeatedly — each night posts exactly once, enforced by a unique key on the
    ledger rather than by a check the caller could race.

    The business date is accepted so a property can close a day it has already
    passed. It cannot be used to post the future: nights are only ever posted up
    to the reservation's own departure.
    """
    return await frontdesk_service.run_night_audit(
        property_id, business_date=(body.businessDate.isoformat() if body and body.businessDate else None)
    )


# ── rooms and tasks ──────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/rooms", response_model=list[RoomOut])
@limiter.limit("120/minute")
async def list_rooms(
    request: Request,
    property_id: str,
    member=Depends(require_member()),
):
    """
    Every room in the property, with whoever is currently in it.

    Read by two screens that want opposite things: housekeeping wants the
    status board, and check-in wants the list of rooms it may actually assign.
    Both need the occupant, because a room can be clean and still unavailable —
    resolving that in the client would mean shipping the arrivals list to a
    housekeeper who has no business reading it.
    """
    rooms, occupants = await workspace_service.list_rooms(property_id)
    return [RoomOut.from_row(r, occupants.get(r["id"])) for r in rooms]


@router.post("/properties/{property_id}/rooms/{room_id}/status")
@limiter.limit("240/minute")
async def set_room_status(
    request: Request,
    property_id: str,
    room_id: str,
    body: RoomStatusRequest,
    member=Depends(require_member("housekeeping")),
):
    return await frontdesk_service.set_room_status(property_id, room_id, body.status)


@router.post("/properties/{property_id}/tasks", response_model=TaskOut)
@limiter.limit("120/minute")
async def create_task(
    request: Request,
    property_id: str,
    body: TaskCreateRequest,
    member=Depends(require_member("tasks")),
):
    """
    Raise a ticket, optionally linked to the guest message that caused it.

    The id is supplied by the caller. A retry, a double-tap, or an outbox replay
    all resolve to the same ticket rather than three.
    """
    row = await frontdesk_service.create_task(
        property_id,
        task_id=body.id,
        title=body.title,
        category=body.category,
        priority=body.priority,
        room_number=body.roomNumber,
        conversation_id=body.conversationId,
        source_channel=body.sourceChannel,
        guest_name=body.guestName,
        created_by=body.createdBy,
        member=member,
    )
    return TaskOut.from_row(row)


@router.post("/properties/{property_id}/tasks/{task_id}/status", response_model=TaskOut)
@limiter.limit("240/minute")
async def set_task_status(
    request: Request,
    property_id: str,
    task_id: str,
    body: TaskStatusRequest,
    member=Depends(require_member("tasks")),
):
    row = await frontdesk_service.set_task_status(property_id, task_id, body.status, member)
    return TaskOut.from_row(row)


# ─────────────────────────────────────────────────────────────────────────────
# Inbox
#
# The screens that were on seed data until slice 2. Everything here reads the
# same `conversations` and `conversation_messages` rows the WhatsApp webhook
# writes, so a receptionist watching the inbox sees a guest's message arrive and
# the agent's answer follow it — one thread, not two systems.
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/conversations", response_model=list[ConversationOut])
@limiter.limit("120/minute")
async def list_conversations(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=300),
    member=Depends(require_member("messaging")),
):
    def _read():
        db = get_db()
        q = (
            db.table("conversations")
            .select("*")
            .eq("property_id", property_id)
            .order("last_message_at", desc=True)
            .limit(limit)
        )
        if status_filter:
            q = q.eq("status", status_filter)
        rows = q.execute().data or []

        # One lookup for every assignee rather than one per row.
        member_ids = {r["assignee_member"] for r in rows if r.get("assignee_member")}
        names: dict[str, str] = {}
        if member_ids:
            members = (
                db.table("partner_members").select("id,profile_id")
                .in_("id", sorted(member_ids)).execute().data or []
            )
            profile_ids = {m["profile_id"] for m in members if m.get("profile_id")}
            profiles = (
                db.table("profiles").select("id,full_name")
                .in_("id", sorted(profile_ids)).execute().data or []
            ) if profile_ids else []
            by_profile = {p["id"]: p.get("full_name") for p in profiles}
            names = {m["id"]: by_profile.get(m["profile_id"]) for m in members}
        return rows, names

    rows, names = await run_db(_read)
    return [
        ConversationOut.from_row(
            r,
            window_open=messaging_service.window_open(r),
            assignee_name=names.get(r.get("assignee_member")),
        )
        for r in rows
    ]


@router.get(
    "/properties/{property_id}/conversations/{conversation_id}/messages",
    response_model=list[MessageOut],
)
@limiter.limit("240/minute")
async def list_messages(
    request: Request,
    property_id: str,
    conversation_id: str,
    member=Depends(require_member("messaging")),
):
    def _read():
        db = get_db()
        rows = (
            db.table("conversation_messages")
            .select("*")
            .eq("property_id", property_id)
            .eq("conversation_id", conversation_id)
            .order("sent_at")
            .limit(300)
            .execute()
            .data
            or []
        )
        # Opening a thread is reading it. The count is the receptionist's, not
        # the guest's, so it clears here rather than on a separate call nobody
        # would remember to make.
        if rows:
            db.table("conversations").update({"unread_count": 0}).eq(
                "id", conversation_id
            ).eq("property_id", property_id).execute()
        return rows

    return [MessageOut.from_row(r) for r in await run_db(_read)]


@router.post(
    "/properties/{property_id}/conversations/{conversation_id}/messages",
    response_model=MessageOut,
)
@limiter.limit("60/minute")
async def send_conversation_message(
    request: Request,
    property_id: str,
    conversation_id: str,
    body: SendMessageRequest,
    member=Depends(require_member("messaging")),
):
    """
    A staff reply.

    Goes through the same `messaging.send` the agent uses, so a receptionist
    typing outside the 24-hour window is refused for exactly the same reason and
    with exactly the same message. Meta's rule does not care who is typing, and
    a second code path would be a second chance to forget that.

    **Sending takes the conversation over.** A person typing a reply is a person
    handling it, and leaving the agent running underneath would let it answer
    the guest's next message over the top of someone mid-conversation. The
    explicit Take over button exists for picking a thread up without saying
    anything yet; this is the same decision made by doing rather than declaring.
    """
    conversation = await run_db(_load_conversation, property_id, conversation_id)
    if not conversation:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    try:
        sent = await messaging_service.send(
            conversation,
            body=body.body,
            author="staff",
            author_name=member.display_name,
            author_member=member.member_id,
        )
    except MessagingError as exc:
        # 409 rather than 500: the request was well formed and the refusal is a
        # fact about the conversation, which is what the desk needs to be told.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": exc.code, "detail": exc.detail},
        ) from exc

    if (conversation.get("handler") or "ai") != "human":
        await messaging_service.take_over(conversation_id, property_id, member)

    return MessageOut.from_row(sent.message)


@router.post(
    "/properties/{property_id}/conversations/{conversation_id}/handler",
    response_model=ConversationOut,
)
@limiter.limit("60/minute")
async def set_conversation_handler(
    request: Request,
    property_id: str,
    conversation_id: str,
    body: ConversationHandlerRequest,
    member=Depends(require_member("messaging")),
):
    """
    Take over, or hand back.

    Taking over stops the agent immediately — the pipeline checks `handler`
    before it opens a run, so nothing in flight can talk over the person who
    just picked it up. Handing back is deliberately a separate action: a
    conversation never drifts back to the AI because a human stopped typing.
    """
    if body.handler == "human":
        row = await messaging_service.take_over(conversation_id, property_id, member)
    else:
        row = await messaging_service.resume_ai(conversation_id, property_id)

    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return ConversationOut.from_row(row, window_open=messaging_service.window_open(row))


def _load_action(action_id: str, property_id: str) -> dict | None:
    rows = (
        get_db().table("agent_actions").select("*")
        .eq("id", action_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _load_conversation(property_id: str, conversation_id: str) -> dict | None:
    rows = (
        get_db().table("conversations").select("*")
        .eq("id", conversation_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────────────────
# Agents, and the approval queue
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/agents", response_model=list[AgentOut])
@limiter.limit("60/minute")
async def list_agents(
    request: Request,
    property_id: str,
    member=Depends(require_member()),
):
    """
    Every agent configured for this property.

    Any member may see *that* an agent exists and what it may do — a
    receptionist working beside it should be able to. The system prompt is not
    here: reading how the agent was told to behave needs the 'agents'
    permission, which is enforced by RLS on `agent_configs` and mirrored by
    simply not selecting it.
    """
    def _read():
        db = get_db()
        agents = (
            db.table("agents").select("*")
            .eq("property_id", property_id).order("agent_type").execute().data or []
        )
        if not agents:
            return [], {}, {}, {}, {}, {}

        ids = [a["id"] for a in agents]
        configs = {
            c["id"]: c for c in (
                db.table("agent_configs").select("id,model,provider,version")
                .in_("id", [a["active_config_id"] for a in agents if a.get("active_config_id")])
                .execute().data or []
            )
        } if any(a.get("active_config_id") for a in agents) else {}

        tools: dict[str, list[str]] = {}
        for t in (db.table("agent_tools").select("agent_id,tool_name,enabled")
                  .in_("agent_id", ids).execute().data or []):
            if t.get("enabled", True):
                tools.setdefault(t["agent_id"], []).append(t["tool_name"])

        perms: dict[str, list[str]] = {}
        for p in (db.table("agent_permissions").select("agent_id,scope")
                  .in_("agent_id", ids).execute().data or []):
            perms.setdefault(p["agent_id"], []).append(p["scope"])

        runs: dict[str, dict] = {}
        run_ids: dict[str, str] = {}
        for r in (db.table("agent_runs").select("id,agent_id,started_at,conversation_id")
                  .in_("agent_id", ids).order("started_at", desc=True)
                  .limit(500).execute().data or []):
            slot = runs.setdefault(r["agent_id"], {
                "last": None, "count": 0, "conversations": set(),
            })
            slot["last"] = slot["last"] or r["started_at"]
            slot["count"] += 1
            if r.get("conversation_id"):
                slot["conversations"].add(r["conversation_id"])
            run_ids[r["id"]] = r["agent_id"]

        # What each agent has cost the hotel, from the ledger's own figure.
        #
        # Summed from `usage_events.credits_charged` rather than derived from a
        # run count times a rate: the two agree today and would stop agreeing
        # the first time a rate changed mid-month, and the one that is right is
        # the one the hotel was actually billed.
        stats: dict[str, dict] = {}
        for e in (db.table("usage_events").select("agent_id,credits_charged")
                  .in_("agent_id", ids).limit(2000).execute().data or []):
            if not e.get("agent_id"):
                continue
            slot = stats.setdefault(e["agent_id"], {"credits": 0.0})
            slot["credits"] += float(e.get("credits_charged") or 0)

        for a in (db.table("agent_actions").select("agent_id,status")
                  .in_("agent_id", ids).limit(2000).execute().data or []):
            slot = stats.setdefault(a["agent_id"], {"credits": 0.0})
            slot["actions"] = slot.get("actions", 0) + 1
            if a["status"] == "awaiting_approval":
                slot["pending"] = slot.get("pending", 0) + 1

        return agents, configs, tools, perms, runs, stats

    agents, configs, tools, perms, runs, stats = await run_db(_read)
    out = []
    for a in agents:
        config = configs.get(a.get("active_config_id")) or {}
        activity = runs.get(a["id"]) or {}
        meter = stats.get(a["id"]) or {}
        # The *effective* model, not the configured one. Most properties leave
        # `agent_configs.model` empty and let the router choose by tier, and a
        # screen that showed "—" there would be telling a hotelier their agent
        # has no model when it plainly has one.
        effective = config.get("model") or model_router.select(
            agent_type=a["agent_type"], task="guest_conversation"
        ).model
        out.append(AgentOut(
            id=a["id"],
            propertyId=a["property_id"],
            agentType=a["agent_type"],
            name=a["name"],
            autonomyLevel=a["autonomy_level"],
            model=effective,
            provider=config.get("provider") or model_router.PROVIDER_VERTEX,
            isActive=a["status"] == "active",
            status=a["status"],
            channels=a.get("channels") or [],
            tools=sorted(tools.get(a["id"], [])),
            permissions=sorted(perms.get(a["id"], [])),
            lastRunAt=activity.get("last"),
            runsToday=activity.get("count", 0),
            creditsCharged=round(meter.get("credits", 0.0)),
            actionsCount=meter.get("actions", 0),
            conversationsCount=len(activity.get("conversations") or ()),
            pendingApprovals=meter.get("pending", 0),
        ))
    return out


@router.patch("/properties/{property_id}/agents/{agent_id}", response_model=AgentOut)
@limiter.limit("30/minute")
async def set_agent_autonomy(
    request: Request,
    property_id: str,
    agent_id: str,
    body: AgentAutonomyRequest,
    member=Depends(require_member("agents")),
):
    """
    Move the dial.

    The only control a hotel has over what its agent may do unattended, and the
    reason the tools are granted at full scope and held back by level — see
    services/ai/agents/guest.py. Requires the 'agents' permission; a receptionist
    cannot give the AI more authority than they have themselves.
    """
    def _write():
        rows = (
            get_db().table("agents")
            .update({"autonomy_level": body.autonomyLevel})
            .eq("id", agent_id).eq("property_id", property_id)
            .execute().data
        )
        return rows[0] if rows else None

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")

    # The agent row alone. The tool and permission lists come from `list_agents`
    # and the client refetches; duplicating those four queries here to return a
    # fully-populated object would be a second place they can drift.
    return AgentOut(
        id=row["id"], propertyId=row["property_id"], agentType=row["agent_type"],
        name=row["name"], autonomyLevel=row["autonomy_level"], model="", provider="",
        isActive=row["status"] == "active", status=row["status"],
        channels=row.get("channels") or [],
    )


@router.post("/properties/{property_id}/frontdesk/ask", response_model=ConsoleAskOut)
@limiter.limit("30/minute")
async def ask_frontdesk_agent(
    request: Request,
    property_id: str,
    body: ConsoleAskRequest,
    member=Depends(require_member("frontdesk")),
):
    """
    Ask the Front Desk agent something, and let it act.

    **The authorisation happens here and nowhere else that matters.**
    `require_member("frontdesk")` resolves the caller against this property from
    their JWT; everything downstream — the agent context, every tool, every PMS
    call — is bounded by the `property_id` that check produced. The request body
    carries a sentence and, optionally, a task name from a fixed list. It cannot
    carry a property, a partner, a guest or a reservation, so there is no field
    in it that a prompt injection could aim at. The model is not the boundary;
    this dependency is.

    An absent task is classified rather than defaulted — see
    `services/ai/classify`. That classifier is given no tools and never the
    property id, so the worst a message can talk it into is the wrong model.

    Requires 'frontdesk' rather than merely membership: a housekeeper has no
    business checking guests in, and the agent must not become a way to do
    something the person driving it could not do themselves. The agent's own
    scopes are a separate and narrower set — a manager with every permission
    still only gets what `agent_permissions` grants.
    """
    try:
        result, conversation = await ai_console.ask(
            property_id=property_id,
            member=member,
            message=body.message,
            task=body.task,
        )
    except ai_console.ConsoleRefused as exc:
        # 409: the request was well formed and the refusal is a fact about the
        # thread — a person has it, or no agent is configured. Both are things
        # the desk needs told, not a server error.
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "detail": exc.detail}
        ) from exc

    actions = [
        ConsoleActionOut(
            tool=a.tool, status=a.status, refusalReason=a.refusal_reason,
            resultType=a.result_type, resultId=a.result_id, latencyMs=a.latency_ms,
        )
        for a in result.actions
    ]
    return ConsoleAskOut(
        runId=result.run_id,
        conversationId=conversation["id"],
        status=result.status,
        reply=result.reply,
        handoff=result.handoff,
        actions=actions,
        pendingApproval=any(a.status == "awaiting_approval" for a in actions),
        tokensIn=result.tokens_in,
        tokensOut=result.tokens_out,
        latencyMs=result.latency_ms,
    )


@router.post("/properties/{property_id}/marketing/ask", response_model=ConsoleAskOut)
@limiter.limit("30/minute")
async def ask_marketing_agent(
    request: Request,
    property_id: str,
    body: ConsoleAskRequest,
    member=Depends(require_member("marketing")),
):
    """
    Ask the Marketing agent to plan something.

    The same console, the same runtime and the same conversation model as the
    front desk — what differs is one argument. Two agents share the `internal`
    channel now, and channel routing puts the front desk first, so the caller
    names which one it wants. That is the honest model for a staff surface:
    which agent you are talking to is a screen somebody opened.

    `require_member("marketing")` rather than mere membership. A receptionist
    has no business planning campaigns to the guest list, and the agent must not
    become a way to do something the person driving it could not do themselves.
    Nothing is sent from here either way: `execute_campaign` always requires a
    separate approval, which is the 'agents' permission and a different person's
    decision.
    """
    try:
        result, conversation = await ai_console.ask(
            property_id=property_id,
            member=member,
            message=body.message,
            task=body.task,
            agent_type="marketing",
        )
    except ai_console.ConsoleRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "detail": exc.detail}
        ) from exc

    actions = [
        ConsoleActionOut(
            tool=a.tool, status=a.status, refusalReason=a.refusal_reason,
            resultType=a.result_type, resultId=a.result_id, latencyMs=a.latency_ms,
        )
        for a in result.actions
    ]
    return ConsoleAskOut(
        runId=result.run_id,
        conversationId=conversation["id"],
        status=result.status,
        reply=result.reply,
        handoff=result.handoff,
        actions=actions,
        pendingApproval=any(a.status == "awaiting_approval" for a in actions),
        tokensIn=result.tokens_in,
        tokensOut=result.tokens_out,
        latencyMs=result.latency_ms,
    )


@router.get("/properties/{property_id}/campaigns", response_model=list[CampaignOut])
@limiter.limit("120/minute")
async def list_campaigns(
    request: Request,
    property_id: str,
    limit: int = Query(40, ge=1, le=200),
    member=Depends(require_member("marketing")),
):
    """
    Every campaign at this property, with the counts a screen needs.

    The counts come from `campaign_recipients` rather than from columns on the
    campaign — one place, computed the same way the run computes it, so a screen
    and a worker can never disagree about how many people were reached.
    """
    def _read():
        db = get_db()
        campaigns = (
            db.table("marketing_campaigns").select("*")
            .eq("property_id", property_id)
            .order("created_at", desc=True).limit(limit).execute().data or []
        )
        if not campaigns:
            return [], {}, {}

        ids = [c["id"] for c in campaigns]
        counts: dict[str, dict] = {}
        for row in (db.table("campaign_recipients").select("campaign_id,status,reason")
                    .in_("campaign_id", ids).limit(5000).execute().data or []):
            slot = counts.setdefault(row["campaign_id"], {"reasons": {}})
            slot[row["status"]] = slot.get(row["status"], 0) + 1
            if row.get("reason"):
                slot["reasons"][row["reason"]] = slot["reasons"].get(row["reason"], 0) + 1

        # What each campaign has cost the hotel, from the ledger's own figure —
        # the same argument as the per-agent credits on /agents: a count times a
        # rate stops being right the first time a rate changes mid-month.
        spend: dict[str, dict] = {}
        for e in (db.table("usage_events")
                  .select("external_ref,credits_charged,provider_cost_inr_micros")
                  .eq("usage_type", "marketing.campaign_send")
                  .in_("external_ref", ids).limit(5000).execute().data or []):
            slot = spend.setdefault(e["external_ref"], {"credits": 0.0, "cost": 0})
            slot["credits"] += float(e.get("credits_charged") or 0)
            slot["cost"] += int(e.get("provider_cost_inr_micros") or 0)

        return campaigns, counts, spend

    campaigns, counts, spend = await run_db(_read)
    out = []
    for c in campaigns:
        n = counts.get(c["id"]) or {}
        money = spend.get(c["id"]) or {}
        out.append(CampaignOut(
            id=c["id"],
            name=c["name"],
            campaignType=c["campaign_type"],
            channel=c["channel"],
            status=c["status"],
            offer=c.get("offer") or {},
            body=c.get("body"),
            templatePurpose=c.get("template_purpose"),
            version=c["version"],
            createdBy=c["created_by"],
            approvedBy=c.get("approved_by"),
            approvedAt=c.get("approved_at"),
            scheduledAt=c.get("scheduled_at"),
            startedAt=c.get("started_at"),
            completedAt=c.get("completed_at"),
            cancelledAt=c.get("cancelled_at"),
            outcomeReason=c.get("outcome_reason"),
            audienceSize=sum(v for k, v in n.items() if k != "reasons"),
            sent=n.get("sent", 0),
            skipped=n.get("skipped", 0),
            failed=n.get("failed", 0),
            blocked=n.get("blocked", 0),
            pending=n.get("pending", 0),
            skippedBy=n.get("reasons") or {},
            optedOut=(n.get("reasons") or {}).get("MARKETING_OPT_OUT", 0),
            creditsCharged=money.get("credits"),
            providerCostMicros=money.get("cost"),
            createdAt=c["created_at"],
        ))
    return out


@router.post("/properties/{property_id}/campaigns/{campaign_id}/cancel",
             response_model=CampaignOut)
@limiter.limit("30/minute")
async def cancel_campaign_endpoint(
    request: Request,
    property_id: str,
    campaign_id: str,
    member=Depends(require_member("marketing")),
):
    """
    Stop a campaign, including one that is part-way through.

    The running worker reloads the campaign before every recipient, so this
    takes effect within one message rather than at the end of the batch. It
    cannot un-send what has already gone, and the response says how many that
    was.
    """
    from app.services.marketing import campaigns as campaign_module

    try:
        await campaign_module.cancel(campaign_id, property_id, f"cancelled by {member.display_name}")
    except campaign_module.CampaignRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": exc.code, "detail": exc.detail}
        ) from exc

    campaigns = await list_campaigns(request, property_id, limit=200, member=member)
    for c in campaigns:
        if str(c.id) == str(campaign_id):
            return c
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")


@router.get("/properties/{property_id}/agent-actions", response_model=list[AgentActionOut])
@limiter.limit("120/minute")
async def list_agent_actions(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(60, ge=1, le=200),
    member=Depends(require_member()),
):
    def _read():
        db = get_db()
        q = (
            db.table("agent_actions").select("*")
            .eq("property_id", property_id)
            .order("created_at", desc=True).limit(limit)
        )
        if status_filter:
            q = q.eq("status", status_filter)
        rows = q.execute().data or []

        run_ids = {r["run_id"] for r in rows}
        runs = {
            r["id"]: r for r in (
                db.table("agent_runs").select("id,conversation_id")
                .in_("id", sorted(run_ids)).execute().data or []
            )
        } if run_ids else {}

        conv_ids = {r.get("conversation_id") for r in runs.values() if r.get("conversation_id")}
        convs = {
            c["id"]: c for c in (
                db.table("conversations").select("id,guest_name,guest_phone")
                .in_("id", sorted(conv_ids)).execute().data or []
            )
        } if conv_ids else {}

        # What each action cost, from the ledger rather than from a rate times a
        # guess. Keyed on `action_id`, which `usage.record_run` sets for the
        # per-tool events — so a read shows 0 because reads are priced at 0, not
        # because nobody looked.
        credits: dict[str, float] = {}
        if run_ids:
            for e in (db.table("usage_events").select("action_id,credits_charged")
                      .in_("run_id", sorted(run_ids)).limit(1000).execute().data or []):
                if e.get("action_id"):
                    credits[e["action_id"]] = (
                        credits.get(e["action_id"], 0.0) + float(e.get("credits_charged") or 0)
                    )
        return rows, runs, convs, credits

    rows, runs, convs, credits = await run_db(_read)
    out = []
    for r in rows:
        run = runs.get(r["run_id"]) or {}
        conv = convs.get(run.get("conversation_id")) or {}
        # The output is prose for a human to skim, not the raw payload. A
        # refusal shows its reason, which is the thing a manager reviewing the
        # queue actually needs.
        summary = r.get("refusal_reason") or ""
        if not summary and r.get("output"):
            summary = json.dumps(r["output"], default=str)[:400]
        out.append(AgentActionOut(
            id=r["id"], agentId=r["agent_id"], runId=r["run_id"],
            conversationId=run.get("conversation_id"),
            tool=r["tool"], input=r.get("input") or {}, output=summary,
            refusalReason=r.get("refusal_reason"),
            whyWaiting=approvals.why_waiting(r["tool"], r["status"]),
            resultType=r.get("result_type"), resultId=r.get("result_id"),
            status=r["status"], createdAt=r["created_at"],
            latencyMs=r.get("latency_ms") or 0,
            creditsCharged=round(credits.get(r["id"], 0.0)),
            guestName=conv.get("guest_name") or conv.get("guest_phone"),
            approvedBy=r.get("approved_by"), approvedAt=r.get("approved_at"),
        ))
    return out


@router.post(
    "/properties/{property_id}/agent-actions/{action_id}/decision",
    response_model=AgentActionOut,
)
@limiter.limit("60/minute")
async def decide_agent_action(
    request: Request,
    property_id: str,
    action_id: str,
    body: AgentDecisionRequest,
    member=Depends(require_member("agents")),
):
    """
    Approve or reject a proposed action — and, on approval, run it.

    Recorded server-side with who decided and when: a decision that only existed
    in a browser is not a decision anyone can audit, and this row is what an
    owner reads six months later when they ask why a discount went out.

    **Approving executes, after revalidating.** A proposal and its approval are
    separated by however long a manager takes to look at their phone, and in
    that time a room sells or a quote expires. `services/ai/approvals` re-derives
    every check the runtime made and adds the tool's own staleness test, then
    runs it. A stale proposal comes back as a 409 naming what changed, and the
    action is terminal — the recovery is a fresh proposal, not another press.
    """
    if body.decision == "rejected":
        def _reject():
            db = get_db()
            current = (
                db.table("agent_actions").select("*")
                .eq("id", action_id).eq("property_id", property_id)
                .limit(1).execute().data or []
            )
            if not current:
                return None, "not_found"
            if current[0]["status"] != "awaiting_approval":
                return current[0], "already_decided"
            rows = db.table("agent_actions").update({
                "status": "rejected",
                "refusal_reason": body.note or "Rejected by a manager",
                "approved_by": member.profile_id,
                "approved_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", action_id).execute().data
            return (rows[0] if rows else None), None

        row, problem = await run_db(_reject)
        if problem == "not_found" or row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
        if problem == "already_decided":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This action is already {row['status'].replace('_', ' ')}",
            )
    else:
        result = await approvals.execute_approved(
            action_id=action_id, property_id=property_id, member=member
        )
        if not result.ok:
            if result.code == "ACTION_NOT_FOUND":
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
            # 409, not 500. The request was fine; the hotel moved. The code and
            # the detail both go back so the desk is told what changed rather
            # than that something went wrong.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"code": result.code, "detail": result.detail},
            )

        found = await run_db(_load_action, action_id, property_id)
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
        row = found

    return AgentActionOut(
        id=row["id"], agentId=row["agent_id"], runId=row["run_id"],
        tool=row["tool"], input=row.get("input") or {},
        output=row.get("refusal_reason") or "",
        refusalReason=row.get("refusal_reason"),
        whyWaiting=approvals.why_waiting(row["tool"], row["status"]),
        resultType=row.get("result_type"), resultId=row.get("result_id"),
        status=row["status"], createdAt=row["created_at"],
        latencyMs=row.get("latency_ms") or 0,
        approvedBy=row.get("approved_by"), approvedAt=row.get("approved_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Usage — measurement, not billing
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/properties/{property_id}/usage", response_model=list[UsageMeterOut])
@limiter.limit("60/minute")
async def get_usage_meters(
    request: Request,
    property_id: str,
    days: int = Query(30, ge=1, le=365),
    member=Depends(require_member("reports")),
):
    """
    What this property has consumed, in units a hotel recognises.

    Grouped by usage type and labelled from `usage_types` — "AI conversation
    turns", "WhatsApp messages" — never tokens. Tokens are our cost problem, and
    a hotelier should not have to learn a new unit to read their own bill.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _read():
        db = get_db()
        events = (
            db.table("usage_events").select("usage_type,quantity")
            .eq("property_id", property_id).gte("created_at", since)
            .limit(5000).execute().data or []
        )
        types = {t["code"]: t for t in (db.table("usage_types").select("*").execute().data or [])}
        return events, types

    events, types = await run_db(_read)
    totals: dict[str, float] = {}
    for e in events:
        totals[e["usage_type"]] = totals.get(e["usage_type"], 0) + float(e["quantity"])

    return [_meter(code, used, types.get(code) or {}) for code, used in sorted(totals.items())]


def _meter(code: str, used: float, spec: dict) -> UsageMeterOut:
    return UsageMeterOut(
        code=code,
        label=spec.get("name") or code,
        unit=spec.get("unit") or "unit",
        used=used,
        billable=bool(spec.get("billable", True)),
        group=spec.get("display_group") or "Usage",
        category=spec.get("category") or "platform",
    )


@router.get("/properties/{property_id}/billing", response_model=BillingSummaryOut)
@limiter.limit("60/minute")
async def get_billing_summary(
    request: Request,
    property_id: str,
    member=Depends(require_member("billing")),
):
    """
    The usage meters, and how much room is left in them.

    **Usage against an allowance, never a credit count.** The hotel sees "AI
    Usage 1,240 / 2,000, resets 1 September"; the credits underneath are an
    internal monetization abstraction and surface only in the purchase flow and
    the ledger export.

    The period is the subscription's, not a rolling 30 days, because "resets on
    the 1st" is a promise and a rolling window is not one.

    Nothing here is a gate. It is what the banner reads; the decision to stop an
    AI reply is taken in the pipeline, against the same wallet, before a run
    opens.
    """
    # Resolved together, because a cached account whose wallet is missing is a
    # stale cache rather than an empty hotel — see `wallet_for_property`. This
    # screen showed a zero balance beside a full wallet until it did.
    account, state = await billing.wallet_for_property(property_id)
    if not account:
        return BillingSummaryOut(tier="unknown")

    def _read():
        db = get_db()
        sub = (
            db.table("subscriptions")
            .select("current_period_start,current_period_end,"
                    "subscription_plans(code,name,included_allowances,price_paise,billing_period)")
            .eq("billing_account_id", account)
            .in_("status", ["trialing", "active"])
            .order("current_period_start", desc=True)
            .limit(1).execute().data or []
        )
        types = {t["code"]: t for t in (db.table("usage_types").select("*").execute().data or [])}
        return (sub[0] if sub else None), types

    subscription, types = await run_db(_read)

    # A promotional period, read through the one function that decides whether a
    # hotel is inside one — see `active_transition` in migration 25. Kept out of
    # `subscriptions` on purpose: that table is the billing record, and a free
    # year written there is indistinguishable from a paid one later.
    def _transition():
        return get_db().rpc("active_transition",
                            {"p_property": property_id}).execute().data

    transition = await run_db(_transition) or {}

    period_start = _as_date(subscription and subscription.get("current_period_start"))
    period_end = _as_date(subscription and subscription.get("current_period_end"))
    plan = ((subscription or {}).get("subscription_plans") or {})
    allowances = plan.get("included_allowances") or {}

    since = (period_start.isoformat() if period_start
             else (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat())

    def _events():
        return (
            get_db().table("usage_events").select("usage_type,quantity")
            .eq("property_id", property_id).gte("created_at", since)
            .neq("status", "reversed")
            .limit(10000).execute().data or []
        )

    events = await run_db(_events)
    totals: dict[str, float] = {}
    for e in events:
        totals[e["usage_type"]] = totals.get(e["usage_type"], 0) + float(e["quantity"])

    # Grouped by what the hotel recognises, which is `display_group`. The
    # allowance is keyed by category, because an allowance is a commercial
    # promise about a family of things and the display grouping is a label.
    groups: dict[str, UsageGroupOut] = {}
    for code, used in sorted(totals.items()):
        spec = types.get(code) or {}
        if not spec.get("billable", True):
            continue
        meter = _meter(code, used, spec)
        group = groups.get(meter.group)
        if group is None:
            group = UsageGroupOut(
                group=meter.group, category=meter.category, used=0,
                allowance=_as_float(allowances.get(meter.category)), items=[],
            )
            groups[meter.group] = group
        group.used += used
        group.items.append(meter)

    return BillingSummaryOut(
        plan=plan.get("name") or plan.get("code"),
        pricePaise=int(plan.get("price_paise") or 0),
        billingPeriod=str(plan.get("billing_period") or "monthly"),
        periodStart=period_start,
        periodEnd=period_end,
        freeUntil=_as_date(transition.get("endsAt")) if transition else None,
        freeReason=transition.get("reason") if transition else None,
        freeDaysLeft=transition.get("daysLeft") if transition else None,
        tier=str(state.get("tier") or "unknown"),
        balance=int(state.get("balance") or 0),
        available=int(state.get("available") or 0),
        groups=list(groups.values()),
    )


@router.get("/properties/{property_id}/usage/events", response_model=list[UsageEventOut])
@limiter.limit("60/minute")
async def list_usage_events(
    request: Request,
    property_id: str,
    limit: int = Query(100, ge=1, le=500),
    member=Depends(require_member("reports")),
):
    """
    The line items behind the meters.

    What was consumed, when, by which agent, and what it was charged. **Not
    what it cost us** — `provider_cost_inr_micros` is not granted to the
    browser role at all, and it is not selected here either. That column is the
    margin on this hotel's account, and printing it beside their own usage
    would be a pricing disclosure rather than transparency.
    """
    def _read():
        db = get_db()
        rows = (
            db.table("usage_events")
            .select("id,usage_type,quantity,credits_charged,provider,model,"
                    "agent_id,conversation_id,status,created_at")
            .eq("property_id", property_id)
            .order("created_at", desc=True)
            .limit(limit).execute().data or []
        )
        types = {t["code"]: t for t in (db.table("usage_types").select("*").execute().data or [])}
        agents = {
            a["id"]: a for a in
            (db.table("agents").select("id,name,agent_type")
             .eq("property_id", property_id).execute().data or [])
        }
        return rows, types, agents

    rows, types, agents = await run_db(_read)
    return [
        UsageEventOut(
            id=r["id"],
            usageType=r["usage_type"],
            displayName=(types.get(r["usage_type"]) or {}).get("name") or r["usage_type"],
            group=(types.get(r["usage_type"]) or {}).get("display_group") or "Usage",
            agentName=(agents.get(r.get("agent_id")) or {}).get("name"),
            conversationId=r.get("conversation_id"),
            units=float(r.get("quantity") or 0),
            creditsCharged=int(r.get("credits_charged") or 0),
            provider=r.get("provider"),
            model=r.get("model"),
            status=r.get("status") or "charged",
            createdAt=r["created_at"],
        )
        for r in rows
    ]


def _as_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


# ── owner copilot and analytics ──────────────────────────────────────────────
#
# Two surfaces, and the order matters: the **structured** endpoints come first
# and the AI sits on top of them. `/analytics/*` answers without a model at all,
# so the Reports page and the owner's dashboard keep working when Vertex is
# down, when the wallet is empty, and when a hotel has turned its agents off.
# `/owner/ask` adds prose and recommendations to numbers that were already
# there. Building it the other way round — a screen whose only source is an
# agent's answer — is how analytics become an AI outage.

def _period_args(period: str, date_from: date | None, date_to: date | None):
    """Turn the query string into what `analytics` accepts, or 400."""
    from app.services.pms import analytics

    if period not in analytics.PERIOD_KEYS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unknown period '{period}'. One of: {', '.join(analytics.PERIOD_KEYS)}.",
        )
    if period == "custom" and not (date_from and date_to):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A custom period needs both 'from' and 'to'.",
        )
    return period, date_from, date_to


async def _report_or_400(property_id: str, period: str,
                         date_from: date | None, date_to: date | None) -> dict:
    from app.services.pms import analytics

    key, start, end = _period_args(period, date_from, date_to)
    try:
        return await analytics.report(property_id, key=key, date_from=start, date_to=end)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/properties/{property_id}/analytics", response_model=AnalyticsReportOut)
@limiter.limit("60/minute")
async def get_analytics(
    request: Request,
    property_id: str,
    period: str = Query("today"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    member=Depends(require_member("reports")),
):
    """
    The property's own figures for a period, with the comparable period beside
    them.

    `require_member("reports")` and nothing else decides who may read this. A
    receptionist cannot, an accounts user can, and a property id that is not
    theirs 404s in the dependency before this body runs — which is why no query
    parameter here names a property, a partner or an account.

    No model is involved. Every number comes from `services/pms/analytics`, the
    same function `/today` calls, so this endpoint and the Today screen cannot
    report different revenue for the same day.
    """
    return await _report_or_400(property_id, period, date_from, date_to)


@router.get("/properties/{property_id}/analytics/series", response_model=list[DailyPointOut])
@limiter.limit("60/minute")
async def get_analytics_series(
    request: Request,
    property_id: str,
    days: int = Query(30, ge=1, le=365),
    member=Depends(require_member("reports")),
):
    """Occupancy, ADR and RevPAR per night, ending on the property's today."""
    from app.services.pms import analytics

    return await analytics.day_series(property_id, days)


@router.get("/properties/{property_id}/recommendations",
            response_model=list[RecommendationOut])
@limiter.limit("60/minute")
async def list_recommendations(
    request: Request,
    property_id: str,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(40, ge=1, le=200),
    member=Depends(require_member("reports")),
):
    from app.services.pms import recommendations as recs

    if status_filter and status_filter not in recs.STATUSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Unknown status. One of: {', '.join(recs.STATUSES)}.")
    rows = await recs.list_for(property_id, status=status_filter, limit=limit)
    return [recs.project(r) for r in rows]


@router.post("/properties/{property_id}/recommendations/{recommendation_id}/status",
             response_model=RecommendationOut)
@limiter.limit("30/minute")
async def set_recommendation_status(
    request: Request,
    property_id: str,
    recommendation_id: str,
    body: RecommendationStatusRequest,
    member=Depends(require_member("reports")),
):
    """
    Mark a recommendation reviewed, dismissed or actioned.

    **'actioned' means a person says they did it.** Nothing here changes a rate,
    an allocation or a stop-sell, and no worker reads these rows looking for
    work. When an execution path exists it will run through the same approval
    and revalidation the other agents' writes already use; this endpoint will
    still only be moving a label.
    """
    from app.services.pms import recommendations as recs

    row = await recs.set_status(property_id, recommendation_id, body.status,
                                profile_id=member.profile_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recommendation not found")
    return recs.project(row)


@router.get("/properties/{property_id}/owner/overview", response_model=OwnerOverviewOut)
@limiter.limit("60/minute")
async def get_owner_overview(
    request: Request,
    property_id: str,
    period: str = Query("last_7"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    member=Depends(require_member("reports")),
):
    """
    Everything the owner's screen shows before anybody asks a question.

    Today and a chosen period, because they answer different questions — "how is
    tonight" and "how is the business" — and an owner opening the app wants both
    without picking one first.
    """
    from app.services.pms import clock
    from app.services.pms import recommendations as recs

    today_report = await _report_or_400(property_id, "today", None, None)
    period_report = await _report_or_400(property_id, period, date_from, date_to)
    rows = await recs.list_for(property_id, limit=20)

    def _name():
        found = (
            get_db().table("properties").select("name")
            .eq("id", property_id).limit(1).execute().data or []
        )
        return found[0]["name"] if found else None

    return OwnerOverviewOut(
        propertyId=property_id,
        propertyName=await run_db(_name),
        businessDate=await clock.abusiness_date_iso(property_id),
        timezone=await clock.atimezone_for(property_id),
        currency=today_report["currency"],
        today=today_report,
        period=period_report,
        recommendations=[recs.project(r) for r in rows],
    )


def _run_credits(run_id: str) -> int:
    """What one run cost the hotel, from the ledger's own figure."""
    rows = (
        get_db().table("usage_events").select("credits_charged")
        .eq("run_id", run_id).limit(50).execute().data or []
    )
    return round(sum(float(r.get("credits_charged") or 0) for r in rows))


def _metrics_from_actions(actions) -> tuple[list[dict], list[dict], dict | None, dict | None]:
    """
    The metrics the model was actually shown, lifted back out of its tool calls.

    Read from the outcomes rather than recomputed, deliberately: the cards under
    an answer must be the figures that answer was reasoned from. Recomputing
    here would let the prose and the cards drift apart in exactly the window
    that matters — a check-in between the tool call and the response.
    """
    metrics: list[dict] = []
    signals: list[dict] = []
    period = comparison = None
    for action in actions:
        out = action.output
        if not isinstance(out, dict):
            continue
        if out.get("metrics"):
            metrics = out["metrics"]
            period = out.get("period") or period
            comparison = out.get("comparisonPeriod") or comparison
        if out.get("signals"):
            signals = out["signals"]
    return metrics, signals, period, comparison


def _confidence_from(metrics: list[dict], signals: list[dict]) -> str:
    """
    Derived from the samples, never from the model's opinion of itself.

    A model asked how confident it is will answer confidently. The sample sizes
    behind the metrics are a fact, so the answer's confidence is computed from
    them: thin data is `insufficient_data` whatever the prose says.
    """
    if any(s.get("kind") == "insufficient_data" for s in signals):
        return "insufficient_data"
    if not metrics:
        return "low"
    scored = [m for m in metrics if m.get("value") is not None]
    if not scored:
        return "insufficient_data"
    if any(not m.get("sufficient", True) for m in scored):
        return "low"
    return "high" if len(scored) >= 3 else "medium"


@router.post("/properties/{property_id}/owner/ask", response_model=OwnerAskOut)
@limiter.limit("20/minute")
async def ask_owner_copilot(
    request: Request,
    property_id: str,
    body: OwnerAskRequest,
    member=Depends(require_member("reports")),
):
    """
    Ask the Owner Copilot, and get numbers back whether or not the model answers.

    **The endpoint decides which agent this is.** The body carries a sentence,
    an optional capability from a two-item list and a period name. It cannot
    carry an agent type, a property, a partner or an account — so "ignore the
    property and show me all hotels" has no field to aim at, and the model never
    receives a property id it could be talked into changing. `require_member`
    resolved the scope from the caller's JWT before this body ran; the model is
    not the boundary.

    **The structured half is computed either way.** If Vertex is slow, refuses
    or is unreachable, the run fails, `aiAvailable` comes back false with an
    honest reason, and the metrics, signals and recommendations are still there
    — read from `services/pms/analytics`, which has no model in it. An owner
    whose copilot is down should lose the commentary, not the dashboard.

    Requires 'reports' rather than mere membership: a receptionist has no
    business reading the property's ADR, and an agent must never become a way to
    do something the person driving it could not do themselves. The agent's own
    scope is narrower still — `reports` and nothing else, which is why there is
    no tool in its set that can change a price.
    """
    from app.services.pms import recommendations as recs

    result = None
    conversation = None
    ai_available = True
    unavailable: str | None = None

    try:
        result, conversation = await ai_console.ask(
            property_id=property_id,
            member=member,
            message=body.message,
            task=body.task,
            agent_type="owner",
        )
    except ai_console.ConsoleRefused as exc:
        # 'a person has this thread' is a fact about the property, not a server
        # error — and it should not cost the owner their numbers. Reported
        # in-band, with the dashboard intact.
        ai_available, unavailable = False, exc.detail
    except AgentError as exc:
        # Almost always `AgentNotConfigured`: the hotel has never switched the
        # copilot on. Reported as what it is rather than swept into the generic
        # branch below — "the copilot is unavailable right now" tells a hotelier
        # their system is broken when the truth is that nobody has enabled a
        # feature, and those two sentences send them to very different places.
        logger.info("owner copilot not available for %s: %s", property_id, exc.reason)
        ai_available = False
        unavailable = exc.reason
    except Exception:  # noqa: BLE001 — the boundary is the point
        logger.exception("owner copilot failed for property %s", property_id)
        ai_available = False
        unavailable = "The copilot is unavailable right now. The figures below are live."

    metrics = signals = []
    period = comparison = None
    actions: list[ConsoleActionOut] = []
    created: list[dict] = []

    if result is not None:
        metrics, signals, period, comparison = _metrics_from_actions(result.actions)
        actions = [
            ConsoleActionOut(
                tool=a.tool, status=a.status, refusalReason=a.refusal_reason,
                resultType=a.result_type, resultId=a.result_id, latencyMs=a.latency_ms,
            )
            for a in result.actions
        ]
        ids = [a.result_id for a in result.actions
               if a.result_type == "recommendation" and a.result_id and a.ok]
        if ids:
            def _read_created():
                return (
                    get_db().table("revenue_recommendations").select("*")
                    .in_("id", ids).execute().data or []
                )
            created = await run_db(_read_created)
        if result.status == "failed":
            ai_available = False
            unavailable = (
                "The copilot could not complete that. The figures below are live."
            )

    # The fallback, and the reason this endpoint is worth having over the
    # console: an answer with no tool call behind it — because the model failed,
    # or because it replied from the conversation without looking — still comes
    # back with the property's real numbers rather than an empty card.
    if not metrics:
        report = await _report_or_400(property_id, body.period, None, None)
        metrics, signals = report["metrics"], report["signals"]
        period, comparison = report["period"], report["comparisonPeriod"]
        currency = report["currency"]
    else:
        currency = next((m.get("currency") for m in metrics if m.get("currency")), "INR")

    if period is None:
        report = await _report_or_400(property_id, body.period, None, None)
        period, comparison = report["period"], report["comparisonPeriod"]

    return OwnerAskOut(
        runId=result.run_id if result else None,
        conversationId=conversation["id"] if conversation else None,
        status=result.status if result else "unavailable",
        summary=result.reply if result else None,
        aiAvailable=ai_available,
        aiUnavailableReason=unavailable,
        period=period,
        comparisonPeriod=comparison,
        currency=currency or "INR",
        metrics=metrics,
        signals=signals,
        recommendations=[recs.project(r) for r in created],
        evidence=actions,
        confidence=_confidence_from(metrics, signals),
        creditsCharged=await run_db(_run_credits, result.run_id) if result else 0,
        tokensIn=result.tokens_in if result else 0,
        tokensOut=result.tokens_out if result else 0,
        latencyMs=result.latency_ms if result else 0,
    )


# ── guests, services, members, margin ────────────────────────────────────────
#
# The four reads the PWA had no route for. Nothing new is being modelled here:
# every table, its RLS and its write path have existed since M1, and these are
# the queries the screens were always going to make.

@router.get("/properties/{property_id}/guests", response_model=list[GuestOut])
@limiter.limit("120/minute")
async def list_guests(
    request: Request,
    property_id: str,
    search: str | None = Query(None, max_length=80),
    limit: int = Query(200, ge=1, le=500),
    member=Depends(require_member("frontdesk")),
):
    """
    The hotel's guest list.

    `require_member("frontdesk")` rather than bare membership: a guest list is
    names, phone numbers and stay history, and a housekeeper has no reason to
    hold it. Accounts does not either — they read folios, not people.

    Search escapes `%` and `_` before it reaches `ilike`, for the same reason
    `plan_coupons` does: a Postgres wildcard typed into a search box should
    match a literal wildcard, not every guest in the building.
    """
    def _read():
        q = (
            get_db().table("guests")
            .select("id,property_id,name,phone,email,city,stays_count,"
                    "total_spend_paise,last_stay_at,vip,notes,marketing_opt_in")
            .eq("property_id", property_id)
        )
        if search:
            escaped = search.replace("%", r"\%").replace("_", r"\_").replace(",", " ")
            q = q.or_(f"name.ilike.%{escaped}%,phone.ilike.%{escaped}%,"
                      f"email.ilike.%{escaped}%")
        return q.order("last_stay_at", desc=True).limit(limit).execute().data or []

    return [GuestOut.from_row(r) for r in await run_db(_read)]


@router.get("/properties/{property_id}/services", response_model=list[ServiceOut])
@limiter.limit("120/minute")
async def list_services(
    request: Request,
    property_id: str,
    include_inactive: bool = Query(False),
    member=Depends(require_member()),
):
    """
    The paid extras this hotel sells.

    Any member may read them — a receptionist quoting a transfer needs the
    price, and the Guest agent already reads the same rows through
    `list_services`. Editing is a different permission; see below.

    Inactive services are excluded by default and available on request: the
    desk should not be able to sell something the hotel retired, but the
    settings screen has to show it in order to bring it back.
    """
    def _read():
        q = (
            get_db().table("services")
            .select("*").eq("property_id", property_id)
        )
        if not include_inactive:
            q = q.eq("is_active", True)
        return q.order("sort_order").limit(200).execute().data or []

    return [ServiceOut.from_row(r) for r in await run_db(_read)]


@router.post("/properties/{property_id}/services", response_model=ServiceOut)
@limiter.limit("60/minute")
async def upsert_service(
    request: Request,
    property_id: str,
    body: ServiceUpsertRequest,
    member=Depends(require_member("rates")),
):
    """
    Create or amend one service.

    `require_member("rates")` because this sets a price. A receptionist may sell
    a transfer and may not decide what it costs — the same line the rate
    calendar draws, and the same one the Sales agent's discount ceiling sits
    behind.

    The property comes from the path and is checked by the dependency; `id`
    from the body is scoped to it on update, so a service id belonging to
    another hotel updates nothing rather than being moved.
    """
    payload = {
        "property_id": property_id,
        "partner_id": member.partner_id,
        "name": body.name,
        "description": body.description or None,
        "price_paise": body.pricePaise,
        "category": body.category,
        "is_active": body.isActive,
        "daily_capacity": body.dailyCapacity,
        "lead_time_hours": body.leadTimeHours,
    }

    def _write():
        db = get_db()
        if body.id:
            rows = (
                db.table("services").update(payload)
                .eq("id", body.id).eq("property_id", property_id)
                .execute().data
            )
            return rows[0] if rows else None
        return db.table("services").insert(payload).execute().data[0]

    row = await run_db(_write)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    return ServiceOut.from_row(row)


@router.get("/properties/{property_id}/members", response_model=list[MemberOut])
@limiter.limit("60/minute")
async def list_members(
    request: Request,
    property_id: str,
    member=Depends(require_member("members")),
):
    """
    Who works here, and what each of them may do.

    Scoped to the property's partner and filtered by `property_ids`, so a
    manager of one hotel in a group does not get the group's staff list. The
    permission set is flattened from `ROLE_PERMS` plus per-member grants — the
    same derivation `/me` does, in the same place, rather than a second table
    the screen would have to interpret.
    """
    def _read():
        db = get_db()
        rows = (
            db.table("partner_members")
            .select("id,profile_id,role,property_ids,permissions,status")
            .eq("partner_id", member.partner_id)
            .order("role")
            .limit(200).execute().data or []
        )
        rows = [
            r for r in rows
            if not (r.get("property_ids") or []) or property_id in r["property_ids"]
        ]
        if not rows:
            return []
        profiles = {
            p["id"]: p for p in
            (db.table("profiles").select("id,email,full_name")
             .in_("id", [r["profile_id"] for r in rows]).execute().data or [])
        }
        return [(r, profiles.get(r["profile_id"], {})) for r in rows]

    out = []
    for row, profile in await run_db(_read):
        extra = {k for k, v in (row.get("permissions") or {}).items() if v is True}
        perms = ROLE_PERMS.get(row["role"], set()) | extra
        out.append(MemberOut(
            id=row["id"],
            name=profile.get("full_name") or profile.get("email") or "Member",
            email=profile.get("email") or "",
            role=row["role"],
            propertyIds=row.get("property_ids") or [],
            status=row.get("status") or "active",
            # An owner's '*' is expanded to nothing useful for a screen, so it
            # is passed through as the single token it is.
            permissions=sorted(perms),
        ))
    return out


@router.get("/properties/{property_id}/margin", response_model=list[MarginRowOut])
@limiter.limit("30/minute")
async def get_margin_rows(
    request: Request,
    property_id: str,
    days: int = Query(30, ge=1, le=365),
    member=Depends(require_member("billing")),
):
    """
    What each property cost us against what it paid — the internal margin view.

    **This is the one route in the PMS that returns provider cost.** Migration
    12 revokes `usage_events.provider_cost_inr_micros` from browser roles precisely so
    this number cannot be read from the client; it is served here, behind
    `require_member("billing")`, through the service-role client.

    Computed from `usage_events` rather than from `billing_period_summary`,
    deliberately: the summary is a materialised period and this screen is asked
    "how are we doing right now". The definitions are the same — credits
    charged is the revenue side, `cost_inr_micros` is the cost side, and neither
    is derived from the other.

    Scoped to the caller's partner. A margin screen that showed every hotel on
    the platform would be an operator tool wearing a hotelier's login.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    def _read():
        db = get_db()
        properties = (
            db.table("properties").select("id,name,city")
            .eq("partner_id", member.partner_id).execute().data or []
        )
        if not properties:
            return []
        ids = [p["id"] for p in properties]
        events = (
            db.table("usage_events")
            .select("property_id,usage_type,credits_charged,provider_cost_inr_micros")
            .in_("property_id", ids)
            .gte("created_at", since)
            .limit(20000).execute().data or []
        )
        return properties, events

    loaded = await run_db(_read)
    if not loaded:
        return []
    properties, events = loaded

    totals: dict[str, dict] = {}
    for e in events:
        slot = totals.setdefault(e["property_id"], {"credits": 0.0, "cost": 0, "by_type": {}})
        credits = float(e.get("credits_charged") or 0)
        slot["credits"] += credits
        slot["cost"] += int(e.get("provider_cost_inr_micros") or 0)
        slot["by_type"][e["usage_type"]] = slot["by_type"].get(e["usage_type"], 0.0) + credits

    out = []
    for p in properties:
        t = totals.get(p["id"], {"credits": 0.0, "cost": 0, "by_type": {}})
        # One credit is worth roughly ₹0.83 of subscription revenue — the
        # anchoring in migration 13. Stated once, here, rather than in the
        # client: a browser that prices a credit is a browser that can disagree
        # with the invoice.
        revenue_paise = round(t["credits"] * CREDIT_VALUE_PAISE)
        cost_paise = round(t["cost"] / 10_000)  # 1e-6 INR → paise
        top = max(t["by_type"].items(), key=lambda kv: kv[1], default=("", 0))[0]
        out.append(MarginRowOut(
            propertyId=p["id"],
            propertyName=p["name"],
            city=p.get("city") or "",
            plan="",
            usageCredits=round(t["credits"]),
            providerCostInrMicros=t["cost"],
            revenuePaise=revenue_paise,
            marginPaise=revenue_paise - cost_paise,
            topUsageType=top,
        ))
    return sorted(out, key=lambda r: -r.usageCredits)


# ── web check-in — the only unauthenticated routes in this file ──────────────
#
# Everything above resolves a member from a JWT before the handler runs. These
# three do not, because the person using them is a guest on their own phone who
# has never had an account and never will.
#
# **The booking reference is the credential.** That is a weaker control than a
# token and it is the one the product has, so the design compensates rather than
# pretends: the read is masked to what a guest needs to recognise their own
# booking, the write cannot reach anything but its own reservation, the rate
# limit is tight enough that guessing `TB-` plus six characters is not
# practical, and a wrong reference is answered identically to a right one that
# has been cancelled — 404, with no hint about which.

def _mask_phone(phone: str | None) -> str:
    """Last four digits. Enough to recognise, useless if leaked."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"••••{digits[-4:]}" if len(digits) >= 4 else ""


# 16 bytes as hex, and nothing else gets as far as the database. A token that
# is not this shape cannot match a row, so refusing it early costs a query
# rather than changing an answer.
_TOKEN_SHAPE = re.compile(r"[0-9a-f]{32}")

_PUBLIC_STAY_COLUMNS = (
    "id,ref,property_id,partner_id,guest_name,guest_phone,check_in,"
    "check_out,nights,units,adults,children,gross_paise,discount_paise,"
    "tax_paise,total_paise,paid_paise,status,room_type_id,rate_plan_id,"
    "cancellation_policy,created_at"
)


def _load_public_stay(ref: str) -> dict | None:
    """
    One reservation by reference, with the names a guest would recognise.

    Reads through the service-role client because the caller has no identity to
    check RLS against — which is exactly why nothing here is selected that a
    guest should not see. The projection *is* the access control.
    """
    return _load_public_reservation("ref", ref.strip(), case_insensitive=True)


def _load_public_booking(token: str) -> dict | None:
    """
    The same reservation, reached by its link rather than by its reference.

    Exact match, never `ilike`: a token is 32 hex characters that nobody types,
    and `ilike` treats `%` and `_` as wildcards — a lesson this codebase has
    already paid for once, on coupon codes.
    """
    return _load_public_reservation("public_token", token.strip(), with_receipt=True)


def _load_public_reservation(
    column: str, value: str, *, case_insensitive: bool = False,
    with_receipt: bool = False,
) -> dict | None:
    db = get_db()
    query = db.table("reservations").select(_PUBLIC_STAY_COLUMNS)
    query = query.ilike(column, value) if case_insensitive else query.eq(column, value)
    rows = query.limit(1).execute().data or []
    if not rows:
        return None
    stay = rows[0]

    prop = (
        db.table("properties")
        .select("name,city,slug,check_in_time,check_out_time,policies")
        .eq("id", stay["property_id"]).limit(1).execute().data or [{}]
    )[0]
    room_type = (
        db.table("room_types").select("name")
        .eq("id", stay["room_type_id"]).limit(1).execute().data or [{}]
    )[0]
    rate_plan = (
        db.table("rate_plans").select("name,inclusions")
        .eq("id", stay["rate_plan_id"]).limit(1).execute().data or [{}]
    )[0]
    registration = (
        db.table("guest_registrations").select("*")
        .eq("reservation_id", stay["id"]).limit(1).execute().data or []
    )
    # Read rather than `rpc("folio_balance")`: both public views need to know
    # not just the sum but whether the folio has been opened at all, and that is
    # the difference between a number a guest can act on and one that reads as
    # a mistake. See `_amount_due`.
    folio = (
        db.table("folio_entries")
        .select("posted_at,kind,description,method,amount_paise")
        .eq("reservation_id", stay["id"])
        .order("posted_at")
        .execute().data or []
    )
    return {
        "stay": stay, "property": prop, "room_type": room_type,
        "rate_plan": rate_plan,
        "balance": _amount_due(stay, folio),
        "registration": registration[0] if registration else None,
        "receipt": folio if with_receipt else [],
    }


def _amount_due(stay: dict, folio: list[dict]) -> int:
    """
    What this guest still owes, in the sense a guest means it.

    **Not `folio_balance`, which is what both public views used to show.** That
    RPC sums the folio, and a booking that has not started yet has a folio
    holding one thing: the deposit that was paid. Its balance is therefore
    *negative* — a guest with ₹43,008 left to pay was shown "₹-10,752", and the
    check-in page, which only renders a balance above zero, showed them nothing
    at all. Both were found by rendering a real four-night booking rather than
    by reading the code.

    So: once the folio has real charges on it the folio is the truth, because it
    carries the extras a guest ordered after arriving and the reservation total
    does not. Before then the reservation is the truth, and the sum that means
    something is total minus paid.

    A cancelled booking that was never charged owes nothing, whatever its total
    says.
    """
    charges = sum(int(e.get("amount_paise") or 0) for e in folio
                  if int(e.get("amount_paise") or 0) > 0)
    if charges > 0:
        return sum(int(e.get("amount_paise") or 0) for e in folio)
    if (stay.get("status") or "") in ("cancelled", "no_show"):
        return 0
    return max(0, int(stay.get("total_paise") or 0) - int(stay.get("paid_paise") or 0))


@router.get("/public/stays/{ref}", response_model=PublicStayOut)
@limiter.limit("10/minute")
async def get_public_stay(request: Request, ref: str):
    """
    A guest's own booking, by the reference on their confirmation.

    No authentication, so the rate limit is the control that matters: ten
    attempts a minute against a six-character reference space is not a
    practical enumeration, and a real guest never needs an eleventh.

    A cancelled stay 404s alongside a reference that never existed. Telling a
    stranger "that booking exists but is cancelled" is a free bit of information
    about somebody else's travel.
    """
    loaded = await run_db(_load_public_stay, ref)
    if not loaded or loaded["stay"]["status"] in ("cancelled", "no_show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that booking.")

    return PublicStayOut(
        **_public_stay_fields(loaded),
        registration=(GuestRegistrationOut.from_row(loaded["registration"])
                      if loaded["registration"] else None),
    )


def _public_stay_fields(loaded: dict) -> dict:
    """
    The projection both public views share.

    One function so the thin view and the linked view cannot drift into
    disagreeing about what a guest may see — the moment they do, the safe one
    stops being the reference for the other.
    """
    stay = loaded["stay"]
    return {
        "ref": stay["ref"],
        "propertyName": loaded["property"].get("name") or "",
        "propertyCity": loaded["property"].get("city") or "",
        "checkInTime": str(loaded["property"].get("check_in_time") or "")[:5],
        "checkOutTime": str(loaded["property"].get("check_out_time") or "")[:5],
        "guestName": stay.get("guest_name") or "Guest",
        "guestPhoneMasked": _mask_phone(stay.get("guest_phone")),
        "checkIn": stay["check_in"], "checkOut": stay["check_out"],
        "nights": stay["nights"], "units": stay["units"],
        "adults": stay.get("adults") or 0, "children": stay.get("children") or 0,
        "roomTypeName": loaded["room_type"].get("name") or "",
        "ratePlanName": loaded["rate_plan"].get("name") or "",
        "inclusions": loaded["rate_plan"].get("inclusions") or [],
        "totalPaise": stay.get("total_paise") or 0,
        "paidPaise": stay.get("paid_paise") or 0,
        "balancePaise": loaded["balance"],
        "status": stay["status"],
    }


@router.get("/public/bookings/{token}", response_model=BookingPageOut)
@limiter.limit("30/minute")
async def get_public_booking(request: Request, token: str):
    """
    A booking, by the link that was sent to the person who paid for it.

    **Why this exists next to `/public/stays/{ref}`.** The reference is six
    characters from a 32-symbol alphabet, produced by `random.choices` — good
    for reading aloud, wrong for a URL that opens somebody's booking. This path
    takes a 128-bit token instead, so it can show the price breakdown, the folio
    and the cancellation terms rather than the recognisably-thin projection a
    guessable reference has to settle for.

    **A cancelled booking is shown, not hidden.** The reference route 404s on
    one because confirming a stranger's cancelled travel is a free bit of
    information; here the caller is holding that booking's own credential, and
    a guest whose booking was cancelled has more right than anyone to read the
    page that says so.

    A bad token is a flat 404 with no hint about which half was wrong.
    """
    if not _TOKEN_SHAPE.fullmatch(token or ""):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that booking.")

    loaded = await run_db(_load_public_booking, token)
    if not loaded:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that booking.")

    stay = loaded["stay"]
    return BookingPageOut(
        **_public_stay_fields(loaded),
        grossPaise=stay.get("gross_paise") or 0,
        discountPaise=stay.get("discount_paise") or 0,
        taxPaise=stay.get("tax_paise") or 0,
        bookedAt=stay.get("created_at"),
        cancellationTerms=_cancellation_terms(loaded),
        propertySlug=loaded["property"].get("slug") or "",
        receipt=[
            ReceiptLineOut(
                postedAt=line["posted_at"],
                kind=line.get("kind") or "",
                description=line.get("description") or "",
                method=line.get("method") or "",
                amountPaise=int(line.get("amount_paise") or 0),
            )
            for line in loaded["receipt"]
        ],
        checkInUrl=f"{(settings.pms_app_url or '').rstrip('/')}/checkin/{stay['ref']}",
        registrationSubmittedAt=((loaded["registration"] or {}).get("submitted_at")),
    )


def _cancellation_terms(loaded: dict) -> str:
    """
    What this guest was told about cancelling, in one sentence.

    The reservation's own `cancellation_policy` snapshot comes first — terms at
    the time of booking beat terms as they stand today — but every row in
    production has left it at `{}`, so in practice this reads the sentence the
    hotelier wrote on the property. Nothing invents a default: a hotel that has
    written no policy produces an empty string, and the page says so.
    """
    snapshot = loaded["stay"].get("cancellation_policy")
    if isinstance(snapshot, dict):
        text = str(snapshot.get("cancellation") or snapshot.get("text") or "").strip()
        if text:
            return text
    policies = loaded["property"].get("policies")
    if isinstance(policies, dict):
        return str(policies.get("cancellation") or "").strip()
    return ""


@router.post("/public/stays/{ref}/check-in", response_model=GuestRegistrationOut)
@limiter.limit("6/minute")
async def submit_web_check_in(request: Request, ref: str, body: WebCheckInRequest):
    """
    File a registration against one booking, before arrival.

    **This does not check anybody in.** It records what the guest told us; the
    desk still runs `check_in`, which is where a room is assigned, the folio is
    opened and the stay's status moves. Conflating the two would let a stranger
    with a reference mark a room occupied.

    The reservation comes from the reference in the path and from nothing in the
    body — there is no `reservationId` field for a caller to point somewhere
    else. Re-submitting amends the guest's own row rather than creating a
    second, which is what correcting a typo means.
    """
    loaded = await run_db(_load_public_stay, ref)
    if not loaded or loaded["stay"]["status"] in ("cancelled", "no_show"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that booking.")

    stay = loaded["stay"]
    if stay["status"] == "checked_out":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This stay has already ended.")

    # The last four characters, and the rest is discarded here — before it
    # reaches the database, so there is no window in which it exists at rest.
    digits = "".join(c for c in body.idNumber if c.isalnum())
    payload = {
        "reservation_id": stay["id"],
        "property_id": stay["property_id"],
        "partner_id": stay["partner_id"],
        "full_name": body.fullName.strip(),
        "email": body.email.strip() or None,
        "phone": body.phone.strip() or None,
        "address": body.address.strip() or None,
        "city": body.city.strip() or None,
        "country": body.country.strip() or None,
        "id_type": body.idType,
        "id_last4": digits[-4:] if digits else None,
        "travelling_with": body.travellingWith,
        "estimated_arrival": body.estimatedArrival,
        "special_requests": (body.specialRequests or "").strip() or None,
        "source": "web",
    }

    def _write():
        return (
            get_db().table("guest_registrations")
            .upsert(payload, on_conflict="reservation_id")
            .execute().data[0]
        )

    return GuestRegistrationOut.from_row(await run_db(_write))


@router.get("/properties/{property_id}/reservations/{reservation_id}/registration",
            response_model=GuestRegistrationOut)
@limiter.limit("120/minute")
async def get_registration(
    request: Request,
    property_id: str,
    reservation_id: str,
    member=Depends(require_member("frontdesk")),
):
    """
    What the guest filled in, for the desk that is about to meet them.

    Authenticated and property-scoped, unlike the two above. Even here it is
    `id_last4` and nothing more — the full document was never received, so there
    is no version of this route that could return it.
    """
    def _read():
        rows = (
            get_db().table("guest_registrations").select("*")
            .eq("reservation_id", reservation_id)
            .eq("property_id", property_id)
            .limit(1).execute().data or []
        )
        return rows[0] if rows else None

    row = await run_db(_read)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "This guest has not checked in online.")
    return GuestRegistrationOut.from_row(row)


# ═════════════════════════════════════════════════════════════════════════════
# M5 — the public booking loop
#
#     /stay/[slug] ──► availability ──► quote ──► order ──► Razorpay
#                                                             │
#                            reservation ◄── confirm ◄────────┤
#                                        ◄── webhook ◄────────┘
#
# **Every one of these composes the services the front desk already uses.**
# Availability is `availability.stay_options`, the price is
# `booking.create_quote`, the reservation is `booking.create_reservation` —
# which already validates the quote, enforces its expiry, converts the hold and
# refuses a second submission. Nothing in this section prices a stay, counts a
# room or decrements inventory, and if it ever appears to, that is a bug.
#
# What this section owns is the boundary: resolving a slug instead of an id,
# refusing anything the browser tried to decide, and making the four possible
# orderings of "browser returns" and "webhook fires" converge on one booking.
# ═════════════════════════════════════════════════════════════════════════════

def _room_option(option, units: int) -> PublicRoomOptionOut:
    """
    One room type as the public sees it.

    Rate plans that are closed or violate the minimum stay are dropped rather
    than shown greyed out: a guest cannot act on them, and a price they are not
    allowed to take is a price they will ask about.
    """
    return PublicRoomOptionOut(
        roomTypeId=option.room_type_id,
        name=option.room_type_name,
        code=option.room_type_code,
        maxOccupancy=option.max_occupancy,
        unitsLeft=option.units_left,
        ratePlans=[
            RatePlanOptionOut(
                ratePlanId=p.rate_plan_id, code=p.code, name=p.name,
                mealPlan=p.meal_plan, audience="public", refundable=p.refundable,
                inclusions=p.inclusions, totalPaise=p.total_paise,
                perNightPaise=p.per_night_paise,
                minStayViolation=p.min_stay_violation, closed=p.closed,
            )
            for p in option.rate_plans
            if not p.closed and p.min_stay_violation is None
        ],
    )


@router.get("/public/hotels/{slug}/chat", response_model=WebChatOpenerOut)
@limiter.limit("120/minute")
async def get_public_chat_opener(request: Request, slug: str):
    """
    What the chat widget says before a visitor types.

    Server-side rather than hard-coded in the widget because the suggestions
    have to be true of *this* hotel: a homestay with no airport pickup should
    not be offering one as an opening prompt, and the widget has no way to know
    that. Only services the hotel has actually configured become suggestions.
    """
    prop = await public_booking.resolve_published(slug)

    def _services() -> list[str]:
        rows = (
            get_db()
            .table("services")
            .select("name,category")
            .eq("property_id", prop.id)
            .eq("is_active", True)
            .order("sort_order")
            .limit(3)
            .execute()
            .data
            or []
        )
        return [str(r["name"]) for r in rows if r.get("name")]

    names = await run_db(_services)

    suggestions = ["Do you have rooms tonight?", "What's included in the rate?"]
    if names:
        # Hotels name services for their own staff — "Airport pickup (Maharana
        # Pratap)", "Spa — 60 min". Lower-casing the whole string turned a
        # proper noun into "maharana pratap" and the parenthetical made the
        # question unreadable, so trim to the part before the bracket or dash
        # and only lower-case a leading word that is plainly not a name.
        label = re.split(r"[(\u2014-]", names[0])[0].strip()
        if label[:1].isupper() and not label.split(" ")[0].isupper():
            label = label[0].lower() + label[1:]
        suggestions.append(f"Do you offer {label}?")
    suggestions.append("Can I book a room?")

    return WebChatOpenerOut(greeting=web_chat.opening_line(prop.name), suggestions=suggestions)


@router.post("/public/hotels/{slug}/chat", response_model=WebChatOut)
@limiter.limit("30/minute")
async def post_public_chat(request: Request, slug: str, body: WebChatIn):
    """
    One turn with the hotel's AI employee, from its own website.

    Public and unauthenticated, because the person is a traveller who has not
    booked anything yet — asking them to sign in to ask a question is how the
    question does not get asked. What stands in for a session is the signed
    token in `body.session`; see `services/pms/web_chat`.

    The rate limit is per-IP and deliberately below the DM channels': a browser
    is the cheapest place to point a script at a model, and a real visitor types
    a few times a minute.
    """
    turn = await web_chat.ask(slug=slug, message=body.message, session=body.session)
    return WebChatOut(reply=turn.reply, session=turn.session, handedOff=turn.handed_off)


@router.get("/public/hotels/{slug}", response_model=PublicSiteOut)
@limiter.limit("60/minute")
async def get_public_hotel(request: Request, slug: str):
    """
    A published hotel's page.

    The content is the website's *published* snapshot, never its draft — a
    hotelier mid-edit is not publishing. The rooms and services come from the
    live PMS, so a new room type or a price change appears here with nothing to
    re-publish; the website holds no inventory of its own.
    """
    prop = await public_booking.resolve_published(slug)

    def _read():
        db = get_db()
        types = (
            db.table("room_types")
            .select("id,code,name,description,max_occupancy,standard_occupancy,"
                    "bed_type,amenities,photos,size_sqft")
            .eq("property_id", prop.id).eq("is_active", True)
            .order("sort_order").execute().data or []
        )
        services = (
            db.table("services")
            .select("id,name,description,category,price_paise,lead_time_hours,daily_capacity")
            .eq("property_id", prop.id).eq("is_active", True)
            .order("sort_order").limit(50).execute().data or []
        )
        # The cheapest public rate per room type — a "from" figure for a
        # brochure page, never a quote. `audience=public` matters: a b2b agent
        # rate is cheaper by design and putting it on a page a guest reads
        # would advertise a price no guest can have.
        plans = (
            db.table("rate_plans")
            .select("room_type_id,base_price_paise,meal_plan")
            .eq("property_id", prop.id).eq("is_active", True)
            .eq("audience", "public").execute().data or []
        )
        domain = (
            db.table("hotel_websites").select("domain,domain_status")
            .eq("property_id", prop.id).limit(1).execute().data or []
        )
        return types, services, plans, (domain[0] if domain else {})

    types, services, plans, site = await run_db(_read)

    cheapest: dict[str, dict] = {}
    for plan in plans:
        price = plan.get("base_price_paise")
        if price is None:
            continue
        current = cheapest.get(plan["room_type_id"])
        if current is None or price < current["base_price_paise"]:
            cheapest[plan["room_type_id"]] = plan
    return PublicSiteOut(
        slug=prop.slug, name=prop.name, city=prop.city, template=prop.template,
        currency=prop.currency, timezone=prop.timezone,
        checkInTime=prop.check_in_time, checkOutTime=prop.check_out_time,
        policies=prop.policies, content=prop.website,
        address=prop.address, state=prop.state, country=prop.country,
        pincode=prop.pincode, lat=prop.lat, lon=prop.lon,
        propertyType=prop.property_type, totalRooms=prop.total_rooms,
        updatedAt=prop.updated_at,
        # Only a *verified* domain. An unverified one is a hostname somebody
        # typed into a form, and pointing a canonical at it would hand the
        # page's ranking to an address that may not resolve.
        domain=(site.get("domain") if site.get("domain_status") == "verified" else None),
        services=[
            PublicServiceOut(
                id=s["id"], name=s["name"], description=s.get("description") or "",
                category=s["category"], pricePaise=s["price_paise"],
                leadTimeHours=s.get("lead_time_hours") or 0,
                dailyCapacity=s.get("daily_capacity"),
            ) for s in services
        ],
        roomTypes=[
            PublicRoomOptionOut(
                roomTypeId=t["id"], name=t["name"], code=t["code"],
                description=t.get("description"),
                maxOccupancy=t.get("max_occupancy") or 2,
                bedType=t.get("bed_type"), amenities=t.get("amenities") or [],
                # Nothing is claimed about availability here. This is the
                # brochure; the number comes from /availability with dates.
                unitsLeft=0,
                slug=onboarding_service.slugify(t["name"]) or t["code"].lower(),
                photos=t.get("photos") or [],
                sizeSqft=t.get("size_sqft"),
                standardOccupancy=t.get("standard_occupancy"),
                fromPricePaise=(cheapest.get(t["id"]) or {}).get("base_price_paise"),
                mealPlan=(cheapest.get(t["id"]) or {}).get("meal_plan"),
            ) for t in types
        ],
    )


@router.get("/public/hotels", response_model=list[PublicHotelIndexOut])
@limiter.limit("30/minute")
async def list_public_hotels(request: Request):
    """
    Every hotel whose website is published, with the counts a quality gate needs.

    **This is what makes the hotel pages discoverable at all.** A sitemap cannot
    enumerate URLs it has no way to list, and until this route existed the only
    entry point was `/public/hotels/{slug}` — reachable if you already knew the
    slug, which a crawler does not.

    It returns counts rather than content on purpose. The caller is deciding
    *which* hotels to submit for indexing, and that decision is "does this page
    have enough on it to be worth a search result" — a number of photos, a
    number of rooms, whether there is a description at all. Sending the content
    itself would make this the heaviest route in the system to answer a question
    that is really about volume.

    Unpublished and suspended hotels are absent, for the same reason
    `resolve_published` 404s them: a hotelier drafting a site has not agreed to
    anybody seeing it.
    """
    def _read():
        db = get_db()
        sites = (
            db.table("hotel_websites")
            .select("property_id,published,published_at,domain,domain_status")
            .eq("status", "published").limit(2000).execute().data or []
        )
        if not sites:
            return []
        ids = [s["property_id"] for s in sites]

        props = {
            p["id"]: p
            for p in (
                db.table("properties")
                .select("id,slug,name,city,status,lat,lon,address,policies,updated_at")
                .in_("id", ids).execute().data or []
            )
        }
        rooms = (
            db.table("room_types").select("property_id,photos,description")
            .in_("property_id", ids).eq("is_active", True).execute().data or []
        )

        by_property: dict[str, list[dict]] = {}
        for room in rooms:
            by_property.setdefault(room["property_id"], []).append(room)

        out = []
        for site in sites:
            prop = props.get(site["property_id"])
            if not prop or prop.get("status") != "active" or not prop.get("slug"):
                continue
            published = site.get("published") or {}
            mine = by_property.get(prop["id"], [])
            photos = sum(len(r.get("photos") or []) for r in mine)
            out.append({
                "slug": prop["slug"],
                "name": prop["name"],
                "city": prop.get("city"),
                "updatedAt": str(site.get("published_at") or prop.get("updated_at") or "") or None,
                "domain": (site.get("domain")
                           if site.get("domain_status") == "verified" else None),
                "roomTypes": len(mine),
                "roomsWithPhotos": sum(1 for r in mine if r.get("photos")),
                "photos": photos,
                "hasDescription": len(str(published.get("about") or "").strip()) >= 120,
                "hasPolicies": bool(prop.get("policies")),
                "hasLocation": prop.get("lat") is not None and prop.get("lon") is not None,
                "amenities": len(published.get("amenities") or []),
            })
        return out

    rows = await run_db(_read)
    return [PublicHotelIndexOut(**r) for r in rows]


@router.get("/public/hotels/{slug}/availability", response_model=PublicAvailabilityOut)
@limiter.limit("30/minute")
async def get_public_availability(
    request: Request,
    slug: str,
    check_in: date = Query(alias="from"),
    check_out: date = Query(alias="to"),
    units: int = Query(1, ge=1, le=5),
    adults: int = Query(2, ge=1, le=20),
    children: int = Query(0, ge=0, le=10),
):
    """
    Live availability, from the same function the front desk calls.

    `audience="public"` is the load-bearing argument: it is what stops a
    stranger being shown — or later quoted — the B2B contracted rate. The
    endpoint knows who is asking; the service does the filtering.
    """
    prop = await public_booking.resolve_published(slug)
    public_booking.validate_stay_dates(prop, check_in, check_out)

    options = await availability_service.stay_options(
        prop.id, check_in, check_out, units=units, audience="public",
    )
    rooms = [
        _room_option(o, units) for o in options
        if o.units_left >= units and o.max_occupancy >= adults + children
    ]
    rooms = [r for r in rooms if r.ratePlans]

    return PublicAvailabilityOut(
        checkIn=check_in, checkOut=check_out,
        nights=(check_out - check_in).days, units=units,
        currency=prop.currency, rooms=rooms, soldOut=not rooms,
    )


@router.post("/public/hotels/{slug}/quote", response_model=PublicQuoteOut)
@limiter.limit("20/minute")
async def create_public_quote(request: Request, slug: str, body: PublicQuoteRequest):
    """
    Price a stay and hold the rooms.

    `booking.create_quote` does all of it — the GST slab per night, the
    discount clamp, the token rule from `properties.settings`, the hold, the
    expiry. This endpoint supplies `audience="public"`, which is what makes a
    rate plan id the guest was never offered unusable even if they guess one.
    """
    prop = await public_booking.resolve_published(slug)
    public_booking.validate_stay_dates(prop, body.checkIn, body.checkOut)

    quote = await booking_service.create_quote(
        property_id=prop.id,
        rate_plan_id=body.ratePlanId,
        check_in=body.checkIn, check_out=body.checkOut,
        units=body.units, adults=body.adults, children=body.children,
        # No discount from a public caller. Negotiation is the Sales agent's,
        # behind an authenticated surface with a ceiling on it.
        requested_discount_pct=0,
        issued_by="booking_engine",
        audience="public",
    )

    def _names():
        db = get_db()
        rt = (db.table("room_types").select("name")
              .eq("id", quote.room_type_id).limit(1).execute().data or [{}])[0]
        rp = (db.table("rate_plans").select("name,inclusions")
              .eq("id", quote.rate_plan_id).limit(1).execute().data or [{}])[0]
        return rt, rp

    room_type, rate_plan = await run_db(_names)
    priced = quote.priced
    return PublicQuoteOut(
        quoteId=quote.id,
        checkIn=quote.check_in, checkOut=quote.check_out,
        nights=(quote.check_out - quote.check_in).days,
        units=priced.units,
        roomTypeName=room_type.get("name") or "",
        ratePlanName=rate_plan.get("name") or "",
        inclusions=rate_plan.get("inclusions") or [],
        grossPaise=priced.gross_paise, discountPaise=priced.discount_paise,
        taxPaise=priced.tax_paise, totalPaise=priced.total_paise,
        tokenPaise=priced.token_paise, balancePaise=priced.balance_paise,
        expiresAt=quote.expires_at, currency=prop.currency,
    )


def _quote_row(quote_id: str, property_id: str) -> dict | None:
    rows = (
        get_db().table("quotes").select("*")
        .eq("id", quote_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


@router.post("/public/hotels/{slug}/quotes/{quote_id}/order", response_model=PublicOrderOut)
@limiter.limit("15/minute")
async def create_public_order(
    request: Request, slug: str, quote_id: str, body: PublicOrderRequest,
):
    """
    Turn a held quote into a Razorpay order.

    **The amount is computed here and never received.** It is the quote's own
    token plus server-priced add-ons; the request carries a service id and a
    quantity and nothing else that touches money. A browser that posts a price
    is posting a field no model on this route has.

    The order row is written *before* the id reaches the browser, because the
    webhook can fire while Checkout is still opening and a webhook that finds no
    order has nothing to settle. The guest's details and their add-ons are
    stored on it for the same reason: the webhook has no browser to ask.
    """
    prop = await public_booking.resolve_published(slug)
    quote = await run_db(_quote_row, quote_id, prop.id)
    if not quote:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That price is no longer available.")
    if quote["status"] != "active":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This price has already been used. Please search again.")
    if datetime.fromisoformat(str(quote["expires_at"]).replace("Z", "+00:00")) \
            < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This price has expired. Please search again.")

    addons = await public_booking.price_addons(
        prop.id, [a.model_dump() for a in body.addons],
        check_in=date.fromisoformat(str(quote["check_in"])),
        check_out=date.fromisoformat(str(quote["check_out"])),
        timezone_name=prop.timezone,
    )
    addons_total = sum(a.total_paise for a in addons)
    amount = public_booking.checkout_amount_paise(
        quote["total_paise"], quote["token_paise"], addons_total,
    )
    if amount < 100:
        # Razorpay refuses anything under a rupee. A property on pay-at-hotel
        # with no extras has nothing to charge, and saying so is better than
        # inventing a one-rupee order.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This stay does not require an online payment. Please contact the hotel.",
        )

    if not razorpay.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Online payment is unavailable right now.")

    rzp = await razorpay.create_order(
        amount_paise=amount,
        receipt=f"stay-{quote_id[:8]}",
        notes={"quote": quote_id, "property": prop.slug},
    )

    order = await payments_service.create(
        razorpay_order_id=rzp["id"], purpose="booking",
        partner_id=prop.partner_id, property_id=prop.id,
        quote_id=quote_id, amount_paise=amount,
        guest_details=body.guest.model_dump(),
        addons=[a.as_dict() for a in addons],
        attribution={
            **({"source": body.source} if body.source else {}),
            **({"visitorRef": body.visitorRef} if body.visitorRef else {}),
        },
    )

    return PublicOrderOut(
        orderId=order["id"], razorpayOrderId=rzp["id"],
        keyId=razorpay.public_key_id() or "",
        amountPaise=amount, quoteId=quote_id,
        addons=[a.as_dict() for a in addons],
        roomTokenPaise=min(quote["token_paise"], quote["total_paise"]),
        addonsTotalPaise=addons_total,
        balanceDuePaise=max(0, quote["total_paise"] - quote["token_paise"]),
        hotelName=prop.name, guestName=body.guest.fullName,
        guestEmail=body.guest.email, guestPhone=body.guest.phone,
    )


async def _fulfil_booking(order: dict) -> dict:
    """
    Create the reservation an order paid for. Called once per order, ever.

    The caller has already won `payments.claim`, so this runs for exactly one of
    the browser callback and the webhook. If it raises, the order is released
    back to 'created' and Razorpay's next webhook retry picks it up — a guest
    who has paid must never be left without a booking because a room sold in the
    seconds between.
    """
    guest = order.get("guest_details") or {}
    reservation = await booking_service.create_reservation(
        property_id=order["property_id"],
        quote_id=order["quote_id"],
        guest_name=guest.get("fullName") or "Guest",
        guest_phone=guest.get("phone"),
        guest_email=guest.get("email"),
        source="travbuddy" if (order.get("attribution") or {}).get("source") == "travbuddy"
               else "direct",
        # What was actually paid now. The rest is balance due on the folio,
        # which is what a folio is for.
        token_paise=order["amount_paise"],
        payment_method="gateway",
        notes=guest.get("notes"),
        created_by="booking_engine",
        member_id=None, member_name=None,
        conversation_id=order.get("conversation_id"),
    )
    await public_booking.attach_addons(reservation, order.get("addons") or [])
    await payments_service.record_result(order["id"], reservation_id=reservation["id"])

    # Point the thread at the booking, and tell the guest.
    #
    # Both after the reservation exists and both best-effort, because a guest
    # who has paid must end up with a booking whether or not we can message
    # them about it. `announce_booking` is idempotent on the reservation, which
    # is what makes it safe on the path the browser callback and the webhook
    # both run.
    await messaging_service.link_booking(reservation)
    await messaging_service.announce_booking(reservation)

    # Who, if anyone, sent this guest. Best-effort and after the fact on
    # purpose: the booking is paid for and must exist whether or not a
    # commission can be worked out, and `resolve` swallows its own failures for
    # exactly that reason. It is also idempotent on the reservation, so the
    # browser callback and the webhook racing here cannot pay twice.
    await creators_service.resolve(
        reservation["id"],
        visitor_ref=(order.get("attribution") or {}).get("visitorRef"),
    )
    return reservation


async def _public_booking_out(reservation: dict, hotel_name: str) -> PublicBookingOut:
    def _read():
        db = get_db()
        rt = (db.table("room_types").select("name")
              .eq("id", reservation["room_type_id"]).limit(1).execute().data or [{}])[0]
        balance = db.rpc("folio_balance",
                         {"p_reservation": reservation["id"]}).execute().data
        return rt, int(balance or 0)

    room_type, balance = await run_db(_read)
    addons = await public_booking.list_for_reservation(reservation["id"])
    return PublicBookingOut(
        ref=reservation["ref"], status=reservation["status"], hotelName=hotel_name,
        checkIn=reservation["check_in"], checkOut=reservation["check_out"],
        nights=reservation["nights"], units=reservation["units"],
        roomTypeName=room_type.get("name") or "",
        guestName=reservation.get("guest_name") or "Guest",
        totalPaise=reservation.get("total_paise") or 0,
        paidPaise=reservation.get("paid_paise") or 0,
        balancePaise=balance,
        addons=[
            QuotedAddonOut(
                serviceId=a["service_id"], name=a["name"], quantity=a["quantity"],
                unitPricePaise=a["unit_price_paise"], totalPaise=a["total_paise"],
                serviceDate=a.get("service_date"),
            ) for a in addons
        ],
        checkInUrl=f"/checkin/{reservation['ref']}",
    )


@router.post("/public/hotels/{slug}/confirm", response_model=PublicBookingOut)
@limiter.limit("15/minute")
async def confirm_public_booking(
    request: Request, slug: str, body: PublicConfirmRequest,
):
    """
    The browser returning from Checkout. The fast path, not the reliable one.

    **Payment success is never inferred from the browser.** The signature is an
    HMAC over `order_id|payment_id` with our secret, so a client cannot forge
    one — and even so, this route does not decide anything the webhook could not
    decide alone. If the webhook won the race this returns the booking it made;
    if the browser wins, the webhook later finds `reservation_id` set and does
    nothing.
    """
    prop = await public_booking.resolve_published(slug)

    if not razorpay.verify_payment_signature(
        body.razorpayOrderId, body.razorpayPaymentId, body.razorpaySignature
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment could not be verified.")

    order = await payments_service.get(body.razorpayOrderId)
    if not order or order["purpose"] != "booking" or order["property_id"] != prop.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that payment.")

    settlement = await payments_service.claim(body.razorpayOrderId, body.razorpayPaymentId)
    if settlement.won:
        try:
            reservation = await _fulfil_booking(settlement.order)
        except HTTPException:
            await payments_service.release(settlement.order["id"], "fulfilment failed")
            raise
        except Exception as exc:  # noqa: BLE001
            await payments_service.release(settlement.order["id"], str(exc)[:200])
            logger.exception("could not fulfil paid order %s", body.razorpayOrderId)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Your payment went through and we could not finish the booking. "
                "The hotel has been notified.",
            ) from exc
        return await _public_booking_out(reservation, prop.name)

    # Somebody else settled it — almost always the webhook, arriving first.
    existing = settlement.order
    if existing.get("reservation_id"):
        def _read():
            rows = (get_db().table("reservations").select("*")
                    .eq("id", existing["reservation_id"]).limit(1).execute().data or [])
            return rows[0] if rows else None
        reservation = await run_db(_read)
        if reservation:
            return await _public_booking_out(reservation, prop.name)

    # Claimed but not yet fulfilled: the other caller is mid-flight. Asking the
    # browser to wait is honest — the alternative is a second reservation.
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        "This payment is still being confirmed. Please refresh in a moment.",
    )


# ── paying for a stay that already exists ────────────────────────────────────
#
#     AI employee / front desk ──► create_payment_link ──► /pay/{order}
#                                                            │
#                          folio payment ◄── confirm ◄────────┤
#                                        ◄── webhook ◄────────┘
#
# The mirror of the booking engine above, with the reservation on the other
# end: there the payment creates the stay, here the stay already exists and the
# payment lands on its folio. Both settle through `payments_service.claim`, so
# the browser and the webhook race to the same single outcome.


# The two kinds of order this page can be showing. They differ in one way that
# matters to the page: a stay balance is money against a booking that exists,
# and a booking payment is money that will *create* one. Everything else — the
# hotel's name, the dates, the amount, the settle-once race — is identical, so
# they share the route rather than getting a second one.
PAY_PAGE_PURPOSES = (stay_payment.PURPOSE, quote_payment.PURPOSE)


async def _stay_payment_out(order: dict) -> StayPaymentOut:
    def _read():
        db = get_db()
        prop = (db.table("properties").select("name,currency")
                .eq("id", order["property_id"]).limit(1).execute().data or [{}])[0]

        if order.get("reservation_id"):
            res = (db.table("reservations")
                   .select("ref,guest_name,check_in,check_out")
                   .eq("id", order["reservation_id"]).limit(1).execute().data or [{}])[0]
            return res, prop

        # A booking payment before it has settled: there is no reservation to
        # read, so the dates come from the quote and the name from what the
        # agent was told. Not a fallback for a missing row — this is the normal
        # state of this kind of order right up until the guest pays.
        quote = {}
        if order.get("quote_id"):
            quote = (db.table("quotes").select("check_in,check_out")
                     .eq("id", order["quote_id"]).limit(1).execute().data or [{}])[0]
        guest = order.get("guest_details") or {}
        return {
            "ref": "",
            "guest_name": guest.get("fullName") or "Guest",
            "check_in": quote.get("check_in"),
            "check_out": quote.get("check_out"),
        }, prop

    reservation, prop = await run_db(_read)
    return StayPaymentOut(
        orderId=order["razorpay_order_id"],
        hotelName=prop.get("name") or "",
        # First name only. The link is forwardable and a full name on a page
        # anybody can open is more than the payer needs to recognise the stay.
        guestName=(reservation.get("guest_name") or "Guest").split(" ")[0],
        ref=reservation.get("ref") or "",
        amountPaise=int(order["amount_paise"]),
        currency=prop.get("currency") or "INR",
        status=order["status"],
        razorpayKeyId=razorpay.public_key_id() if order["status"] == "created" else None,
        checkIn=str(reservation.get("check_in") or "") or None,
        checkOut=str(reservation.get("check_out") or "") or None,
    )


@router.get("/public/payments/{order_id}", response_model=StayPaymentOut)
@limiter.limit("30/minute")
async def get_stay_payment(request: Request, order_id: str):
    """
    What a guest is being asked to pay, by the link they were sent.

    No authentication: the Razorpay order id *is* the credential, and it is
    random enough that guessing one is not a route in. The rate limit is what
    stops somebody trying anyway. A guest who has already paid gets the page
    saying so rather than a second chance to pay.
    """
    order = await payments_service.get(order_id)
    if not order or order["purpose"] not in PAY_PAGE_PURPOSES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that payment.")
    return await _stay_payment_out(order)


@router.post("/public/payments/{order_id}/confirm", response_model=StayPaymentOut)
@limiter.limit("15/minute")
async def confirm_stay_payment(
    request: Request, order_id: str, body: StayPaymentConfirmRequest,
):
    """
    The browser returning from Checkout, on the same terms as a booking: the
    signature is checked, and this decides nothing the webhook could not decide
    by itself.
    """
    if body.razorpayOrderId != order_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment could not be verified.")

    if not razorpay.verify_payment_signature(
        body.razorpayOrderId, body.razorpayPaymentId, body.razorpaySignature
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment could not be verified.")

    order = await payments_service.get(order_id)
    if not order or order["purpose"] not in PAY_PAGE_PURPOSES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that payment.")

    settlement = await payments_service.claim(order_id, body.razorpayPaymentId)
    if settlement.won:
        try:
            # The same table the webhook dispatches on, so the browser callback
            # and the webhook cannot disagree about what settling this order
            # means. A booking order creates the reservation here; a stay
            # balance posts to the folio. Whichever arrives first wins the
            # claim and the other one reads the finished state.
            await FULFIL[settlement.order["purpose"]](settlement.order)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            await payments_service.release(settlement.order["id"], str(exc)[:200])
            logger.exception("could not post stay payment %s", order_id)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "Your payment went through and we could not update the bill. "
                "The hotel has been notified.",
            ) from exc

    return await _stay_payment_out(await payments_service.get(order_id))


async def _redeem_plan_coupon(subscription_row: dict) -> None:
    """
    Record the coupon this mandate used, once.

    Called only from `subscription.authenticated` / `.activated`, which is the
    first moment Razorpay has confirmed the mandate is real. Doing it at
    checkout would let anybody who opens Razorpay and closes the tab spend a
    redemption — and on a capped code, take it from a hotel that would have
    paid.

    Failure is logged and swallowed: the mandate is authorised and the hotel is
    live, and refusing that because a marketing counter did not increment would
    be the wrong way round. The redemption row is recoverable from the
    subscription's own `coupon_id`.
    """
    coupon_id = subscription_row.get("coupon_id")
    if not coupon_id:
        return
    try:
        prop = await run_db(
            lambda: get_db().table("properties").select("partner_id")
            .eq("id", subscription_row["property_id"]).limit(1).execute().data or []
        )
        partner_id = (prop[0] or {}).get("partner_id") if prop else None
        if not partner_id:
            return
        recorded = await plan_coupons_service.redeem(
            coupon_id=coupon_id,
            partner_id=partner_id,
            property_id=subscription_row.get("property_id"),
            subscription_id=subscription_row.get("id"),
            razorpay_subscription_id=subscription_row.get("razorpay_subscription_id"),
            value_paise=int(subscription_row.get("amount_paise") or 0),
        )
        logger.info("coupon %s on mandate %s: %s", coupon_id,
                    subscription_row.get("razorpay_subscription_id"),
                    "redeemed" if recorded else "already redeemed")
    except Exception:  # noqa: BLE001
        logger.exception("could not record coupon redemption for %s",
                         subscription_row.get("razorpay_subscription_id"))


@router.post("/public/razorpay/webhook")
async def public_razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=None),
    x_razorpay_event_id: str = Header(default=None),
):
    """
    Razorpay's server-to-server callback. The reliable path.

    Deliberately not origin-guarded and not authenticated: Razorpay is not a
    browser. The HMAC over the **raw** body is the whole authentication story,
    which is why the body is read as bytes before anything parses it —
    re-serialising the JSON changes the bytes and the signature never matches.

    Always 200 once the signature is good. A non-2xx makes Razorpay retry, and
    retrying something that already succeeded is noise; the one case that
    deliberately *does* fail is a fulfilment error, because there a retry is
    exactly what we want.
    """
    if not razorpay.webhook_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook not configured.")

    raw = await request.body()
    if not razorpay.verify_webhook_signature(raw, x_razorpay_signature):
        logger.warning("[pms webhook] bad signature")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bad signature.")

    try:
        event = json.loads(raw or b"{}")
    except ValueError:
        return {"ok": True, "ignored": "unparseable"}

    name = event.get("event") or ""
    payload = event.get("payload") or {}
    payment = (payload.get("payment") or {}).get("entity") or {}
    order_id = payment.get("order_id") or (
        (payload.get("order") or {}).get("entity") or {}
    ).get("id")
    subscription_entity = (payload.get("subscription") or {}).get("entity") or {}
    subscription_id = subscription_entity.get("id") or payment.get("subscription_id")

    # ── seen it already? ────────────────────────────────────────────────────
    #
    # Before any handler, and before the order lookup — a redelivered event
    # should cost one insert that fails, not a fulfilment path that has to be
    # safe to run twice. Every handler below is anyway; this is the cheaper
    # guard in front, and the reconciliation log when the two sides disagree.
    fresh = await webhook_log.claim(
        x_razorpay_event_id, name, event,
        subscription_id=subscription_id,
        payment_id=payment.get("id"),
        order_id=order_id,
    )
    if not fresh:
        return {"ok": True, "handled": "already seen"}

    # ── the hotel's own ₹999 mandate ────────────────────────────────────────
    #
    # Handled before the order branch because a subscription event carries no
    # order id and would otherwise fall out of the early return below. This is
    # the only path that may activate a plan or grant a month of credits: the
    # browser callback cannot, by construction.
    if name.startswith("subscription."):
        if not subscription_id:
            await webhook_log.mark(x_razorpay_event_id, "ignored")
            return {"ok": True, "ignored": "no subscription id"}
        try:
            if name == "subscription.charged":
                invoice = (payload.get("invoice") or {}).get("entity") or {}
                payment_id = payment.get("id")
                if not payment_id:
                    await webhook_log.mark(x_razorpay_event_id, "ignored")
                    return {"ok": True, "ignored": "charge with no payment id"}
                result = await subscription_service.record_charge(
                    razorpay_subscription_id=subscription_id,
                    payment_id=payment_id,
                    amount_paise=int(payment.get("amount") or 0) or None,
                    invoice_id=invoice.get("id"),
                )
                await webhook_log.mark(x_razorpay_event_id, "processed")
                return {"ok": True, "handled": name, **result}

            updated = await subscription_service.apply_status(
                subscription_id,
                subscription_entity.get("status") or name.split(".", 1)[1],
                subscription_entity,
            )
            # A free-days mandate defers its first charge, so
            # `subscription.charged` will not arrive for weeks and this is the
            # only server-side event that says the mandate is real. The coupon
            # is redeemed here and **never a charge**: on an ordinary mandate
            # the authentication payment *is* the first ₹999, and
            # `subscription.charged` grants against that same payment id.
            if updated and name in ("subscription.authenticated",
                                    "subscription.activated"):
                await _redeem_plan_coupon(updated)
            await webhook_log.mark(x_razorpay_event_id, "processed")
            return {"ok": True, "handled": name}
        except Exception as exc:  # noqa: BLE001
            # 500 so Razorpay retries. Every handler above is idempotent, so a
            # redelivery is safe; swallowing a transient database error would
            # leave a hotel that has paid without its plan and without a trace.
            await webhook_log.mark(x_razorpay_event_id, "failed", str(exc))
            logger.exception("subscription webhook %s failed — asking for a retry", name)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR, "Handler failed; please retry.",
            ) from None

    if not order_id:
        await webhook_log.mark(x_razorpay_event_id, "ignored")
        return {"ok": True, "ignored": "no order id"}

    order = await payments_service.get(order_id)
    if not order:
        # Not ours — the marketplace webhook handles its own orders.
        return {"ok": True, "ignored": "unknown order"}

    if name in ("payment.failed",):
        await payments_service.mark(order_id, "failed", payment.get("error_description"))
        return {"ok": True, "handled": "failed"}

    if name not in ("payment.captured", "order.paid"):
        return {"ok": True, "ignored": name}

    payment_id = payment.get("id")
    if not payment_id:
        return {"ok": True, "ignored": "no payment id"}

    settlement = await payments_service.claim(order_id, payment_id)
    if not settlement.won:
        # The browser got here first, or this is a duplicate delivery. Either
        # way there is nothing to do — and saying so is the whole point.
        return {"ok": True, "handled": "already settled"}

    purpose = settlement.order.get("purpose")
    # Dispatched by name, with no `else`. This was `if booking … else credits`,
    # which meant every purpose added later — the first was `stay_balance` —
    # was silently handed to the credits path, where it read a null
    # billing_account_id, threw, released the order and made Razorpay retry the
    # same failure for twenty-four hours. An unrecognised purpose is a
    # deployment that is behind its own database, and the safe answer is to
    # leave the order claimed and say so, not to guess which fulfilment it
    # meant.
    fulfil = FULFIL.get(purpose)

    if fulfil is None:
        logger.error("webhook cannot fulfil unknown purpose %r on order %s",
                     purpose, order_id)
        await payments_service.release(settlement.order["id"],
                                       f"unknown purpose {purpose!r}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Could not fulfil the order.")

    try:
        await fulfil(settlement.order)
    except Exception as exc:  # noqa: BLE001
        await payments_service.release(settlement.order["id"], str(exc)[:200])
        logger.exception("webhook could not fulfil order %s", order_id)
        # The one deliberate non-2xx: Razorpay retries for 24 hours, and a
        # retry is exactly the recovery this needs.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Could not fulfil the order.") from exc

    return {"ok": True, "handled": purpose}


# ── M5.6 — buying credits ────────────────────────────────────────────────────
#
# The ledger has been built and tested since migration 12; this is only the
# commerce. A purchase creates a `purchased` bucket through `grant_credits()`
# and touches nothing else — `charge_usage` is unchanged, no wallet balance is
# written directly, and the idempotency key is the Razorpay order id, so a
# webhook delivered three times grants once.

async def _fulfil_credits(order: dict) -> dict:
    """
    Grant the credits an order paid for. Called once per order, ever.

    `billing.grant()` is the only writer, and its key is derived from the
    Razorpay order — not from the clock, not from a uuid. That is what makes
    this safe to call from both the browser callback and a retried webhook.
    """
    key = f"credit-purchase:{order['razorpay_order_id']}"
    result = await billing.grant(
        billing_account_id=order["billing_account_id"],
        kind="purchased",
        credits=int(order["credits"]),
        idempotency_key=key,
        source_type="purchase",
        actor_type="user",
        metadata={
            "razorpayOrderId": order["razorpay_order_id"],
            "razorpayPaymentId": order.get("razorpay_payment_id"),
            "amountPaise": order["amount_paise"],
        },
    )
    await payments_service.record_result(order["id"], ledger_key=key)
    return result


# What settles each kind of payment order.
#
# A table rather than a chain of ifs, because the chain had an `else` on it and
# the `else` meant "credits". Every purpose added after the first two was
# therefore handed to the credits path, which read a null billing account and
# threw — and because the webhook releases and 500s on a fulfilment error,
# Razorpay retried that same failure for twenty-four hours. A dict has no
# `else`, so the next purpose somebody adds cannot silently become a credit
# grant.
FULFIL: dict[str, object] = {
    "booking": _fulfil_booking,
    "credits": _fulfil_credits,
    stay_payment.PURPOSE: stay_payment.settle,
}


@router.post("/properties/{property_id}/credits/order", response_model=CreditOrderOut)
@limiter.limit("10/minute")
async def create_credit_order(
    request: Request,
    property_id: str,
    body: CreditOrderRequest,
    member=Depends(require_member("billing")),
):
    """
    Start a credit top-up.

    The client asks for a number of credits; **the server decides what they
    cost**. `CREDIT_PRICE_PAISE` is the one place that conversion happens, and a
    rupee amount in the request body would be a field this model does not have.
    """
    account = await billing.account_for_property(property_id)
    if not account:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This property has no billing account.")
    if not razorpay.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Payments are unavailable right now.")

    amount = body.credits * CREDIT_PRICE_PAISE
    rzp = await razorpay.create_order(
        amount_paise=amount,
        receipt=f"credits-{property_id[:8]}",
        notes={"credits": str(body.credits), "property": property_id},
    )
    order = await payments_service.create(
        razorpay_order_id=rzp["id"], purpose="credits",
        partner_id=member.partner_id, property_id=property_id,
        billing_account_id=account, credits=body.credits, amount_paise=amount,
    )
    return CreditOrderOut(
        orderId=order["id"], razorpayOrderId=rzp["id"],
        keyId=razorpay.public_key_id() or "",
        credits=body.credits, amountPaise=amount,
    )


@router.post("/properties/{property_id}/credits/verify", response_model=CreditPurchaseOut)
@limiter.limit("20/minute")
async def verify_credit_purchase(
    request: Request,
    property_id: str,
    body: PublicConfirmRequest,
    member=Depends(require_member("billing")),
):
    """
    The browser returning from a credit top-up.

    Same convergence as a booking: the signature is checked, the claim decides
    who acts, and the webhook is the reliable path behind it. A duplicate call
    reports the wallet rather than granting again.
    """
    if not razorpay.verify_payment_signature(
        body.razorpayOrderId, body.razorpayPaymentId, body.razorpaySignature
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That payment could not be verified.")

    order = await payments_service.get(body.razorpayOrderId)
    if not order or order["purpose"] != "credits" or order["property_id"] != property_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that payment.")

    settlement = await payments_service.claim(body.razorpayOrderId, body.razorpayPaymentId)
    if settlement.won:
        try:
            result = await _fulfil_credits(settlement.order)
        except Exception as exc:  # noqa: BLE001
            await payments_service.release(settlement.order["id"], str(exc)[:200])
            logger.exception("could not grant credits for %s", body.razorpayOrderId)
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                                "Your payment went through and the credits have not "
                                "landed yet. They will shortly.") from exc
        state = await billing.wallet_state(order["billing_account_id"])
        return CreditPurchaseOut(
            status="granted", credits=int(order["credits"]),
            creditsGranted=int(order["credits"]),
            balanceAfter=int(state.get("balance") or 0),
        )

    state = await billing.wallet_state(order["billing_account_id"])
    return CreditPurchaseOut(
        status="already_granted", credits=int(order["credits"]),
        creditsGranted=0, balanceAfter=int(state.get("balance") or 0),
        alreadyProcessed=True,
    )


@router.get("/properties/{property_id}/payments", response_model=list[PaymentOrderOut])
@limiter.limit("60/minute")
async def list_payment_orders(
    request: Request,
    property_id: str,
    limit: int = Query(50, ge=1, le=200),
    member=Depends(require_member("billing")),
):
    """Every order this property has started, paid or otherwise."""
    rows = await payments_service.history(property_id, limit)
    return [
        PaymentOrderOut(
            id=r["id"], razorpayOrderId=r["razorpay_order_id"], purpose=r["purpose"],
            amountPaise=r["amount_paise"], credits=r.get("credits"),
            status=r["status"], reservationId=r.get("reservation_id"),
            createdAt=r["created_at"], paidAt=r.get("paid_at"),
            failureReason=r.get("failure_reason"),
        ) for r in rows
    ]


# ── M5.4 — the website builder's own API ─────────────────────────────────────

async def _domain_out(property_id: str) -> DomainOut:
    """
    The stored state plus a live look at DNS.

    The lookup happens on read rather than on a schedule because the moment an
    owner is looking at this screen is the moment they have just edited their
    zone and want to know whether it took. A cached answer here is the one
    thing that would make the screen useless.
    """
    row = await website_service.get_config(property_id)
    state = domains_service.project(row)
    records = []
    if state["domain"]:
        records = [
            DnsRecordOut(type=r.type, name=r.name, value=r.value, found=r.found)
            for r in await domains_service.records_for(state["domain"], state["token"])
        ]
    return DomainOut(
        status=state["status"], domain=state["domain"],
        verifiedAt=state["verifiedAt"], checkedAt=state["checkedAt"],
        error=state["error"], records=records,
    )


@router.get("/properties/{property_id}/website", response_model=WebsiteConfigOut)
@limiter.limit("120/minute")
async def get_website(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    The builder's view: the **draft**, always.

    'rates' rather than a new scope. Publishing a website is a commercial act
    about how a hotel sells itself, and the permission that already governs what
    it sells for is the right one — a receptionist does not get to rewrite the
    hotel's home page.
    """
    row = await website_service.get_config(property_id)
    return {
        **website_service.project(row),
        **await website_service.direct_30d(property_id),
    }


@router.patch("/properties/{property_id}/website", response_model=WebsiteConfigOut)
@limiter.limit("120/minute")
async def update_website(
    request: Request,
    property_id: str,
    body: WebsiteUpdateRequest,
    member=Depends(require_member("rates")),
):
    """
    Save the draft. **Never the published copy.**

    `website.save_draft` writes one column and cannot reach the other, which is
    the whole reason draft and published are separate: a hotelier typing a new
    headline must not change the page a guest is reading.
    """
    row = await website_service.save_draft(
        property_id,
        template=body.template,
        draft=body.model_dump(exclude_none=True, exclude={"template"}),
    )
    return website_service.project(row)


@router.post("/properties/{property_id}/website/publish", response_model=WebsiteConfigOut)
@limiter.limit("30/minute")
async def publish_website(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """Copy the draft to published, and make `/stay/[slug]` answer."""
    row = await website_service.publish(property_id, member.profile_id)
    return website_service.project(row)


@router.post("/properties/{property_id}/website/unpublish", response_model=WebsiteConfigOut)
@limiter.limit("30/minute")
async def unpublish_website(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    Take the site down. The content is kept.

    Unpublishing is not deleting: a hotel that goes quiet for a season should
    not have to rebuild its page to come back.
    """
    row = await website_service.unpublish(property_id)
    return website_service.project(row)


# ── the hotel's own address ──────────────────────────────────────────────────
#
#   PUT    …/website/domain          claim it, and get the two records to add
#   POST   …/website/domain/verify   look at real DNS and say what is there
#   DELETE …/website/domain          disconnect; the TravBuddy address remains
#
# `rates`, matching the rest of the builder: which address a hotel sells itself
# under is a commercial decision, not a front-desk one.
#
# None of these serve anything. `services/pms/domains.hotel_for_host` is the
# only thing that turns a hostname into a hotel, and it answers for a verified
# domain on a published site and nothing else.

@router.get("/properties/{property_id}/website/domain", response_model=DomainOut)
@limiter.limit("60/minute")
async def get_website_domain(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """The address, and whether each DNS record is visible from the internet."""
    return await _domain_out(property_id)


@router.put("/properties/{property_id}/website/domain", response_model=DomainOut)
@limiter.limit("20/hour")
async def set_website_domain(
    request: Request,
    property_id: str,
    body: DomainRequest,
    member=Depends(require_member("rates")),
):
    """
    Claim a domain. Nothing is served on it yet and nobody is told it is.

    Rate limited hard: this is the endpoint that mints an ownership token, and
    an unbounded one is a way to ask which domains are already connected here.
    """
    await domains_service.claim(property_id, body.domain)
    return await _domain_out(property_id)


@router.post("/properties/{property_id}/website/domain/verify", response_model=DomainOut)
@limiter.limit("20/hour")
async def verify_website_domain(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """
    Check the real DNS now.

    "Still not there" is a legitimate answer and comes back as a 200 with the
    records showing which half is missing — a 4xx here would read to the owner
    as *they* did something wrong, when the usual truth is that their registrar
    has not propagated yet.
    """
    await domains_service.verify(property_id)
    return await _domain_out(property_id)


@router.delete("/properties/{property_id}/website/domain", response_model=DomainOut)
@limiter.limit("20/hour")
async def clear_website_domain(
    request: Request, property_id: str, member=Depends(require_member("rates")),
):
    """Disconnect. The hotel is never off the internet — /stay/[slug] remains."""
    await domains_service.release(property_id)
    return await _domain_out(property_id)


@router.get("/public/sites/by-host", response_model=PublicSiteHostOut)
@limiter.limit("120/minute")
async def resolve_site_host(request: Request, host: str = Query(max_length=253)):
    """
    Which hotel a hostname belongs to.

    Public and unauthenticated because it runs before anything else on a guest's
    first paint: the browser is on `www.grandhotel.com` and the app has to know
    whose booking page to render before it can render anything. It returns a
    slug and a name — both already public on that page — and 404s for a host
    that is not verified, not published, or not ours.
    """
    found = await domains_service.hotel_for_host(host)
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "We could not find that hotel.")
    return PublicSiteHostOut(slug=found["slug"], name=found.get("name") or "")


# ── the hotel's plan ─────────────────────────────────────────────────────────
#
#   GET    …/plan               what it costs and where this hotel stands
#   POST   …/plan/coupon        price a code, without spending it
#   POST   …/plan/subscribe     open a mandate; nothing is active yet
#   POST   …/plan/cancel        stop it
#
# `billing`, which owners and accounts hold and nobody else does. A receptionist
# has no business seeing what the hotel pays, and no business starting it.
#
# **No route here takes a price, a plan id or an amount.** The plan comes from
# `subscription_plans`, the discount from `plan_coupons`, and the only thing
# that may set a subscription to active is a signature-verified webhook.

@router.get("/properties/{property_id}/plan", response_model=PlanStateOut)
@limiter.limit("120/minute")
async def get_plan_state(
    request: Request, property_id: str, member=Depends(require_member("billing")),
):
    """Where this hotel stands on the ₹999 plan, in the hotel's own words."""
    plan_row = await subscription_service.plan()
    row = await subscription_service.current(property_id)
    return PlanStateOut(**subscription_service.project(row, plan_row))


@router.get("/properties/{property_id}/credits", response_model=CreditsSummaryOut)
@limiter.limit("120/minute")
async def get_credits_summary(
    request: Request,
    property_id: str,
    days: int = Query(default=30, ge=1, le=90),
    member=Depends(require_member("billing")),
):
    """
    What the hotel has left, where it came from, and what it went on.

    The same function the Owner Copilot calls, so the screen and the answer to
    "how many credits do I have" cannot disagree — showing an owner two versions
    of a number is the fastest way to lose their trust in both.
    """
    return CreditsSummaryOut(**await billing.credits_summary(property_id, days=days))


@router.post("/properties/{property_id}/plan/coupon", response_model=CouponPreviewOut)
@limiter.limit("20/minute")
async def preview_plan_coupon(
    request: Request,
    property_id: str,
    body: CouponRequest,
    member=Depends(require_member("billing")),
):
    """
    What a code is worth, before anybody commits to anything.

    Reads only — a preview must never spend a redemption, or a hotel that types
    a capped code and changes its mind has taken it from one that would have
    paid. Rate limited because this is also the endpoint somebody would use to
    guess codes; every rejection is the same sentence for the same reason.
    """
    plan_row = await subscription_service.plan()
    try:
        discount = await plan_coupons_service.resolve(
            body.code,
            partner_id=str(member.partner_id),
            price_paise=int(plan_row["price_paise"]),
        )
    except plan_coupons_service.CouponError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message) from None

    return CouponPreviewOut(
        code=discount.code, label=discount.label, kind=discount.kind,
        firstPaymentPaise=discount.first_payment_paise,
        discountPaise=discount.discount_paise,
        renewalPaise=discount.renewal_paise,
        freeDays=discount.free_days,
        description=discount.description,
    )


@router.post("/properties/{property_id}/plan/subscribe", response_model=CheckoutOut)
@limiter.limit("10/hour")
async def start_plan(
    request: Request,
    property_id: str,
    body: SubscribeRequest,
    member=Depends(require_member("billing")),
):
    """
    Open a mandate and hand back what Checkout needs.

    **Nothing is active when this returns.** The row is written `pending`, the
    owner authorises in Razorpay's own sheet, and a webhook is what makes it
    real. Rate limited hard: each call mints a Razorpay subscription, and a
    double-clicked button should cost one.
    """
    try:
        checkout = await subscription_service.start(
            property_id=property_id,
            partner_id=str(member.partner_id),
            coupon_code=body.couponCode,
        )
    except plan_coupons_service.CouponError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message) from None
    except subscription_service.SubscriptionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message) from None

    return CheckoutOut(
        razorpaySubscriptionId=checkout.razorpay_subscription_id,
        razorpayKeyId=checkout.razorpay_key_id,
        planName=checkout.plan_name,
        amountPaise=checkout.amount_paise,
        firstPaymentPaise=checkout.first_payment_paise,
        currency=checkout.currency,
        couponCode=checkout.coupon_code,
        couponLabel=checkout.coupon_label,
    )


@router.post("/properties/{property_id}/plan/cancel", response_model=PlanStateOut)
@limiter.limit("10/hour")
async def cancel_plan(
    request: Request, property_id: str, member=Depends(require_member("billing")),
):
    """
    Stop the plan at the end of the cycle the hotel has already paid for.

    Never immediately: they bought the month.
    """
    try:
        await subscription_service.cancel(property_id, at_cycle_end=True)
    except subscription_service.SubscriptionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message) from None
    plan_row = await subscription_service.plan()
    return PlanStateOut(**subscription_service.project(
        await subscription_service.current(property_id), plan_row))


# ── M6: the B2B agent portal ─────────────────────────────────────────────────
#
#   /b2b ──► /agency/me                                who am I, and my line
#        ──► /agency/properties                        what I may sell
#        ──► /agency/properties/{id}/availability      the same ARI calendar
#        ──► /agency/properties/{id}/quotes            price it, hold it, commit
#        ──► /agency/properties/{id}/bookings          quote → reservation
#        ──► /agency/bookings                          what I have sold
#
# Every one of these resolves the agency from the JWT through `require_agency`,
# and the property id in the path against the contract through
# `b2b.contract_for`. No route accepts an agency id — the parallel of no PMS
# route accepting a partner id, and for the same reason.
#
# Not one of them prices anything. `stay_options` and `create_quote` are the
# same functions the front desk calls; the only difference is that `audience` is
# fixed to 'b2b' here by the server rather than being a query parameter.

@router.get("/agency/me", response_model=AgencyMeOut)
@limiter.limit("120/minute")
async def get_agency_me(request: Request, agency=Depends(require_agency)):
    """
    The signed-in agency and the state of every credit line it holds.

    An agency with contracts at two hotel groups gets two, because it negotiated
    twice. The portal shows the one covering whichever hotel is on screen.
    """
    contracts = []
    for contract in agency.contracts:
        committed = await b2b_service.committed_paise(contract.id)
        contracts.append(AgencyContractOut(
            id=contract.id,
            code=contract.code,
            companyName=contract.company_name,
            contactName=contract.contact_name,
            city=contract.city,
            phone=contract.phone,
            email=contract.email,
            commissionPct=contract.commission_pct,
            creditLimitPaise=contract.credit_limit_paise,
            committedPaise=committed,
            availablePaise=max(contract.credit_limit_paise - committed, 0),
            status=contract.status,
            partnerId=contract.partner_id,
            propertyIds=list(contract.property_ids),
        ))
    return AgencyMeOut(
        profileId=agency.profile_id,
        fullName=agency.full_name,
        email=agency.email,
        contracts=contracts,
    )


@router.get("/agency/properties", response_model=list[AgencyPropertyOut])
@limiter.limit("120/minute")
async def list_agency_properties(request: Request, agency=Depends(require_agency)):
    """The hotels this agency may sell, each carrying the contract that covers it."""
    return [
        AgencyPropertyOut(
            id=row["id"],
            name=row["name"],
            city=row.get("city"),
            currency=row.get("currency") or "INR",
            checkInTime=str(row.get("check_in_time") or "")[:5] or None,
            checkOutTime=str(row.get("check_out_time") or "")[:5] or None,
            agencyId=row["agency_id"],
        )
        for row in await b2b_service.sellable_properties(agency)
    ]


@router.get("/agency/properties/{property_id}/availability",
            response_model=list[StayOptionOut])
@limiter.limit("120/minute")
async def get_agency_availability(
    request: Request,
    property_id: str,
    check_in: date = Query(..., alias="checkIn"),
    check_out: date = Query(..., alias="checkOut"),
    units: int = Query(1, ge=1, le=20),
    agency=Depends(require_agency),
):
    """
    What this hotel can sell these dates, on the plans this agency may see.

    `audience='b2b'` is passed here and cannot be overridden by the caller —
    unlike the staff route, which honours a query parameter because the front
    desk legitimately books on behalf of agencies and must be able to see both.
    An agency has no such reason, and a portal that could ask for `audience=None`
    would be a portal that could read the hotel's public rate card.
    """
    await b2b_service.contract_for(agency, property_id)
    options = await availability_service.stay_options(
        property_id, check_in, check_out, units=units, audience="b2b"
    )
    return [_stay_option_out(o) for o in options]


@router.post("/agency/properties/{property_id}/quotes", response_model=AgencyQuoteOut)
@limiter.limit("60/minute")
async def create_agency_quote(
    request: Request,
    property_id: str,
    body: AgencyQuoteRequest,
    agency=Depends(require_agency),
):
    """
    Price a stay, hold the stock, and take the credit.

    **The credit is committed here, not at booking.** A quote holds a room for
    fifteen minutes; from this moment it holds the money for the same fifteen
    minutes, and both are released by the same expiry. That is what makes the
    limit hold under two simultaneous bookings — `b2b_commit_credit` tags the
    quote inside a lock on the agency's row, so the second one counts the first.
    Checking at booking time instead would leave a fifteen-minute window in
    which two agents at the same agency are both told yes.

    A refusal releases the hold rather than leaving a room off sale for a
    booking everybody already knows is not happening.
    """
    contract = await b2b_service.contract_for(agency, property_id)
    contract.require_active()

    result = await booking_service.create_quote(
        property_id=property_id,
        rate_plan_id=body.ratePlanId,
        check_in=body.checkIn,
        check_out=body.checkOut,
        units=body.units,
        adults=body.adults,
        children=body.children,
        issued_by="agent_portal",
        # The clamp that matters. A b2b plan id is the only thing this path can
        # price, so an agency that guessed a public rate plan's uuid gets the
        # same 404 as one that invented it.
        audience="b2b",
    )

    try:
        verdict = await b2b_service.commit_credit(contract.id, result.id)
    except HTTPException:
        await booking_service.abandon_quote(result.id, result.hold_id)
        raise

    committed = int(verdict.get("committedPaise") or 0)
    limit = int(verdict.get("creditLimitPaise") or 0)
    return AgencyQuoteOut(
        id=result.id,
        holdId=result.hold_id,
        propertyId=property_id,
        roomTypeId=result.room_type_id,
        ratePlanId=result.rate_plan_id,
        checkIn=result.check_in,
        checkOut=result.check_out,
        units=result.priced.units,
        adults=result.adults,
        children=result.children,
        nightly=[NightlyOut(date=n.date, pricePaise=n.price_paise) for n in result.priced.nightly],
        grossPaise=result.priced.gross_paise,
        discountPaise=result.priced.discount_paise,
        taxPaise=result.priced.tax_paise,
        totalPaise=result.priced.total_paise,
        # An agency settles on invoice, so nothing is collected now and the
        # property's token rule does not apply to it. The whole total is the
        # balance, and it sits on the folio until the hotel bills it.
        tokenPaise=0,
        balancePaise=result.priced.total_paise,
        issuedBy="agent_portal",
        expiresAt=result.expires_at,
        agencyId=contract.id,
        committedPaise=committed,
        creditLimitPaise=limit,
        availablePaise=max(limit - committed, 0),
    )


@router.post("/agency/properties/{property_id}/bookings", response_model=ReservationOut)
@limiter.limit("60/minute")
async def create_agency_booking(
    request: Request,
    property_id: str,
    body: AgencyBookingRequest,
    agency=Depends(require_agency),
):
    """
    Turn a committed quote into a booking.

    The agency is read off the quote by `create_reservation` and is never sent
    in the request, so the only stay that can reach a credit line is one
    `b2b_commit_credit` already checked the limit for. The check below is the
    matching half: a quote committed to *another* agency is not bookable here,
    even though both are valid quotes on the same hotel.

    Nothing is collected. An agency pays on invoice, which means the entire
    total sits on the folio as a balance — and that balance is exactly what
    `b2b_committed_paise` reads, so the desk posting a payment against it is the
    whole of settlement.
    """
    contract = await b2b_service.contract_for(agency, property_id)
    contract.require_active()

    quote = await run_db(_quote_row, body.quoteId, property_id)
    if not quote or quote.get("b2b_agent_id") != contract.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Quote not found")

    row = await booking_service.create_reservation(
        property_id=property_id,
        quote_id=body.quoteId,
        guest_name=body.guestName.strip(),
        guest_phone=(body.guestPhone or "").strip() or None,
        guest_email=(body.guestEmail or "").strip() or None,
        source="b2b",
        token_paise=0,
        payment_method=None,
        notes=(body.notes or "").strip() or None,
        created_by="b2b",
        member_id=None,
        member_name=contract.company_name,
    )
    return ReservationOut.from_row(row)


@router.get("/agency/bookings", response_model=list[ReservationOut])
@limiter.limit("120/minute")
async def list_agency_bookings(request: Request, agency=Depends(require_agency)):
    """Everything this agency has booked, newest first, across every contract."""
    return [ReservationOut.from_row(row) for row in await b2b_service.list_bookings(agency)]


# ── M6b: the creator programme ───────────────────────────────────────────────
#
#   public ──► POST /public/hotels/{slug}/attribution   record the click
#
#   staff  ──► GET  /properties/{id}/creator-campaigns
#          ──► GET  .../creator-campaigns/{cid}/touches   the evidence chain
#          ──► GET  .../creator-campaigns/{cid}/payouts   and what it is worth
#          ──► POST .../creator-campaigns/{cid}/status
#          ──► POST .../creator-campaigns/{cid}/approve
#
# The two permissions are deliberately different. Reading and running a campaign
# is `marketing`, which a manager holds. **Approving money is `payouts`**, which
# by default only an owner and an accounts clerk hold — a manager can run the
# programme all day and cannot sign off a rupee of it. That separation is the
# whole reason the approval step exists.
#
# Not one of these routes accepts an amount, a room-night count, a commission or
# a creator id. There is nothing on the wire that could move money: the figures
# come out of `creator_payout_recalc`, from the reservation.


@router.post("/public/hotels/{slug}/attribution", response_model=PublicAttributionOut)
@limiter.limit("30/minute")
async def record_public_attribution(
    request: Request, slug: str, body: PublicAttributionRequest,
):
    """
    A guest arrived on a creator's link.

    The only write an unauthenticated caller can cause on this path, and it is
    deliberately almost inert: it records that a code was opened, and returns a
    ref the browser will hand back later. It cannot create a booking, cannot see
    a price and cannot name a creator — the code resolves server-side or it
    resolves to nothing.

    **A refusal is a 200.** A campaign the hotel paused last week must still
    render its own hotel page, so `attributed=false` is the answer rather than a
    404 the front end would have to special-case. The visitor ref comes back
    either way, because the next click on a *live* link needs to be the same
    visitor.
    """
    # Resolving the hotel first is what scopes this: a code belonging to another
    # property is simply not this page's, and the 404 for an unpublished slug
    # arrives before any campaign is looked at.
    prop = await public_booking.resolve_published(slug)

    touch = await creators_service.record_touch(
        body.code,
        source="website",
        visitor_ref=body.visitorRef,
        metadata={"slug": prop.slug},
    )
    if not touch.ok:
        return PublicAttributionOut(
            attributed=False,
            visitorRef=body.visitorRef or creators_service.new_visitor_ref(),
        )
    return PublicAttributionOut(
        attributed=True,
        visitorRef=touch.visitor_ref or "",
        creatorName=touch.creator_name,
        creatorHandle=touch.creator_handle,
        campaignName=touch.campaign_name,
        guestDiscountPct=touch.guest_discount_pct,
        touchRef=touch.touch_ref,
    )


def _campaign_out(p) -> CreatorCampaignOut:
    return CreatorCampaignOut(
        id=p.id,
        campaignId=p.campaign_id,
        campaignName=p.campaign_name,
        propertyId=p.property_id,
        creatorId=p.creator_id,
        creatorName=p.creator_name,
        creatorHandle=p.creator_handle,
        platform=p.platform if p.platform in
            ("instagram", "youtube", "blog", "other") else "other",
        followers=p.followers,
        code=p.code,
        status=p.status,
        startDate=p.start_date,
        endDate=p.end_date,
        guestDiscountPct=p.guest_discount_pct,
        commissionPct=p.commission_pct,
        attributionWindowDays=p.attribution_window_days,
        minNights=p.min_nights,
        clicks=p.clicks,
        conversations=p.conversations,
        bookings=p.bookings,
        roomNights=p.room_nights,
        revenuePaise=p.revenue_paise,
        payoutPaise=p.payout_paise,
        approvedPaise=p.approved_paise,
        payoutStatus=p.payout_status,
    )


@router.get("/properties/{property_id}/creator-campaigns",
            response_model=list[CreatorCampaignOut])
@limiter.limit("120/minute")
async def list_creator_campaigns(
    request: Request, property_id: str, member=Depends(require_member("marketing")),
):
    """Every creator this hotel works with, newest first, with the numbers rolled up."""
    return [_campaign_out(p) for p in await creators_service.list_participations(property_id)]


@router.get("/properties/{property_id}/creator-campaigns/{campaign_id}/touches",
            response_model=list[AttributionTouchOut])
@limiter.limit("120/minute")
async def list_attribution_touches(
    request: Request, property_id: str, campaign_id: str,
    member=Depends(require_member("marketing")),
):
    """
    The evidence chain for one creator, newest first.

    This is the screen that answers "why did we pay this". The booking touch
    carries the reservation reference and what the stay was worth, so the trail
    reads as a sequence of facts rather than a list of uuids.
    """
    await creators_service.require_participation(campaign_id, property_id)
    out: list[AttributionTouchOut] = []
    for touch in await creators_service.list_touches(campaign_id):
        reservation = touch.get("reservation") or {}
        out.append(AttributionTouchOut(
            id=touch["id"],
            campaignId=campaign_id,
            kind=touch["kind"],
            source=touch.get("source") or "other",
            at=touch["occurred_at"],
            expiresAt=touch["expires_at"],
            city=touch.get("city"),
            touchRef=touch.get("touch_ref"),
            conversationId=touch.get("conversation_id"),
            reservationId=touch.get("reservation_id"),
            reservationRef=reservation.get("ref"),
            nights=reservation.get("nights"),
            valuePaise=reservation.get("total_paise"),
        ))
    return out


@router.get("/properties/{property_id}/creator-campaigns/{campaign_id}/payouts",
            response_model=list[CreatorPayoutOut])
@limiter.limit("120/minute")
async def list_creator_payouts(
    request: Request, property_id: str, campaign_id: str,
    member=Depends(require_member("marketing")),
):
    """
    What this creator is owed, booking by booking, and why.

    Readable on `marketing` rather than `payouts` on purpose: a manager running
    the programme needs to see what it is costing. Approving is the narrower
    permission, and it is a different route.
    """
    await creators_service.require_participation(campaign_id, property_id)
    out: list[CreatorPayoutOut] = []
    for payout in await creators_service.list_payouts(campaign_id):
        reservation = payout.get("reservation") or {}
        out.append(CreatorPayoutOut(
            id=payout["id"],
            campaignId=campaign_id,
            reservationId=payout["reservation_id"],
            reservationRef=reservation.get("ref") or "",
            guestName=reservation.get("guest_name") or "",
            checkIn=reservation.get("check_in"),
            checkOut=reservation.get("check_out"),
            source=reservation.get("source") or "direct",
            reservationStatus=reservation.get("status") or "",
            status=payout["status"],
            eligible=bool(payout.get("eligible")),
            ineligibleReason=payout.get("ineligible_reason"),
            roomNights=int(payout.get("room_nights") or 0),
            netRevenuePaise=int(payout.get("net_revenue_paise") or 0),
            commissionPct=float(payout.get("commission_pct") or 0),
            termsVersion=int(payout.get("terms_version") or 1),
            amountPaise=int(payout.get("amount_paise") or 0),
            approvedByName=payout.get("approved_by_name"),
            approvedAt=payout.get("approved_at"),
            events=[
                PayoutEventOut(
                    fromStatus=e["from_status"], toStatus=e["to_status"],
                    actorName=e["actor_name"],
                    amountPaise=int(e.get("amount_paise") or 0),
                    note=e.get("note"), at=e["at"],
                ) for e in payout.get("events") or []
            ],
        ))
    return out


@router.post("/properties/{property_id}/creator-campaigns/{campaign_id}/status",
             response_model=CreatorCampaignOut)
@limiter.limit("60/minute")
async def set_creator_campaign_status(
    request: Request, property_id: str, campaign_id: str,
    body: CampaignStatusRequest, member=Depends(require_member("marketing")),
):
    """
    Pause, resume or end one creator's participation.

    The client sends a word, not a state machine: which transitions are legal is
    decided in `creators.set_status` and, for the campaign itself, by a trigger
    in migration 23. A client that could set an arbitrary status could restart
    an ended campaign, which would make the touches already recorded under it
    ambiguous about which run they belonged to.
    """
    await creators_service.set_status(campaign_id, property_id, body.status)
    for p in await creators_service.list_participations(property_id):
        if p.id == campaign_id:
            return _campaign_out(p)
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Campaign not found")


@router.post("/properties/{property_id}/creator-campaigns/{campaign_id}/approve",
             response_model=PayoutApprovalOut)
@limiter.limit("30/minute")
async def approve_creator_payout(
    request: Request, property_id: str, campaign_id: str,
    body: PayoutApprovalRequest | None = None,
    member=Depends(require_member("payouts")),
):
    """
    Approve everything currently payable for this creator.

    **`payouts`, not `marketing`.** A manager runs the programme; an owner or an
    accounts clerk signs off what it costs. That is the separation the approval
    step exists for, and it is the reason this route does not simply live behind
    the same permission as the screen it is on.

    The amount is not in the request and cannot be. Two people pressing approve
    at the same moment serialise on the participation's row lock and the second
    approves nothing, which is reported as `approved: 0` rather than as an
    error — pressing it twice is not a mistake, it is a person checking.
    """
    verdict = await creators_service.approve(
        campaign_id, property_id,
        profile_id=member.profile_id,
        member_id=member.member_id,
        name=member.display_name or "Owner",
        note=(body.note if body else None),
    )
    return PayoutApprovalOut(
        approved=int(verdict.get("approved") or 0),
        amountPaise=int(verdict.get("amountPaise") or 0),
    )


@router.post("/properties/{property_id}/creator-campaigns",
             response_model=CreatorCampaignOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/minute")
async def create_creator_campaign(
    request: Request, property_id: str, body: CreatorCampaignCreateRequest,
    member=Depends(require_member("marketing")),
):
    """
    Put a creator on a campaign.

    The campaign is created as a **draft** and the creator as **invited**, which
    is not a formality: nothing records a touch until somebody presses go live.
    A hotel typing in a partnership it is still negotiating has not started
    crediting clicks to it.

    A creator already on the platform is joined rather than duplicated — one
    handle on one platform is one person, and two rows for the same person is
    how one booking becomes two payouts.
    """
    if not body.campaignId:
        if not (body.campaignName and body.startDate and body.endDate):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "A new campaign needs a name and the dates it runs between.",
            )
        if body.endDate < body.startDate:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "A campaign cannot end before it starts."
            )

    participation = await creators_service.create(
        property_id, member.partner_id, member.member_id,
        {
            "creator_name": body.creatorName,
            "creator_handle": body.creatorHandle,
            "platform": body.platform,
            "followers": body.followers,
            "city": body.city,
            "campaign_id": body.campaignId,
            "campaign_name": body.campaignName,
            "start_date": body.startDate,
            "end_date": body.endDate,
            "commission_pct": body.commissionPct,
            "guest_discount_pct": body.guestDiscountPct,
            "attribution_window_days": body.attributionWindowDays,
            "min_nights": body.minNights,
        },
    )
    for p in await creators_service.list_participations(property_id):
        if p.id == participation["id"]:
            return _campaign_out(p)
    raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                        "The campaign was created but could not be read back")
