"""
Instagram Business Login — the routes Meta and the PWA call.

    PWA   POST /messaging/properties/{id}/instagram/connect   → authorize url
    Meta  GET  /messaging/instagram/oauth/callback            → 302 to the PWA
    Meta  POST /messaging/instagram/deauthorize               → signed_request
    Meta  POST /messaging/instagram/data-deletion             → signed_request

**Three of these four are unauthenticated and cannot be otherwise.** Meta holds
no JWT of ours. What stands in for a session is different in each case and is
the security property of the route:

    callback        a signed `state` we issued, naming the property, expiring
                    in ten minutes
    deauthorize     Meta's `signed_request`, HMAC'd with the app secret
    data-deletion   the same

The callback never trusts a query parameter for *which hotel this is*. That
comes out of the signed state and nowhere else; taking a `property_id` from the
URL would let anyone attach their own Instagram account to someone else's hotel
by editing a link.

The callback always redirects — success and failure alike — because the person
at the other end is a hotel owner in a browser, not a client library. A JSON
error body at the end of an OAuth flow is a dead end with a stack trace in it.
"""
# NOTE: no `from __future__ import annotations` — PEP 563 breaks FastAPI body
# resolution through slowapi's wrapper. Same note as api/v1/pms/endpoint.py.
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from pydantic import BaseModel
from fastapi.responses import RedirectResponse

from app.config import settings
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.property import require_member
from app.services.messaging import store
from app.services.messaging.crypto import CredentialCryptoError
from app.services.messaging.instagram import oauth as ig_oauth
from app.services.messaging.whatsapp import onboarding as wa_onboarding

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messaging", tags=["messaging"])

CHANNEL = "instagram"


