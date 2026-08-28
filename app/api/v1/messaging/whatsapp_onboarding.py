"""
Embedded Signup — the routes the PWA calls to connect a hotel's own WhatsApp.

    PWA  POST /messaging/properties/{id}/whatsapp/start       → what the popup needs
    PWA  POST /messaging/properties/{id}/whatsapp/finish      → code → a live number
    PWA  POST /messaging/properties/{id}/whatsapp/disconnect
    PWA  GET  /messaging/properties/{id}/whatsapp/claimable   → Meta-hosted arrivals
    PWA  POST /messaging/properties/{id}/whatsapp/claim
    PWA  GET  /messaging/properties/{id}/whatsapp/templates

**Unlike Instagram, none of these is unauthenticated.** There is no redirect
here: Embedded Signup hands the code back to the window that opened it by
`postMessage`, so the PWA posts it to us with the caller's JWT already attached.
That removes the whole class of problem `instagram/oauth.py` guards against with
a signed state on a public callback — and the state is still signed anyway,
because the Meta-hosted path has no window to post from and because a `finish`
whose state does not match a `start` is a replay.

Its own module rather than more of `oauth.py`, which is 600 lines of Instagram.

── the one rule that matters ────────────────────────────────────────────────

**The `wabaId` in the request body is a string the browser chose.** Everything
downstream — which templates get created, whose guests reach this hotel — turns
on it being right. So it is checked against `debug_token`'s `target_ids`, which
is Meta's own answer to "what may this token act on", and a mismatch is a 403
rather than a warning. Trusting it would let anyone who can call `finish`
attach someone else's WhatsApp account to their own hotel.
"""
# NOTE: no `from __future__ import annotations` — PEP 563 breaks FastAPI body
# resolution through slowapi's wrapper. Same note as api/v1/pms/endpoint.py.
import logging
from datetime import datetime, timezone
from functools import partial

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import settings
from app.core.limiter import limiter
from app.db.client import get_db, run_db
from app.deps.property import require_member
from app.services.messaging import credentials as creds_module
from app.services.messaging import crypto, store
from app.services.messaging.channels import MessagingError
from app.services.messaging.crypto import CredentialCryptoError
from app.services.messaging.whatsapp import onboarding, provisioning

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messaging", tags=["messaging"])

CHANNEL = "whatsapp"


# ── shapes ──────────────────────────────────────────────────────────────────


class StartOut(BaseModel):
    """Everything the popup needs, and nothing it does not."""

    appId: str
    configId: str
    state: str
    graphVersion: str
    # Which Embedded Signup variant to run. Must match the configuration —
    # see `settings.whatsapp_signup_feature_type`.
    featureType: str = ""
    # Null unless a Meta-hosted landing page is configured. The PWA offers it as
    # "send this hotel a link instead" — the sales-led path.
    metaHostedUrl: str | None = None
    onboardingId: str


class FinishIn(BaseModel):
    state: str
    code: str
    wabaId: str = Field(min_length=1)
    phoneNumberId: str = Field(min_length=1)
    # Only present when the hotel already had two-step verification on the
    # number. See `onboarding.register` — no PIN we generate can match one the
    # hotel set themselves.
    pin: str | None = Field(default=None, min_length=6, max_length=6)


class ClaimIn(BaseModel):
    """
    Which arrival to attach, named one of two ways.

    `onboardingId` is for a row this partner *started* — we issued the link, so
    the row already carries their partner id and `/claimable` can list it.

    `wabaId` is for the harder case, and the one the Meta-hosted link actually
    produces: a completion that arrives with nothing but Meta's ids on it. There
    is no list to pick from, deliberately — enumerating unattributed arrivals
    would show every hotel that had just onboarded, and claiming one is claiming
    their guests. Instead the owner types the WhatsApp Business Account ID from
    their own WhatsApp Manager, which is a thing only somebody with access to
    that account can read off a screen.
    """

    onboardingId: str | None = None
    wabaId: str | None = None


