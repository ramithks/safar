"""
Google Business Profile — the routes the PWA and Google call.

    PWA     POST   /google/properties/{id}/connect        → authorize url
    Google  GET    /google/oauth/callback                 → 303 to the PWA
    PWA     GET    /google/properties/{id}                → status + profile
    PWA     GET    /google/properties/{id}/locations      → the picker
    PWA     POST   /google/properties/{id}/location       → choose one
    PWA     DELETE /google/properties/{id}                → disconnect
    PWA     GET    /google/properties/{id}/reviews        → rating + reviews
    PWA     POST   /google/properties/{id}/reviews/draft  → AI draft, unpublished
    PWA     POST   /google/properties/{id}/reviews/reply  → publish one reply
    PWA     GET    /google/properties/{id}/posts          → recent posts
    PWA     POST   /google/properties/{id}/posts/draft    → AI draft, unpublished
    PWA     POST   /google/properties/{id}/posts          → publish one post

**Drafting and publishing are separate routes and always will be.** The draft
routes reach Google not at all; the publish routes take text in the request
body. There is no route that generates and publishes in one call, so no bug and
no future refactor can put words on a hotel's public listing that a person did
not read first. That is the single most important property of this file.

**The callback is the only unauthenticated route here**, and it never trusts a
query parameter for which hotel it is acting on — that comes out of the signed
`state` and nowhere else. Same reasoning, same implementation, as the Instagram
callback next door.

Every error an owner can see goes through `messages.humanise`. A raw reason slug
reaching a screen is a bug in this file.
"""
# NOTE: no `from __future__ import annotations` — PEP 563 breaks FastAPI body
# resolution through slowapi's wrapper. Same note as api/v1/pms/endpoint.py.
import logging
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core.limiter import limiter
from app.db.client import run_db
from app.deps.property import require_member
from app.api.v1.google.messages import humanise
from app.services.google import client as gclient
from app.services.google import connection as gconn
from app.services.google import oauth
from app.services.messaging import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/google", tags=["google"])

CHANNEL = "google"


# ── schemas ─────────────────────────────────────────────────────────────────


class ConnectStartOut(BaseModel):
    authorizeUrl: str
    redirectUri: str


class GoogleProfileOut(BaseModel):
    status: str
    connected: bool
    # Present on every non-ok state, already in a hotelier's words.
    message: Optional[str] = None
    actionable: bool = False
    action: Optional[str] = None

    locationName: Optional[str] = None
    title: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    website: Optional[str] = None
    category: Optional[str] = None
    isVerified: bool = False
    mapsUri: Optional[str] = None
    reviewUri: Optional[str] = None
    averageRating: Optional[float] = None
    reviewCount: Optional[int] = None
    lastSyncedAt: Optional[str] = None


class LocationOut(BaseModel):
    accountName: str
    accountDisplayName: str
    locationName: str
    title: str
    address: Optional[str] = None
    isVerified: bool = False
    # True when another property on StaySuite already holds this listing. Shown
    # greyed rather than hidden: an owner who cannot find their hotel in the
    # list needs to know it is taken, not that it is missing.
    alreadyClaimed: bool = False


class ChooseLocationIn(BaseModel):
    accountName: str = Field(min_length=1, max_length=200)
    locationName: str = Field(min_length=1, max_length=200)


class ReviewOut(BaseModel):
    id: str
    name: str
    reviewer: str
    reviewerPhoto: Optional[str] = None
    rating: int
    comment: Optional[str] = None
    createdAt: Optional[str] = None
    reply: Optional[str] = None
    repliedAt: Optional[str] = None


class ReviewsOut(BaseModel):
    averageRating: Optional[float] = None
    totalCount: Optional[int] = None
    reviews: list[ReviewOut] = []
    nextPageToken: Optional[str] = None


class DraftReplyIn(BaseModel):
    reviewName: str = Field(min_length=1, max_length=300)


class DraftOut(BaseModel):
    draft: str


class PublishReplyIn(BaseModel):
    reviewName: str = Field(min_length=1, max_length=300)
    # The text the owner approved. Sent back from the client on purpose: it is
    # what makes "approved" mean the words in front of them rather than whatever
    # the model would produce on a second call.
    comment: str = Field(min_length=1, max_length=4096)