def _back_to_app(**params) -> RedirectResponse:
    """Everything the owner sees at the end of the flow is this redirect."""
    base = settings.pms_app_url.rstrip("/")
    return RedirectResponse(
        url=f"{base}/settings?{urlencode(params)}",
        # 303: the callback is a GET but the browser must not re-issue it if the
        # owner refreshes the settings page — the code is single-use and the
        # second attempt would fail with a confusing "code already used".
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── start ───────────────────────────────────────────────────────────────────


@router.post("/properties/{property_id}/instagram/connect")
@limiter.limit("20/hour")
async def start_instagram_connect(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """
    Hand the PWA the URL to send the owner to.

    Returns the URL instead of redirecting: the PWA opens it as a top-level
    navigation, and a 302 out of an XHR would be followed by fetch and land the
    consent screen inside a response body.

    `agents`, not `messaging` — a receptionist answers guests, and connecting or
    disconnecting the hotel's Instagram account is an owner's or manager's act.
    """
    if not ig_oauth.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Instagram connections are not switched on yet.",
        )

    state = ig_oauth.issue_state(
        property_id=property_id,
        partner_id=str(member.partner_id),
        profile_id=str(member.profile_id),
    )
    return {
        "authorizeUrl": ig_oauth.authorize_url(state),
        "redirectUri": settings.instagram_redirect_uri,
    }


@router.post("/properties/{property_id}/instagram/disconnect")
@limiter.limit("20/hour")
async def disconnect_instagram(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """
    Stop answering on this hotel's Instagram.

    **The button existed and there was no route behind it.** `get_channels` has
    always returned `action: "Disconnect"` on a connected row, and the Channels
    screen rendered it — clicking it did nothing at all, because the client had
    nowhere to send it and no other surface offered the act either. A hotel that
    wanted to stop had to go and remove the app on instagram.com and wait for
    Meta to call `/deauthorize`.

    The credential is revoked first and the row marked second, in that order:
    a row that says "disconnected" while a live token is still in the store is
    an account we can still post to, which is the direction of this pair that
    actually matters.

    `agents`, matching connect — turning the hotel's Instagram off is an
    owner's act, not a receptionist's.
    """

    def _disconnect() -> dict:
        db = get_db()
        rows = (
            db.table("instagram_connections")
            .select("instagram_account_id,credentials_ref,status")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        if not rows:
            return {"disconnected": False, "revoked": 0}
        row = rows[0]

        revoked = 0
        if row.get("credentials_ref"):
            revoked = 1 if store.revoke(
                channel=CHANNEL, credentials_ref=row["credentials_ref"],
            ) else 0
        elif row.get("instagram_account_id"):
            revoked = len(store.revoke_by_account(
                channel=CHANNEL, external_account_id=row["instagram_account_id"],
            ))

        # `last_error` is cleared, not set. This was a deliberate act by the
        # hotel, and a red "needs reconnecting" on a channel somebody switched
        # off on purpose is the screen telling an owner their setup is broken.
        (
            db.table("instagram_connections")
            .update({"status": "disconnected", "last_error": None})
            .eq("property_id", property_id)
            .execute()
        )
        return {"disconnected": True, "revoked": revoked}

    result = await run_db(_disconnect)
    logger.info("instagram disconnected: property=%s revoked=%s",
                property_id, result.get("revoked"))
    return result


# ── callback ────────────────────────────────────────────────────────────────


def _persist(claims: dict, account: ig_oauth.ConnectedAccount) -> dict:
    """Store the token, then point the connection row at it. In that order."""
    property_id = claims["p"]
    partner_id = claims["pa"]
    ref = store.ref_for_property(property_id)

    store.save_token(
        channel=CHANNEL,
        credentials_ref=ref,
        property_id=property_id,
        partner_id=partner_id,
        access_token=account.access_token,
        expires_in=account.expires_in,
        scopes=account.scopes,
        external_account_id=account.instagram_account_id,
    )

    db = get_db()
    row = {
        "property_id": property_id,
        "partner_id": partner_id,
        "instagram_account_id": account.instagram_account_id,
        "username": account.username,
        "credentials_ref": ref,
        "status": "connected",
        # The whole point of the flow. A row left sandboxed here would consume
        # a real consent and still answer guests with the fake.
        "is_sandbox": False,
        "last_error": None,
        "connected_at": "now()",
    }
    result = (
        db.table("instagram_connections")
        .upsert(row, on_conflict="instagram_account_id")
        .execute()
    )
    return (result.data or [{}])[0]


@router.get("/instagram/oauth/callback")
@limiter.limit("60/minute")
async def instagram_oauth_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_reason: str | None = Query(None),
    error_description: str | None = Query(None),
):
    """Meta's redirect back. Always ends in a 303 to the PWA."""
    # The owner pressed Cancel. Not an error worth logging as one.
    if error:
        logger.info("instagram consent declined: %s / %s", error, error_reason)
        return _back_to_app(instagram="error", reason=error_reason or error)

    try:
        claims = ig_oauth.verify_state(state)
    except ig_oauth.InstagramOAuthError as exc:
        # Deliberately before touching `code`: an unverified state means we do
        # not know whose hotel this is, and exchanging the code anyway would
        # spend it on a connection we could not attribute.
        logger.warning("instagram callback with bad state: %s", exc)
        return _back_to_app(instagram="error", reason=exc.reason)

    if not code:
        return _back_to_app(instagram="error", reason="no_code")

    try:
        account = await ig_oauth.connect(code)
    except ig_oauth.InstagramOAuthError as exc:
        logger.warning("instagram code exchange failed for property %s: %s",
                       claims.get("p"), exc)
        return _back_to_app(instagram="error", reason=exc.reason)

    try:
        await run_db(_persist, claims, account)
    except CredentialCryptoError:
        # **Retrying cannot help, so the owner must not be told to.**
        #
        # This is `MESSAGING_CREDENTIALS_KEY` missing from the deployment: the
        # consent succeeded, Meta handed us a real token, and we have no key to
        # encrypt it with. It happened in production and the hotelier tried
        # three times in thirty-five seconds, because the screen said "please
        # try again" — which is the correct message for a dropped connection
        # and a cruel one for a configuration we have not done.
        #
        # Logged as an error with the fix in the message, because the person
        # who can act on it is us and the only place they will look is here.
        logger.error(
            "instagram connected for property %s but MESSAGING_CREDENTIALS_KEY is "
            "not set on this deployment — no hotel can connect a channel until "
            "it is. Generate one with: python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\"",
            claims.get("p"),
        )
        return _back_to_app(instagram="error", reason="save_not_configured")
    except Exception:  # noqa: BLE001
        # The token is valid and we could not keep it. Say so plainly rather
        # than sending the owner back to a screen that claims success.
        logger.exception("instagram connected but could not be stored for property %s",
                         claims.get("p"))
        return _back_to_app(instagram="error", reason="save_failed")

    logger.info("instagram connected: property=%s account=%s (@%s)",
                claims.get("p"), account.instagram_account_id, account.username)
    return _back_to_app(instagram="connected", account=account.username or "")


# ── the Channels screen ─────────────────────────────────────────────────────


class ChannelOut(BaseModel):
    """
    One row on the Channels screen.

    Everything here is already in a hotelier's words. There is no slug, no
    provider name and no status code, because this response *is* what gets
    rendered — a screen that has to translate a backend enum into a sentence is
    a screen where the two drift apart.
    """

    id: str
    name: str
    # live · connected · action_needed · coming_soon · not_connected
    state: str
    headline: str
    detail: str
    # What the button says, or null when there is nothing to press.
    action: str | None = None
    # Where an already-live channel can be seen.
    url: str | None = None


class ChannelsOut(BaseModel):
    channels: list[ChannelOut]
    # "Your AI employee is answering in 2 places."
    liveCount: int


def instagram_row(connection: dict | None) -> ChannelOut:
    """
    What every surface says about this hotel's Instagram.

    **Extracted because two screens used to disagree.** The Channels page read
    this decision from the server; the Settings page rendered an unconditional
    "Connect Instagram" button that queried nothing at all — so a hotel with
    Instagram connected saw "Connected" on one screen and an invitation to
    connect on the other, and refreshing never helped because the second screen
    held no state to be stale. Both clients now read this one row through one
    cached query (see `useChannel`), and this function is the only thing that
    turns a connection into words.

    Pure, and takes the row rather than a property id, so the mapping can be
    asserted without a database — which is what makes "every state has a
    defined answer" a test rather than an intention.
    """
    status = (connection or {}).get("status")

    if connection and status == "connected" and not connection.get("is_sandbox"):
        handle = connection.get("username")
        return ChannelOut(
            id="instagram", name="Instagram", state="connected",
            headline=f"@{handle}" if handle else "Your Instagram account",
            detail="Your AI employee answers Instagram messages.",
            action="Disconnect",
        )

    if connection and status not in (None, "connected", "disconnected"):
        return ChannelOut(
            id="instagram", name="Instagram", state="action_needed",
            headline="Needs reconnecting",
            detail=connection.get("last_error")
                   or "Instagram messages aren't reaching your AI employee. Reconnect to fix it.",
            action="Reconnect",
        )

    # `disconnected` lands here rather than in the branch above.
    #
    # A hotel that pressed Disconnect has a row saying so, and reading that as
    # "needs reconnecting" put an amber "Needs you" badge on a channel somebody
    # had deliberately switched off — the screen telling an owner their setup is
    # broken because they used it. What is offered is Connect, the true next
    # step. A sandbox row lands here too: it is not a connection a guest's
    # message can reach.
    was_connected = bool(connection and status == "disconnected")
    return ChannelOut(
        id="instagram", name="Instagram", state="not_connected",
        headline="Not connected",
        detail=(
            "Your AI employee has stopped answering here. Connect again whenever you like."
            if was_connected
            else "Connect the account guests already message you on."
        ),
        action="Connect Instagram",
    )


def whatsapp_row(connection: dict | None, *, onboarding_enabled: bool) -> ChannelOut:
    """
    What every surface says about this hotel's WhatsApp.

    **This row used to be a hardcoded "coming soon".** The pipeline behind it
    has been real since migration 11 — signed webhook, property resolution,
    24-hour window, templates, delivery receipts — but there was exactly one
    connection in the table and it was ours, so the honest answer to a hotelier
    was "soon". Embedded Signup is what makes it theirs, and this function is
    what turns a connection into words.

    Pure, and takes the row rather than a property id, so the mapping can be
    asserted without a database — same as `instagram_row`, and for the same
    reason: "every state has a defined answer" should be a test.
    """
    status = (connection or {}).get("status")

    if connection and status == "connected" and not connection.get("is_sandbox"):
        number = connection.get("display_number")
        quality = (connection.get("quality_rating") or "").upper()
        detail = "Your AI employee answers WhatsApp messages."
        # Only ever mentioned when it is bad news a hotel can act on. A GREEN
        # rating is the normal state and saying so on every page load teaches an
        # owner to read a health metric they cannot influence.
        if quality == "RED":
            detail = ("Your AI employee answers WhatsApp messages. Meta has "
                      "marked this number's quality as low — too many guests "
                      "have blocked or reported it.")
        elif quality == "YELLOW":
            detail = ("Your AI employee answers WhatsApp messages. Meta has "
                      "flagged this number's quality; keep an eye on it.")
        return ChannelOut(
            id="whatsapp", name="WhatsApp", state="connected",
            headline=number or connection.get("verified_name") or "Your WhatsApp number",
            detail=detail,
            action="Disconnect",
        )

    # Claimed from a Meta-hosted signup and waiting for the one popup that mints
    # a token. Not an error and not connected — a real half-finished state that
    # would otherwise read as either.
    if connection and status == "pending" and not connection.get("is_sandbox"):
        return ChannelOut(
            id="whatsapp", name="WhatsApp", state="action_needed",
            headline="Almost there",
            detail=connection.get("last_error")
                   or "Finish connecting and your AI employee starts answering on WhatsApp.",
            action="Finish connecting",
        )

    if connection and status not in (None, "connected", "disconnected", "pending"):
        return ChannelOut(
            id="whatsapp", name="WhatsApp", state="action_needed",
            headline="Needs reconnecting",
            detail=connection.get("last_error")
                   or "WhatsApp messages aren't reaching your AI employee. Reconnect to fix it.",
            action="Reconnect",
        )

    # Nothing a hotel can connect yet on this deployment. Honest rather than
    # absent: WhatsApp is the first thing an Indian hotelier asks about, and a
    # missing row reads as "never" while a Connect button that 503s reads as
    # "broken".
    #
    # The caller passes `onboarding.is_configured()`, which is all four of the
    # flag, the app id, the app secret and the configuration id — not the flag
    # on its own. A half-configured deployment lands here and says "soon",
    # which is true.
    if not onboarding_enabled:
        return ChannelOut(
            id="whatsapp", name="WhatsApp", state="coming_soon",
            headline="Launching soon",
            detail="Your AI employee will answer WhatsApp messages too.",
        )

    # `disconnected` lands here rather than in the branch above, exactly as it
    # does for Instagram: a hotel that pressed Disconnect has a row saying so,
    # and reading that as "needs reconnecting" puts an amber badge on something
    # somebody switched off deliberately. A sandbox row lands here too — it is
    # not a connection a guest's message can reach.
    was_connected = bool(connection and status == "disconnected")
    return ChannelOut(
        id="whatsapp", name="WhatsApp", state="not_connected",
        headline="Not connected",
        detail=(
            "Your AI employee has stopped answering here. Connect again whenever you like."
            if was_connected
            else "Connect your hotel's WhatsApp number. Guests message the number "
                 "they already have, and your AI employee answers."
        ),
        action="Connect WhatsApp",
    )


@router.get("/properties/{property_id}/channels", response_model=ChannelsOut)
@limiter.limit("120/minute")
async def get_channels(
    request: Request,
    property_id: str,
    member=Depends(require_member("messaging")),
):
    """
    Everywhere this hotel's AI employee can meet a guest, and whether it does.

    One call for one screen, deliberately. The alternative was four reads the
    client stitches together, which is four loading states, four failure modes
    and four chances for the screen to say "Instagram: —" while it waits.

    **A channel that is not available yet says so honestly.** WhatsApp is not
    an error and is not "disconnected"; it is coming, and the row says that.
    Showing a red disconnected badge for something a hotel cannot connect yet
    teaches an owner their setup is broken.
    """

    def _read() -> dict:
        db = get_db()
        site = (
            db.table("hotel_websites")
            .select("status,domain,travbuddy_path")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        instagram = (
            db.table("instagram_connections")
            .select("username,status,is_sandbox,last_error")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        whatsapp = (
            db.table("whatsapp_connections")
            .select("display_number,verified_name,status,is_sandbox,last_error,quality_rating")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        prop = (
            db.table("properties").select("slug")
            .eq("id", property_id).limit(1).execute().data or [{}]
        )[0]
        # Not read at all while Google is out of launch scope — see
        # `settings.enable_google_business_profile`. A deployment that has
        # never had the table should not be paying a failed query per screen
        # load to find that out.
        google: list[dict] = []
        if settings.enable_google_business_profile:
            try:
                google = (
                    db.table("google_connections")
                    .select("status,location_title,is_verified,average_rating,review_count")
                    .eq("property_id", property_id).limit(1).execute().data or []
                )
            except Exception:  # noqa: BLE001
                google = []
        return {
            "site": site[0] if site else None,
            "instagram": instagram[0] if instagram else None,
            "whatsapp": whatsapp[0] if whatsapp else None,
            "google": google[0] if google else None,
            "slug": prop.get("slug") or "",
        }

    found = await run_db(_read)
    base = settings.pms_app_url.rstrip("/")
    channels: list[ChannelOut] = []

    # ── the hotel's own website ──
    site = found["site"]
    if site and site.get("status") == "published":
        # A custom domain wins; otherwise the path the website service wrote at
        # publish time; otherwise the slug. Three fallbacks because a hotel that
        # is live must always have somewhere to click through to.
        path = site.get("travbuddy_path") or f"/stay/{found['slug']}"
        live_url = f"https://{site['domain']}" if site.get("domain") else f"{base}{path}"
        # The headline is the address, not the word "Live" — the badge beside it
        # already says that, and what an owner actually wants to read here is
        # where their site is.
        shown = site["domain"] if site.get("domain") else live_url.split("://", 1)[-1]
        channels.append(ChannelOut(
            id="website", name="Hotel website", state="live",
            headline=shown,
            detail="Guests can browse rooms, ask questions and book direct.",
            action="View site", url=live_url,
        ))
    else:
        channels.append(ChannelOut(
            id="website", name="Hotel website", state="not_connected",
            headline="Not published yet",
            detail="Publish your website and guests can book you direct, with your "
                   "AI employee answering on the page.",
            action="Set up website",
        ))

    # ── Instagram ──
    channels.append(instagram_row(found["instagram"]))

    # ── WhatsApp ──
    #
    # Above Google and above the "not yet" rows, because it is the channel an
    # Indian hotelier asks about first.
    # `is_configured()`, not the bare flag. The flag alone is not enough to
    # connect a hotel — Embedded Signup also needs the app id, the app secret
    # and the configuration id — and a screen that reads only the flag offers a
    # Connect button that 503s the moment somebody switches onboarding on before
    # pasting the configuration id. Which is exactly the order a deploy happens
    # in. The endpoint has always checked all four; this makes the screen agree.
    channels.append(whatsapp_row(
        found["whatsapp"],
        onboarding_enabled=wa_onboarding.is_configured(),
    ))

    # ── Google Business Profile ──
    #
    # Written, tested, and not shown. StayOS launches on the two channels a
    # hotel can switch on for itself; Google needs an approved access request
    # from us before a review reply or a post works at all, so a row here would
    # be a Connect button leading to a wall. It is not listed as "coming soon"
    # either — a hotelier weighing this product up is not asking about Google,
    # and a soon that has no date is noise on the one screen that should read
    # as a short, finishable list.
    if settings.enable_google_business_profile:
        google = found["google"]
        state = (google or {}).get("status")
        if state == "connected":
            rating = (google or {}).get("average_rating")
            count = (google or {}).get("review_count")
            detail = "Manage your Google presence and reply to reviews from here."
            if rating and count:
                detail = f"{rating}★ from {count} reviews. Reply to them from here."
            channels.append(ChannelOut(
                id="google", name="Google Business Profile", state="connected",
                headline=google.get("location_title") or "Connected",
                detail=detail, action="Manage",
            ))
        elif state == "pending":
            channels.append(ChannelOut(
                id="google", name="Google Business Profile", state="action_needed",
                headline="Almost there",
                detail="Choose which of your Google listings is this hotel.",
                action="Choose listing",
            ))
        elif state == "error":
            channels.append(ChannelOut(
                id="google", name="Google Business Profile", state="action_needed",
                headline="Needs reconnecting",
                detail="Your Google connection has expired. Reconnect it and we'll pick up "
                       "where we left off.",
                action="Reconnect",
            ))
        else:
            channels.append(ChannelOut(
                id="google", name="Google Business Profile", state="not_connected",
                headline="Not connected",
                detail="Travellers find you on Google. Reply to reviews and post offers "
                       "without leaving StayOS.",
                action="Connect Google",
            ))

    # ── not yet ──
    #
    # Listed rather than hidden, on the same argument that used to apply to
    # WhatsApp: a hotelier deciding whether to move their enquiries here wants
    # to know where this goes next, and an absent row reads as "never".
    #
    # WhatsApp has moved out of this section. It is a real row now — see
    # `whatsapp_row`, which still returns `coming_soon` on a deployment where
    # Embedded Signup is not switched on.
    channels.append(ChannelOut(
        id="messenger", name="Facebook Messenger", state="coming_soon",
        headline="Coming soon",
        detail="One AI employee, more places to meet your guests.",
    ))

    return ChannelsOut(
        channels=channels,
        liveCount=sum(1 for c in channels if c.state in ("live", "connected")),
    )


# ── deauthorize and data deletion ───────────────────────────────────────────
#
# Meta requires both URLs before Instagram Business Login can be configured, and
# calls them when a person removes the app or asks for their data back. Both
# take a `signed_request` and nothing else.


def _verified_claims(signed_request: str | None) -> dict:
    app_secret = ig_oauth.client_secret()
    try:
        return ig_oauth.parse_signed_request(signed_request or "", app_secret)
    except ig_oauth.InstagramOAuthError as exc:
        logger.warning("instagram signed_request refused: %s", exc)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signed_request") from exc


@router.post("/instagram/deauthorize")
@limiter.limit("60/minute")
async def instagram_deauthorize(request: Request, signed_request: str = Form(default="")):
    """
    The hotel removed our app from its Instagram account.

    Revoke the credential and mark the connection disconnected. The token is
    already dead at Meta; the point of doing it here is that the next send fails
    with "reconnect Instagram" instead of an opaque 190 from the adapter.
    """
    claims = _verified_claims(signed_request)
    account_id = str(claims.get("user_id") or "")
    if not account_id:
        return {"received": True, "revoked": 0}

    def _revoke() -> int:
        revoked = store.revoke_by_account(channel=CHANNEL, external_account_id=account_id)
        db = get_db()
        (
            db.table("instagram_connections")
            .update({"status": "disconnected",
                     "last_error": "The hotel removed StayOS from this Instagram account."})
            .eq("instagram_account_id", account_id)
            .execute()
        )
        return len(revoked)

    count = await run_db(_revoke)
    logger.info("instagram deauthorized: account=%s credentials revoked=%s", account_id, count)
    return {"received": True, "revoked": count}


@router.post("/instagram/data-deletion")
@limiter.limit("60/minute")
async def instagram_data_deletion(request: Request, signed_request: str = Form(default="")):
    """
    A data deletion request from Meta.

    Meta wants a JSON body with a URL a person can check and a confirmation
    code. What is deleted is the credential and the connection — the guest
    conversations belong to the hotel's own booking records and are not Meta's
    to erase, which is why this revokes rather than cascading.
    """
    claims = _verified_claims(signed_request)
    account_id = str(claims.get("user_id") or "")

    def _delete() -> int:
        revoked = store.revoke_by_account(channel=CHANNEL, external_account_id=account_id)
        db = get_db()
        (
            db.table("instagram_connections")
            .update({"status": "disconnected",
                     "last_error": "Data deletion requested through Instagram."})
            .eq("instagram_account_id", account_id)
            .execute()
        )
        return len(revoked)

    count = await run_db(_delete) if account_id else 0
    logger.info("instagram data deletion: account=%s revoked=%s", account_id, count)

    base = settings.pms_app_url.rstrip("/")
    return {
        "url": f"{base}/settings?instagram=deleted",
        "confirmation_code": f"ig-{account_id or 'unknown'}",
    }