class TestMessageIn(BaseModel):
    """
    One number, given deliberately.

    No list, no default, no "send to everyone on the account". A diagnostic that
    could quietly widen into outbound traffic would be the worst possible way to
    discover a mistake — the same rule `scripts/meta_smoke.py` follows.
    """

    to: str = Field(min_length=8, max_length=20)


class TemplateOut(BaseModel):
    purpose: str
    language: str
    status: str
    category: str | None = None
    preview: str | None = None
    rejectedReason: str | None = None


# ── start ───────────────────────────────────────────────────────────────────


@router.post("/properties/{property_id}/whatsapp/start", response_model=StartOut)
@limiter.limit("20/hour")
async def start_whatsapp_connect(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """
    Hand the PWA what it needs to open Meta's popup.

    `agents`, matching Instagram: connecting the hotel's WhatsApp is an owner's
    or manager's act, not a receptionist's.
    """
    if not onboarding.is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WhatsApp connections are not switched on yet.",
        )
    # Checked here rather than at `finish`, because the failure is ours and the
    # hotel would otherwise discover it *after* granting Meta consent — the
    # exact mistake `instagram/oauth.py` documents at `save_not_configured`,
    # where a hotelier retried three times in thirty-five seconds.
    if not crypto.is_configured():
        logger.error(
            "a hotel tried to connect WhatsApp but MESSAGING_CREDENTIALS_KEY is not "
            "set on this deployment — no hotel can connect a channel until it is. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "WhatsApp connections aren't set up correctly on our side. "
            "We've been notified.",
        )

    signed, nonce = onboarding.issue_state(
        property_id=property_id,
        partner_id=str(member.partner_id),
        profile_id=str(member.profile_id),
    )

    def _record() -> dict:
        db = get_db()
        result = (
            db.table("whatsapp_onboardings")
            .insert({
                "property_id": property_id,
                "partner_id": str(member.partner_id),
                "profile_id": str(member.profile_id),
                "source": "embedded",
                "status": "started",
                "state_nonce": nonce,
            })
            .execute()
        )
        return (result.data or [{}])[0]

    row = await run_db(_record)
    return StartOut(
        appId=str(onboarding.app_id()),
        configId=str(settings.whatsapp_config_id),
        state=signed,
        graphVersion=settings.whatsapp_api_base.rstrip("/").rsplit("/", 1)[-1],
        featureType=settings.whatsapp_signup_feature_type,
        metaHostedUrl=onboarding.meta_hosted_url(nonce),
        onboardingId=str(row.get("id") or ""),
    )


# ── finish ──────────────────────────────────────────────────────────────────