class DraftPostIn(BaseModel):
    brief: str = Field(min_length=1, max_length=500)


class PublishPostIn(BaseModel):
    summary: str = Field(min_length=1, max_length=1500)
    callToActionType: Optional[str] = Field(default=None, max_length=40)
    callToActionUrl: Optional[str] = Field(default=None, max_length=500)


class PostOut(BaseModel):
    name: Optional[str] = None
    summary: Optional[str] = None
    state: Optional[str] = None
    createTime: Optional[str] = None
    searchUrl: Optional[str] = None


# ── helpers ─────────────────────────────────────────────────────────────────


# Google's own list. Closed rather than passed through, because an unknown
# actionType is rejected by Google with a validation error the owner would see
# as "something went wrong".
CTA_TYPES = {"BOOK", "ORDER", "SHOP", "LEARN_MORE", "SIGN_UP", "CALL"}


def _fail(reason: str, *, code: int = status.HTTP_502_BAD_GATEWAY) -> HTTPException:
    """Every error an owner can see is built here, and never from a slug."""
    human = humanise(reason)
    return HTTPException(
        code,
        detail={
            "message": human.message,
            "actionable": human.actionable,
            "action": human.action,
        },
    )


async def _require_connection(property_id: str) -> dict:
    row = await run_db(gconn.get_connection, property_id)
    if not row or row.get("status") in (None, "disconnected"):
        raise _fail("not_connected", code=status.HTTP_409_CONFLICT)
    return row


async def _require_location(property_id: str) -> dict:
    row = await _require_connection(property_id)
    if not row.get("location_name") or not row.get("account_name"):
        # Consent granted, listing not chosen. A real state with its own screen.
        raise _fail("not_connected", code=status.HTTP_409_CONFLICT)
    return row


async def _token(connection: dict) -> str:
    try:
        return await gconn.token_for(connection)
    except oauth.GoogleOAuthError as exc:
        if exc.reason in ("reconnect_required", "not_connected"):
            await run_db(gconn.record_error, connection["property_id"], humanise(exc.reason).message)
        raise _fail(exc.reason, code=status.HTTP_409_CONFLICT) from exc