def _persist(
    *, claims: dict, account: onboarding.Account, number: onboarding.PhoneNumber,
    token: onboarding.BusinessToken, pin: str, onboarding_id: str | None,
) -> dict:
    """
    Store the token, then point the connection row at it. In that order.

    Same order as `instagram/oauth.py::_persist`, for the same reason: a
    connection row that says "connected" while no credential exists is a hotel
    whose guests reach an adapter that cannot be built.
    """
    property_id = claims["p"]
    partner_id = claims["pa"]
    ref = store.ref_for_property(property_id)

    store.save_token(
        channel=CHANNEL,
        credentials_ref=ref,
        property_id=property_id,
        partner_id=partner_id,
        access_token=token.access_token,
        expires_in=token.expires_in,
        scopes=token.scopes,
        external_account_id=account.waba_id,
    )

    db = get_db()
    row = {
        "property_id": property_id,
        "partner_id": partner_id,
        "phone_number_id": number.phone_number_id,
        "waba_id": account.waba_id,
        "business_id": account.owner_business_id,
        "solution_id": settings.whatsapp_solution_id,
        "display_number": number.display_number,
        "verified_name": number.verified_name,
        "quality_rating": number.quality_rating,
        "platform_type": number.platform_type,
        "credentials_ref": ref,
        "status": "connected",
        # The whole point of the flow. A row left sandboxed here would consume a
        # real consent and still answer guests with the fake.
        "is_sandbox": False,
        "pin_ciphertext": crypto.encrypt(pin),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "onboarding_id": onboarding_id,
        "last_error": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    result = (
        db.table("whatsapp_connections")
        .upsert(row, on_conflict="property_id")
        .execute()
    )

    if onboarding_id:
        (
            db.table("whatsapp_onboardings")
            .update({
                "status": "completed",
                "waba_id": account.waba_id,
                "owner_business_id": account.owner_business_id,
                "phone_number_id": number.phone_number_id,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", onboarding_id)
            .execute()
        )

    return (result.data or [{}])[0]


def _fail_onboarding(nonce: str | None, reason: str) -> None:
    if not nonce:
        return
    try:
        (
            get_db().table("whatsapp_onboardings")
            .update({"status": "failed", "error": reason[:500],
                     "updated_at": datetime.now(timezone.utc).isoformat()})
            .eq("state_nonce", nonce)
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not mark onboarding %s failed", nonce)


@router.post("/properties/{property_id}/whatsapp/finish")
@limiter.limit("20/hour")
async def finish_whatsapp_connect(
    request: Request,
    property_id: str,
    body: FinishIn,
    member=Depends(require_member("agents")),
):
    """
    The code from the popup → a number that can answer guests.

    Six steps, in this order, and the order is not incidental:

        1. verify the state          — is this a `finish` for a real `start`?
        2. exchange the code         — 30-second life, one use
        3. check the WABA            — against debug_token, not the request body
        4. subscribe our app         — before anything else, or the hotel is silent
        5. register the number       — or it cannot send
        6. store, then provision     — credential, row, templates

    Step 4 before step 5 because a number registered on a WABA we are not
    subscribed to is a hotel that appears connected and receives nothing.
    """
    if not onboarding.is_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "WhatsApp connections are not switched on yet.")

    try:
        claims = onboarding.verify_state(body.state)
    except onboarding.OnboardingError as exc:
        logger.warning("whatsapp finish with bad state for %s: %s", property_id, exc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.reason) from exc

    # The state names the hotel; the path names a hotel. They must agree.
    # `require_member` has already proved the caller may act on the path's
    # property, so a mismatch is a state minted for a different hotel.
    if claims.get("p") != property_id:
        logger.warning("whatsapp finish state names %s but path names %s",
                       claims.get("p"), property_id)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "state_property_mismatch")

    nonce = claims.get("n")

    def _find_onboarding() -> dict | None:
        rows = (
            get_db().table("whatsapp_onboardings")
            .select("id,status").eq("state_nonce", nonce).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    started = await run_db(_find_onboarding) if nonce else None
    # A signed state whose row is already `completed` is a double submit — the
    # popup fired twice, or the owner pressed the button again. The code is
    # already spent, so exchanging it would 100; saying so plainly is better
    # than a Meta error the PWA has to translate.
    if started and started.get("status") == "completed":
        raise HTTPException(status.HTTP_409_CONFLICT, "already_connected")
    onboarding_id = str(started["id"]) if started else None

    try:
        token = await onboarding.exchange_code(body.code)

        # **The check that makes the request body safe.**
        if token.waba_ids and body.wabaId not in token.waba_ids:
            logger.warning(
                "whatsapp finish for %s claimed WABA %s; the token may only act on %s",
                property_id, body.wabaId, token.waba_ids,
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "waba_not_granted")

        await onboarding.subscribe_app(body.wabaId, token.access_token)
        account = await onboarding.account(body.wabaId, token.access_token)

        number = next(
            (n for n in account.numbers if n.phone_number_id == body.phoneNumberId),
            None,
        )
        if number is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "number_not_on_account")

        pin = body.pin or onboarding.generate_pin()
        await onboarding.register(number.phone_number_id, pin, token.access_token)

    except HTTPException:
        _fail_onboarding(nonce, "refused")
        raise
    except onboarding.OnboardingError as exc:
        logger.warning("whatsapp onboarding failed for %s: %s", property_id, exc)
        await run_db(_fail_onboarding, nonce, str(exc))
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY if exc.retryable else status.HTTP_400_BAD_REQUEST,
            exc.reason,
        ) from exc

    try:
        # partial, not kwargs: `run_db(fn, *args)` is positional-only.
        await run_db(partial(
            _persist, claims=claims, account=account, number=number,
            token=token, pin=pin, onboarding_id=onboarding_id,
        ))
    except CredentialCryptoError:
        # Retrying cannot help, so the hotel must not be told to. `start`
        # already refuses when the key is missing; reaching here means it went
        # missing mid-flow, which is a deploy, not a hotel's mistake.
        logger.error(
            "WhatsApp connected for property %s but the credential could not be "
            "encrypted — check MESSAGING_CREDENTIALS_KEY on this deployment",
            property_id,
        )
        await run_db(_fail_onboarding, nonce, "credentials key missing")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "save_not_configured") from None
    except Exception as exc:  # noqa: BLE001
        logger.exception("WhatsApp connected but could not be stored for %s", property_id)
        await run_db(_fail_onboarding, nonce, "save failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "save_failed") from exc

    # **After the connection is usable, never before.** A template created
    # against a WABA we then failed to subscribe to is a template on an account
    # we cannot hear from. Failures here do not fail the connection — see
    # `provisioning.provision`.
    templates = await provisioning.provision(
        property_id=property_id,
        partner_id=str(member.partner_id),
        waba_id=account.waba_id,
        token=token.access_token,
    )

    logger.info("whatsapp connected: property=%s waba=%s number=%s templates=%s/%s",
                property_id, account.waba_id, number.display_number,
                len(templates["created"]), templates["total"])

    return {
        "connected": True,
        "displayNumber": number.display_number,
        "verifiedName": number.verified_name,
        "templates": templates,
    }


# ── disconnect ──────────────────────────────────────────────────────────────


def disconnect_connection(connection: dict, property_id: str) -> dict:
    """
    Revoke the credential, then mark the row. In that order.

    **Module level rather than a closure so the order can be tested.** A row
    saying "disconnected" while a live token is still in the store is an account
    we can still send as, which is the direction of this pair that actually
    matters — and an ordering property that is only stated in a comment is one
    that gets reversed by the next person tidying the function.
    """
    revoked = 0
    ref = connection.get("credentials_ref")
    if ref and store.is_db_ref(ref):
        revoked = 1 if store.revoke(channel=CHANNEL, credentials_ref=ref) else 0
    elif connection.get("waba_id"):
        revoked = len(store.revoke_by_account(
            channel=CHANNEL, external_account_id=connection["waba_id"],
        ))

    # `last_error` cleared, not set. This was a deliberate act by the hotel, and
    # an amber "needs reconnecting" on a channel somebody switched off on purpose
    # is the screen telling an owner their setup is broken.
    (
        get_db().table("whatsapp_connections")
        .update({"status": "disconnected", "last_error": None,
                 "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("property_id", property_id)
        .execute()
    )
    return {"disconnected": True, "revoked": revoked}


@router.post("/properties/{property_id}/whatsapp/disconnect")
@limiter.limit("20/hour")
async def disconnect_whatsapp(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """
    Stop answering on this hotel's WhatsApp.

    The credential is revoked first and the row marked second, exactly as
    Instagram does: a row that says "disconnected" while a live token is still
    in the store is an account we can still send as, which is the direction of
    this pair that actually matters.

    The number is *not* deregistered by default. Deregistering hands it back to
    Meta and a hotel that pressed Disconnect by mistake would have to redo phone
    verification to return — a much worse outcome than a dormant registration.
    """
    def _read() -> dict | None:
        rows = (
            get_db().table("whatsapp_connections")
            .select("credentials_ref,phone_number_id,waba_id,status,is_sandbox")
            .eq("property_id", property_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    connection = await run_db(_read)
    if not connection:
        return {"disconnected": False, "revoked": 0}

    result = await run_db(disconnect_connection, connection, property_id)
    logger.info("whatsapp disconnected: property=%s revoked=%s",
                property_id, result.get("revoked"))
    return result


# ── the Meta-hosted path ────────────────────────────────────────────────────


def claim_arrival(body: "ClaimIn", partner_id: str, property_id: str) -> dict:
    """
    Attach one unclaimed arrival to a hotel.

    **Module level rather than a closure so the refusals can be tested.** The
    interesting behaviour here is entirely what it says no to — an arrival that
    belongs to another partner, one already attached, one named only by a uuid
    somebody could walk — and a rule that only exists inside a request handler
    is a rule with no test.
    """
    db = get_db()
    query = (
        db.table("whatsapp_onboardings")
        .select("id,waba_id,owner_business_id,phone_number_id,status,partner_id")
        .eq("status", "unclaimed")
    )
    if body.onboardingId:
        query = query.eq("id", body.onboardingId)
    else:
        # Digits only. A WABA id read off a screen arrives with spaces in it
        # about half the time.
        query = query.eq("waba_id", "".join(c for c in body.wabaId if c.isdigit()))

    rows = query.limit(1).execute().data or []
    if not rows:
        return {"claimed": False, "reason": "not_found"}
    row = rows[0]

    # A row that names a partner belongs to that partner and nobody else.
    # A row that names none is an unattributed Meta-hosted arrival, and the
    # WABA id the caller typed is what stands in for proof — which is why
    # that path is not reachable by `onboardingId`, a value they could
    # otherwise guess by walking uuids.
    owner = str(row.get("partner_id") or "")
    if owner and owner != partner_id:
        return {"claimed": False, "reason": "not_yours"}
    if not owner and not body.wabaId:
        return {"claimed": False, "reason": "not_yours"}

    taken = (
        db.table("whatsapp_connections").select("property_id")
        .eq("waba_id", row["waba_id"]).limit(1).execute().data or []
    )
    if taken and str(taken[0]["property_id"]) != property_id:
        return {"claimed": False, "reason": "waba_taken"}

    db.table("whatsapp_connections").upsert({
        "property_id": property_id,
        "partner_id": partner_id,
        "phone_number_id": row.get("phone_number_id") or f"pending:{row['waba_id']}",
        "waba_id": row["waba_id"],
        "business_id": row.get("owner_business_id"),
        "solution_id": settings.whatsapp_solution_id,
        "credentials_ref": store.ref_for_property(property_id),
        # `pending`, not `connected`. There is no token yet, and a row that
        # claims otherwise is a hotel told it is live while every send
        # raises CHANNEL_NOT_CONFIGURED.
        "status": "pending",
        "is_sandbox": False,
        "onboarding_id": row["id"],
        "last_error": "Almost there — finish connecting from this screen to "
                      "start answering guests.",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, on_conflict="property_id").execute()

    db.table("whatsapp_onboardings").update({
        "status": "completed", "property_id": property_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row["id"]).execute()

    return {"claimed": True, "wabaId": row["waba_id"]}


@router.get("/properties/{property_id}/whatsapp/claimable")
@limiter.limit("60/minute")
async def claimable_accounts(
    request: Request,
    property_id: str,
    member=Depends(require_member("agents")),
):
    """
    WhatsApp accounts that finished the Meta-hosted flow and belong to nobody.

    **Deliberately narrow.** A hotel that completed the Meta-hosted link in
    their own browser reaches us only as a `PARTNER_ADDED` webhook carrying a
    `waba_id` and an `owner_business_id` — nothing that says which hotel. So the
    completion waits here and an owner attaches it.

    Only rows this partner started are listed. An `unclaimed` row with no
    partner — a hotel that used a link we never issued to them — is **not**
    offered: that list is every hotel that has just onboarded, and claiming one
    is claiming their guests. Those are attached by us, by hand, on purpose.
    """
    def _read() -> list[dict]:
        rows = (
            get_db().table("whatsapp_onboardings")
            .select("id,waba_id,owner_business_id,phone_number_id,created_at,source")
            .eq("status", "unclaimed")
            .eq("partner_id", str(member.partner_id))
            .order("created_at", desc=True)
            .limit(20)
            .execute().data or []
        )
        return rows

    rows = await run_db(_read)
    return {
        "accounts": [
            {
                "onboardingId": r["id"],
                "wabaId": r.get("waba_id"),
                "phoneNumberId": r.get("phone_number_id"),
                "arrivedAt": r.get("created_at"),
            }
            for r in rows
        ]
    }


@router.post("/properties/{property_id}/whatsapp/claim")
@limiter.limit("20/hour")
async def claim_account(
    request: Request,
    property_id: str,
    body: ClaimIn,
    member=Depends(require_member("agents")),
):
    """
    Attach an unclaimed Meta-hosted completion to this hotel.

    **This cannot mint a credential**, and that is the honest limitation of the
    Meta-hosted path: there was no code, so there is no customer-scoped business
    token to store. The connection is written `pending` with the WABA recorded,
    and it starts working when the hotel completes the in-app flow once — which
    is a single popup, not a re-onboarding, because Meta already knows the
    account.

    Claiming is refused if the WABA is already attached anywhere. Two hotels on
    one WABA is an ambiguity that must be impossible rather than resolved at
    read time.
    """
    if not body.onboardingId and not body.wabaId:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "nothing_named")

    result = await run_db(claim_arrival, body, str(member.partner_id), property_id)
    if not result.get("claimed"):
        raise HTTPException(status.HTTP_409_CONFLICT, result.get("reason", "could_not_claim"))
    logger.info("whatsapp account claimed: property=%s waba=%s",
                property_id, result.get("wabaId"))
    return result


# ── proving it works ────────────────────────────────────────────────────────

def _mask_contacts(raw: dict | None, recipient: str) -> dict:
    """Meta's reply, with the phone number reduced to its last four digits."""
    if not isinstance(raw, dict):
        return {}
    masked = dict(raw)
    if isinstance(masked.get("contacts"), list):
        tail = f"…{recipient[-4:]}"
        masked["contacts"] = [
            {**c, **{k: tail for k in ("input", "wa_id") if k in c}}
            for c in masked["contacts"]
            if isinstance(c, dict)
        ]
    return masked




@router.post("/properties/{property_id}/whatsapp/test-message")
@limiter.limit("5/hour")
async def send_test_message(
    request: Request,
    property_id: str,
    body: TestMessageIn,
    member=Depends(require_member("agents")),
):
    """
    Send one WhatsApp message from this hotel's own number, and show the call.

    **A hotel that has just connected has no way to know it worked.** The badge
    says "Connected", and the first proof otherwise is a guest who never got an
    answer. Every WhatsApp provider has this button for that reason.

    It also happens to be what Meta's App Review asks to watch: their own
    instruction is to *"embed the cURL command into your website and record how
    you send a message"*. So the response carries the request we sent and Meta's
    reply verbatim, for the screen to render — a reviewer who cannot see the API
    call cannot grant the permission.

    ── why a template, and not "hi there" ───────────────────────────────────

    Meta allows free text only inside the 24-hour window a guest's own message
    opens. A hotel testing its connection has no such window — nobody has
    written to it yet — so free text is refused with `131047` and the test
    looks like a broken integration. An approved template has no such
    restriction, which is exactly why Meta's own "Try it out" tool sends
    `hello_world`.

    ── why the adapter directly, and not `messaging.send` ───────────────────

    This is a diagnostic, not a conversation. Going through `messaging.send`
    would open a conversation row, a guest record and a billable window for
    somebody testing their own setup, and the hotel's inbox would fill with
    threads it never had. Nothing here is metered and nothing is recorded as a
    guest interaction — the point is the wire, not the thread.
    """
    def _read() -> dict | None:
        rows = (
            get_db().table("whatsapp_connections")
            .select("*").eq("property_id", property_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    connection = await run_db(_read)
    if not connection:
        raise HTTPException(status.HTTP_409_CONFLICT, "not_connected")
    if connection.get("status") != "connected":
        raise HTTPException(status.HTTP_409_CONFLICT, "not_connected")

    # Digits only. Meta wants an E.164 number without the plus, and a hotelier
    # will type spaces, dashes and brackets.
    recipient = "".join(c for c in body.to if c.isdigit())
    if len(recipient) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "bad_number")

    template = settings.whatsapp_test_template
    language = settings.whatsapp_test_template_language

    try:
        adapter = await creds_module.adapter_for_async(connection)
        sent = await adapter.send_template(
            to=recipient,
            template_name=template,
            language=language,
            parameters=[],
            client_reference=f"test:{property_id}",
        )
    except MessagingError as exc:
        logger.warning("whatsapp test message failed for %s: %s", property_id, exc.detail)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            {"code": exc.code, "detail": exc.detail},
        ) from exc

    logger.info("whatsapp test message sent: property=%s template=%s id=%s",
                property_id, template, sent.external_id)

    # The request is reconstructed rather than captured, because the adapter
    # builds it. It is the same shape byte for byte — `send_template` above —
    # and it is here so the screen can show what went over the wire.
    return {
        "sent": True,
        "messageId": sent.external_id,
        "sandbox": bool(connection.get("is_sandbox")),
        "request": {
            "method": "POST",
            "url": f"{settings.whatsapp_api_base.rstrip('/')}/"
                   f"{connection['phone_number_id']}/messages",
            "body": {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                # Masked. This response is rendered on a screen that is about to
                # be screen-recorded and sent to Meta.
                "to": f"…{recipient[-4:]}",
                "type": "template",
                "template": {"name": template, "language": {"code": language}},
            },
        },
        # Meta echoes the recipient back in `contacts`. Masked for the same
        # reason `to` is above: this panel is rendered on a screen that is about
        # to be recorded and attached to an App Review submission.
        "response": _mask_contacts(sent.raw, recipient),
    }


# ── templates ───────────────────────────────────────────────────────────────


@router.get("/properties/{property_id}/whatsapp/templates")
@limiter.limit("120/minute")
async def list_templates(
    request: Request,
    property_id: str,
    member=Depends(require_member("messaging")),
):
    """
    What this hotel may say once the 24-hour window has closed.

    `messaging`, not `agents` — a receptionist wondering why a guest never got
    their booking confirmation should be able to see that the template is still
    pending, without being able to change anything.

    Read from our own table rather than from Meta. The status webhook keeps it
    current, and a screen that calls Graph to draw a badge goes blank the first
    time Meta rate limits us.
    """
    def _read() -> list[dict]:
        return (
            get_db().table("message_templates")
            .select("purpose,language,status,category,body_preview,rejected_reason")
            .eq("property_id", property_id)
            .eq("channel", CHANNEL)
            .order("purpose")
            .execute().data or []
        )

    rows = await run_db(_read)
    return {
        "templates": [
            TemplateOut(
                purpose=r["purpose"],
                language=r["language"],
                status=r["status"],
                category=r.get("category"),
                preview=r.get("body_preview"),
                rejectedReason=r.get("rejected_reason"),
            )
            for r in rows
        ]
    }