def _back_to_app(**params) -> RedirectResponse:
    base = settings.pms_app_url.rstrip("/")
    return RedirectResponse(
        url=f"{base}/settings?{urlencode(params)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── connect ─────────────────────────────────────────────────────────────────


@router.post("/properties/{property_id}/connect", response_model=ConnectStartOut)
@limiter.limit("20/hour")
async def start_connect(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """
    Hand the PWA the URL to send the owner to.

    `agents`, not `messaging` — same call as Instagram. A receptionist answers
    guests; connecting the hotel's public Google listing is an owner's act.
    """
    if not oauth.is_configured():
        raise _fail("not_configured", code=status.HTTP_503_SERVICE_UNAVAILABLE)

    state = oauth.issue_state(
        property_id=property_id,
        partner_id=str(member.partner_id),
        profile_id=str(member.profile_id),
    )
    return ConnectStartOut(
        authorizeUrl=oauth.authorize_url(state),
        redirectUri=settings.google_oauth_redirect_uri,
    )


@router.get("/oauth/callback")
@limiter.limit("60/minute")
async def oauth_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """
    Google's redirect back. Always ends in a 303 to the PWA.

    Never a JSON error body: the thing at the other end of this is a hotel owner
    in a browser, and a stack trace is a dead end for them.
    """
    if error:
        logger.info("google consent declined: %s", error)
        return _back_to_app(google="error", reason="consent_declined")

    try:
        claims = oauth.verify_state(state)
    except oauth.GoogleOAuthError as exc:
        # Before touching `code` on purpose: an unverified state means we do not
        # know whose hotel this is, and spending the code anyway would attach a
        # live credential to nothing.
        logger.warning("google callback with bad state: %s", exc)
        return _back_to_app(google="error", reason=exc.reason)

    if not code:
        return _back_to_app(google="error", reason="bad_state")

    property_id = claims["p"]
    partner_id = claims["pa"]

    try:
        tokens = await oauth.exchange_code(code)
    except oauth.GoogleOAuthError as exc:
        logger.warning("google code exchange failed for property %s: %s", property_id, exc)
        return _back_to_app(google="error", reason=exc.reason)

    ref = store.ref_for_property(property_id)
    try:
        await run_db(
            store.save_token,
            channel=CHANNEL,
            credentials_ref=ref,
            property_id=property_id,
            partner_id=partner_id,
            access_token=tokens.access_token,
            expires_in=tokens.expires_in,
            scopes=tokens.scopes or None,
            external_account_id=None,
            refresh_token=tokens.refresh_token,
        )
        await run_db(
            gconn.upsert_pending,
            property_id=property_id,
            partner_id=partner_id,
            credentials_ref=ref,
        )
    except Exception:  # noqa: BLE001
        logger.exception("google connected but could not be stored for property %s", property_id)
        return _back_to_app(google="error", reason="save_failed")

    logger.info("google connected for property %s, awaiting location choice", property_id)
    # `choose` rather than `connected`: consent is done, but the owner still has
    # to say which of their listings this hotel is, and the PWA opens the picker
    # on this parameter.
    return _back_to_app(google="choose")


# ── status ──────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}", response_model=GoogleProfileOut)
@limiter.limit("120/minute")
async def get_profile(
    request: Request,
    property_id: str,
    member=Depends(require_member("messaging")),
):
    """
    What the Channels screen draws. Never 404s and never throws.

    A disconnected Google is a normal state of a working hotel, not an error,
    so this answers 200 with `connected: false` and a sentence. A screen that
    has to catch an exception to render "not connected yet" is a screen that
    shows a red box on a hotel's first day.
    """
    row = await run_db(gconn.get_connection, property_id)

    if not row or row.get("status") == "disconnected":
        human = humanise("not_configured" if not oauth.is_configured() else "not_connected")
        return GoogleProfileOut(
            status="disconnected",
            connected=False,
            message=human.message,
            actionable=human.actionable,
            action=human.action,
        )

    state = row.get("status") or "pending"
    message = None
    actionable = False
    action = None

    if state == "pending":
        message = "Almost there — choose which of your Google listings is this hotel."
        actionable, action = True, "choose"
    elif state == "error":
        message = row.get("last_error") or humanise(None).message
        actionable, action = True, "reconnect"
    elif not row.get("is_verified"):
        human = humanise("not_verified")
        message, actionable = human.message, human.actionable

    return GoogleProfileOut(
        status=state,
        connected=state == "connected",
        message=message,
        actionable=actionable,
        action=action,
        locationName=row.get("location_name"),
        title=row.get("location_title"),
        address=row.get("address"),
        phone=row.get("primary_phone"),
        website=row.get("website_uri"),
        category=row.get("primary_category"),
        isVerified=bool(row.get("is_verified")),
        mapsUri=row.get("maps_uri"),
        reviewUri=row.get("review_uri"),
        averageRating=float(row["average_rating"]) if row.get("average_rating") else None,
        reviewCount=row.get("review_count"),
        lastSyncedAt=row.get("last_synced_at"),
    )


@router.get("/properties/{property_id}/locations", response_model=list[LocationOut])
@limiter.limit("30/minute")
async def list_locations(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """Every hotel on the owner's Google account, for the picker."""
    conn = await _require_connection(property_id)
    token = await _token(conn)

    try:
        accounts = await gclient.list_accounts(token)
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    if not accounts:
        raise _fail("no_accounts", code=status.HTTP_409_CONFLICT)

    claimed = await run_db(_claimed_locations, property_id)

    out: list[LocationOut] = []
    for account in accounts:
        try:
            locations = await gclient.list_locations(token, account.name)
        except gclient.GoogleApiError as exc:
            # One inaccessible account must not empty the whole picker — an
            # agency's personal account often has no locations and 403s.
            logger.info("skipping google account %s: %s", account.name, exc)
            continue

        for loc in locations:
            out.append(
                LocationOut(
                    accountName=account.name,
                    accountDisplayName=account.display_name,
                    locationName=loc.name,
                    title=loc.title,
                    address=loc.address,
                    isVerified=loc.is_verified,
                    alreadyClaimed=loc.name in claimed,
                )
            )

    if not out:
        raise _fail("no_locations", code=status.HTTP_409_CONFLICT)
    return out


def _claimed_locations(property_id: str) -> set[str]:
    """Locations already attached to some *other* property."""
    from app.db.client import get_db

    db = get_db()
    rows = (
        db.table(gconn.TABLE)
        .select("location_name,property_id")
        .eq("status", "connected")
        .execute()
    )
    return {
        r["location_name"]
        for r in (rows.data or [])
        if r.get("location_name") and r.get("property_id") != property_id
    }


@router.post("/properties/{property_id}/location", response_model=GoogleProfileOut)
@limiter.limit("30/minute")
async def choose_location(
    request: Request,
    property_id: str,
    body: ChooseLocationIn,
    member=Depends(require_member("agents")),
):
    """Attach one Google listing to this hotel."""
    conn = await _require_connection(property_id)
    token = await _token(conn)

    claimed = await run_db(_claimed_locations, property_id)
    if body.locationName in claimed:
        raise _fail("already_claimed", code=status.HTTP_409_CONFLICT)

    try:
        # Re-read the location from Google rather than trusting the values the
        # client posted back. The client's copy came from the picker and could
        # be minutes stale; more to the point, it is a client, and this row is
        # what every later review and post call is addressed by.
        location = await gclient.get_location(token, body.locationName)
        accounts = await gclient.list_accounts(token)
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    account = next((a for a in accounts if a.name == body.accountName), None)
    if account is None:
        raise _fail("no_permission", code=status.HTTP_403_FORBIDDEN)

    await run_db(
        gconn.attach_location, property_id=property_id, account=account, location=location
    )
    logger.info("google location attached: property=%s location=%s", property_id, location.name)
    return await get_profile(request, property_id, member)  # type: ignore[arg-type]


@router.delete("/properties/{property_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
async def disconnect(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """Disconnect Google and tell Google to forget us."""
    row = await run_db(gconn.get_connection, property_id)
    if row:
        await gconn.revoke_everything(row)
    return None


# ── reviews ─────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/reviews", response_model=ReviewsOut)
@limiter.limit("60/minute")
async def list_reviews(
    request: Request,
    property_id: str,
    pageToken: Optional[str] = Query(None, max_length=2000),
    member=Depends(require_member("messaging")),
):
    conn = await _require_location(property_id)
    token = await _token(conn)

    try:
        page = await gclient.list_reviews(
            token,
            account_name=conn["account_name"],
            location_name=conn["location_name"],
            page_token=pageToken,
        )
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    # Only on the first page: a later page carries the same totals and rewriting
    # the row on every scroll is a write per keystroke for no new information.
    if not pageToken:
        await run_db(
            gconn.record_rating,
            property_id,
            average=page.average_rating,
            count=page.total_count,
        )

    return ReviewsOut(
        averageRating=page.average_rating,
        totalCount=page.total_count,
        nextPageToken=page.next_page_token,
        reviews=[
            ReviewOut(
                id=r.review_id,
                name=r.name,
                reviewer=r.reviewer_name,
                reviewerPhoto=r.reviewer_photo,
                rating=r.star_rating,
                comment=r.comment,
                createdAt=r.create_time,
                reply=r.reply_comment,
                repliedAt=r.reply_time,
            )
            for r in page.reviews
        ],
    )


@router.post("/properties/{property_id}/reviews/draft", response_model=DraftOut)
@limiter.limit("30/minute")
async def draft_reply(
    request: Request,
    property_id: str,
    body: DraftReplyIn,
    member=Depends(require_member("messaging")),
):
    """
    Write a reply. **Publishes nothing.**

    The review is re-read from Google rather than taken from the request, so the
    draft answers what the guest actually wrote and not what a client says they
    wrote.
    """
    conn = await _require_location(property_id)
    token = await _token(conn)

    if not body.reviewName.startswith(f"{conn['account_name']}/{conn['location_name']}/reviews/"):
        # The only tenant check that matters on this route: a review name is a
        # full resource path, and accepting an arbitrary one would draft (and
        # then let an owner publish) against another hotel's listing.
        raise _fail("not_found", code=status.HTTP_404_NOT_FOUND)

    try:
        page = await gclient.list_reviews(
            token,
            account_name=conn["account_name"],
            location_name=conn["location_name"],
            page_size=50,
        )
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    review = next((r for r in page.reviews if r.name == body.reviewName), None)
    if review is None:
        raise _fail("not_found", code=status.HTTP_404_NOT_FOUND)

    property_name = await run_db(_property_name, property_id)
    try:
        draft = await gconn.draft_review_reply(property_name=property_name, review=review)
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("google review draft failed for property %s", property_id)
        raise _fail("draft_failed") from exc

    return DraftOut(draft=draft)


@router.post("/properties/{property_id}/reviews/reply", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/hour")
async def publish_reply(
    request: Request,
    property_id: str,
    body: PublishReplyIn,
    member=Depends(require_member("agents")),
):
    """
    Publish the reply the owner approved.

    `agents`, not `messaging`: this writes in the hotel's name on a public page
    that outlives the conversation. And the text comes from the body — this
    route never calls the model, which is what makes owner approval real.
    """
    conn = await _require_location(property_id)
    token = await _token(conn)

    if not body.reviewName.startswith(f"{conn['account_name']}/{conn['location_name']}/reviews/"):
        raise _fail("not_found", code=status.HTTP_404_NOT_FOUND)

    try:
        await gclient.reply_to_review(token, review_name=body.reviewName, comment=body.comment)
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    logger.info("google review replied: property=%s review=%s", property_id, body.reviewName)
    return None


# ── posts ───────────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/posts", response_model=list[PostOut])
@limiter.limit("60/minute")
async def list_posts(
    request: Request,
    property_id: str,
    member=Depends(require_member("messaging")),
):
    conn = await _require_location(property_id)
    token = await _token(conn)

    try:
        rows = await gclient.list_local_posts(
            token, account_name=conn["account_name"], location_name=conn["location_name"]
        )
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    return [
        PostOut(
            name=r.get("name"),
            summary=r.get("summary"),
            state=r.get("state"),
            createTime=r.get("createTime"),
            searchUrl=r.get("searchUrl"),
        )
        for r in rows
    ]


@router.post("/properties/{property_id}/posts/draft", response_model=DraftOut)
@limiter.limit("30/minute")
async def draft_post(
    request: Request,
    property_id: str,
    body: DraftPostIn,
    member=Depends(require_member("messaging")),
):
    """Write a post. **Publishes nothing.**"""
    await _require_location(property_id)
    name, city = await run_db(_property_name_and_city, property_id)

    try:
        draft = await gconn.draft_post(property_name=name, city=city, brief=body.brief)
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("google post draft failed for property %s", property_id)
        raise _fail("draft_failed") from exc

    return DraftOut(draft=draft)


@router.post("/properties/{property_id}/posts", response_model=PostOut, status_code=201)
@limiter.limit("30/hour")
async def publish_post(
    request: Request,
    property_id: str,
    body: PublishPostIn,
    member=Depends(require_member("agents")),
):
    """Publish the post the owner approved. Never calls the model."""
    conn = await _require_location(property_id)
    token = await _token(conn)

    cta_type = (body.callToActionType or "").upper() or None
    if cta_type and cta_type not in CTA_TYPES:
        raise _fail("request_failed", code=status.HTTP_422_UNPROCESSABLE_ENTITY)

    try:
        created = await gclient.create_local_post(
            token,
            account_name=conn["account_name"],
            location_name=conn["location_name"],
            summary=body.summary,
            call_to_action_type=cta_type,
            call_to_action_url=body.callToActionUrl,
        )
    except gclient.GoogleApiError as exc:
        raise _fail(exc.reason) from exc

    logger.info("google post published: property=%s", property_id)
    return PostOut(
        name=created.get("name"),
        summary=created.get("summary"),
        state=created.get("state"),
        createTime=created.get("createTime"),
        searchUrl=created.get("searchUrl"),
    )


# ── small reads ─────────────────────────────────────────────────────────────


def _property_name(property_id: str) -> str:
    from app.db.client import get_db

    db = get_db()
    row = db.table("properties").select("name").eq("id", property_id).limit(1).execute()
    return str((row.data or [{}])[0].get("name") or "the hotel")


def _property_name_and_city(property_id: str) -> tuple[str, str | None]:
    from app.db.client import get_db

    db = get_db()
    row = db.table("properties").select("name,city").eq("id", property_id).limit(1).execute()
    record = (row.data or [{}])[0]
    return str(record.get("name") or "the hotel"), record.get("city")
