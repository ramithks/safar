"""
Embedded Signup — the flow that lets a hotel connect its *own* WhatsApp number.

Until this existed there was one WABA, ours, and "connecting WhatsApp" meant
putting a token in the environment by hand. That works for a demo and cannot
work for the second hotel: Meta bills the WABA that owns the number, the
24-hour window belongs to that number, and template approval is granted to that
WABA. One number shared across hotels means one hotel's template rejection
silences another hotel's booking confirmations.

    PWA  ──► POST .../whatsapp/start    → signed state + what the popup needs
    owner ──► Meta's Embedded Signup popup (or the Meta-hosted landing page)
    PWA  ──► POST .../whatsapp/finish   → { state, code, wabaId, phoneNumberId }
                    │
                    ├─ code       → business token   (scoped to this customer)
                    ├─ debug_token→ which WABAs that token may act on
                    ├─ subscribe  → our app onto the hotel's WABA
                    ├─ numbers    → display number, verified name, quality
                    └─ register   → the number is usable on Cloud API

**Every function here takes strings and returns dataclasses.** Nothing touches
the database or a request object, so the route reads as a sequence of steps and
each step is testable without a webserver — the same shape as
`instagram/oauth.py`, deliberately.

── the token is the whole point ─────────────────────────────────────────────

A Tech Provider uses **business tokens**: one per onboarded customer, obtained
by exchanging the code Embedded Signup returns. That token is what reaches
`messaging_credentials` under `credentials_ref = 'prop:<property_id>'`, and it
is why `credentials.py` needed no change — it has always preferred a database
token over an environment one when the ref says so.

The platform's own system-user token is **not** a substitute. It can act on our
WABA and no hotel's, which is exactly the `131005` in `adapter.py`'s error map:
*"the token is valid but is not authorised to act on the WABA that owns this
number"*.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# Meta's own name for the permission whose `target_ids` name the WABAs a
# business token may act on. Read from debug_token rather than trusted from the
# popup: the `wabaId` the client posts is a string the client chose.
WABA_SCOPE = "whatsapp_business_management"


class OnboardingError(RuntimeError):
    """Anything that went wrong between the popup closing and a usable number."""

    def __init__(self, reason: str, message: str, *, retryable: bool = False):
        # `reason` is a short slug the PWA turns into a sentence; `message` is
        # for the log and for us.
        self.reason = reason
        self.retryable = retryable
        super().__init__(message)


# What a hotelier should be told, per Meta error code. Same principle as
# `adapter.py::_META_ERRORS`: a bare code sends whoever is on call to a search
# engine, and only codes with an unambiguous action are listed.
_META_ERRORS: dict[int, tuple[str, str]] = {
    100: ("bad_request",
          "Meta rejected the request as malformed. Usually the code was already "
          "used — it is valid for 30 seconds and exactly one exchange."),
    190: ("token_invalid",
          "The code did not become a usable token. The hotel should try "
          "connecting again."),
    200: ("missing_permission",
          "Our app does not hold advanced access to whatsapp_business_management "
          "yet. This is App Review, not the hotel's setup."),
    131009: ("bad_number",
             "Meta will not accept this phone number for the Cloud API."),
    133005: ("pin_required",
             "This number already has a WhatsApp two-step PIN that we did not "
             "set. The hotel has to give us that PIN, or clear it in the "
             "WhatsApp app under Settings → Account → Two-step verification."),
    133006: ("not_verified",
             "The number has not completed Meta's verification. Finish it in "
             "WhatsApp Manager, then connect again."),
    133010: ("not_registered", "The number is not registered for the Cloud API."),
    133015: ("rate_limited",
             "Meta is rate limiting registration for this number. Wait a few "
             "minutes and try again."),
}


@dataclass(frozen=True)
class BusinessToken:
    access_token: str
    expires_in: int | None
    # The WABAs this token may actually act on, straight from debug_token. Not
    # what the client claimed.
    waba_ids: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhoneNumber:
    phone_number_id: str
    display_number: str | None
    verified_name: str | None
    quality_rating: str | None
    platform_type: str | None
    code_verification_status: str | None


@dataclass(frozen=True)
class Account:
    """The hotel's WABA, as Meta describes it."""

    waba_id: str
    name: str | None = None
    currency: str | None = None
    timezone_id: str | None = None
    review_status: str | None = None
    owner_business_id: str | None = None
    numbers: tuple[PhoneNumber, ...] = field(default_factory=tuple)


# ── configuration ───────────────────────────────────────────────────────────


def app_id() -> str | None:
    return settings.whatsapp_app_id


def app_secret() -> str | None:
    return settings.whatsapp_app_secret


def is_configured() -> bool:
    """
    Whether a hotel can be onboarded at all on this deployment.

    The configuration id is required and not optional: Embedded Signup without
    one falls back to a generic Facebook Login dialogue that asks a hotelier for
    permissions we do not need and does not create a WABA.
    """
    return bool(
        settings.whatsapp_onboarding_enabled
        and app_id()
        and app_secret()
        and settings.whatsapp_config_id
    )


def meta_hosted_url(nonce: str | None = None) -> str | None:
    """
    The Meta-hosted landing page, when one is configured.

    **The nonce is appended hopefully, not authoritatively.** Meta hosts that
    page and we do not control what it does with a query string; if it survives
    to the `PARTNER_ADDED` webhook the onboarding is attributed automatically,
    and if it does not the completion lands as `unclaimed` and an owner claims
    it in the PWA. Nothing depends on it arriving — see
    `whatsapp_onboardings`.
    """
    base = (settings.whatsapp_meta_hosted_url or "").strip()
    if not base:
        return None
    if not nonce:
        return base
    join = "&" if "?" in base else "?"
    return f"{base}{join}{urlencode({'state': nonce})}"


# ── state ───────────────────────────────────────────────────────────────────
#
# Signed rather than kept in Redis, matching Instagram: a stateless state
# survives a deploy mid-signup and leaves nothing to clean up when an owner
# abandons the popup. What differs is that it is *also* written to
# `whatsapp_onboardings` — not for verification, which the signature does, but
# so a signup that never came back has a record saying so.


def _state_key() -> bytes:
    secret = app_secret() or ""
    if not secret:
        raise OnboardingError("not_configured",
                              "no WhatsApp app secret to sign state with")
    return secret.encode()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def issue_state(*, property_id: str, partner_id: str, profile_id: str) -> tuple[str, str]:
    """Returns (signed state, nonce). The nonce is what the database stores."""
    nonce = secrets.token_urlsafe(24)
    payload = json.dumps(
        {"p": property_id, "pa": partner_id, "u": profile_id,
         "n": nonce, "t": int(time.time())},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    body = _b64e(payload)
    sig = _b64e(hmac.new(_state_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", nonce


def verify_state(state: str | None) -> dict:
    if not state or "." not in state:
        raise OnboardingError("bad_state", "state missing or malformed")

    body, _, sig = state.partition(".")
    expected = _b64e(hmac.new(_state_key(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise OnboardingError("bad_state", "state signature did not verify")

    try:
        claims = json.loads(_b64d(body))
    except Exception as exc:  # noqa: BLE001
        raise OnboardingError("bad_state", f"state payload unreadable: {exc}") from exc

    age = int(time.time()) - int(claims.get("t") or 0)
    if age > settings.oauth_state_ttl_seconds:
        raise OnboardingError(
            "state_expired",
            f"state is {age}s old, limit {settings.oauth_state_ttl_seconds}s",
        )
    if age < -60:
        raise OnboardingError("bad_state", "state is issued in the future")
    return claims


# ── the wire ────────────────────────────────────────────────────────────────


def _base() -> str:
    return settings.whatsapp_api_base.rstrip("/")


def _raise_for(resp: httpx.Response, step: str) -> dict:
    """One place where a Meta error becomes something a hotelier can act on."""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        payload = {}

    if resp.status_code < 400:
        return payload

    error = payload.get("error") or {}
    code = error.get("code")
    detail = str(error.get("message") or resp.text)[:300]
    subcode = (error.get("error_data") or {}).get("details")

    reason, advice = "provider_failed", ""
    try:
        mapped = _META_ERRORS.get(int(code)) if code is not None else None
    except (TypeError, ValueError):
        mapped = None
    if mapped:
        reason, advice = mapped

    raise OnboardingError(
        reason,
        f"{step} failed: Meta returned {resp.status_code} ({code}): "
        f"{subcode or detail}{(' — ' + advice) if advice else ''}",
        # 5xx and 429 are worth another attempt; a 400 means the request itself
        # is wrong and retrying produces the same 400 forever.
        retryable=resp.status_code >= 500 or resp.status_code == 429,
    )


async def _get(client: httpx.AsyncClient, path: str, token: str, step: str,
               **params) -> dict:
    resp = await client.get(
        f"{_base()}/{path}", params=params,
        headers={"Authorization": f"Bearer {token}"},
    )
    return _raise_for(resp, step)


async def _post(client: httpx.AsyncClient, path: str, token: str, step: str,
                data: dict | None = None) -> dict:
    resp = await client.post(
        f"{_base()}/{path}", data=data or {},
        headers={"Authorization": f"Bearer {token}"},
    )
    return _raise_for(resp, step)


# ── the steps ───────────────────────────────────────────────────────────────


async def exchange_code(code: str) -> BusinessToken:
    """
    The code from the popup → a business token scoped to this hotel.

    **No `redirect_uri`.** Embedded Signup is not a redirect-based OAuth flow —
    the code is delivered to the opening window by `postMessage`, there is no
    callback URL, and sending one makes Meta compare it against a registered
    value that does not exist. This is the single most common way this call is
    written wrong, and the resulting error names the URI rather than the flow.

    The code lives for **30 seconds** and exactly one exchange. A second attempt
    with the same code is a `100`, which is why `finish` must not be retried
    blindly — it is idempotent on the *connection*, not on the code.
    """
    if not app_id() or not app_secret():
        raise OnboardingError(
            "not_configured",
            "WHATSAPP_APP_ID / WHATSAPP_APP_SECRET are not set on this deployment",
        )

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        payload = _raise_for(
            await client.get(
                f"{_base()}/oauth/access_token",
                params={
                    "client_id": app_id(),
                    "client_secret": app_secret(),
                    "code": code,
                },
            ),
            "code exchange",
        )

        token = payload.get("access_token")
        if not token:
            raise OnboardingError("token_invalid",
                                  "Meta returned no access_token for the code")

        # Ask Meta what the token may do rather than believing the client.
        #
        # `wabaId` arrives in the popup's payload and is therefore a string the
        # browser chose. Acting on it without this check would let anyone who
        # can call `finish` attach *someone else's* WABA to their own hotel —
        # the same class of hole as taking a property_id from a webhook body.
        debug = _raise_for(
            await client.get(
                f"{_base()}/debug_token",
                params={"input_token": token, "access_token": f"{app_id()}|{app_secret()}"},
            ),
            "token inspection",
        )

    data = debug.get("data") or {}
    granular = data.get("granular_scopes") or []
    waba_ids: tuple[str, ...] = ()
    for entry in granular:
        if entry.get("scope") == WABA_SCOPE:
            waba_ids = tuple(str(t) for t in (entry.get("target_ids") or []))
            break

    expires_at = data.get("expires_at") or 0
    expires_in = max(0, int(expires_at) - int(time.time())) if expires_at else None

    return BusinessToken(
        access_token=token,
        expires_in=expires_in,
        waba_ids=waba_ids,
        scopes=tuple(data.get("scopes") or []),
    )


async def subscribe_app(waba_id: str, token: str) -> bool:
    """
    Put our app on the hotel's WABA so its messages reach our webhook.

    Without this a hotel completes signup, sees "connected", and never receives
    a guest message — the connection is real and silent, which is the worst of
    the available failures. Already-subscribed returns success, so this is safe
    to run again on a reconnect.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        payload = await _post(client, f"{waba_id}/subscribed_apps", token,
                              "subscribing to the hotel's WhatsApp account")
    return bool(payload.get("success", True))


async def account(waba_id: str, token: str) -> Account:
    """The WABA and its numbers, in one call each."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        waba = await _get(
            client, waba_id, token, "reading the WhatsApp account",
            fields="id,name,currency,timezone_id,account_review_status,owner_business_info",
        )
        numbers = await _get(
            client, f"{waba_id}/phone_numbers", token, "reading the phone numbers",
            fields=("id,display_phone_number,verified_name,quality_rating,"
                    "platform_type,code_verification_status"),
        )

    owner = (waba.get("owner_business_info") or {}).get("id")
    return Account(
        waba_id=str(waba.get("id") or waba_id),
        name=waba.get("name"),
        currency=waba.get("currency"),
        timezone_id=waba.get("timezone_id"),
        review_status=waba.get("account_review_status"),
        owner_business_id=str(owner) if owner else None,
        numbers=tuple(
            PhoneNumber(
                phone_number_id=str(n.get("id")),
                display_number=n.get("display_phone_number"),
                verified_name=n.get("verified_name"),
                quality_rating=n.get("quality_rating"),
                platform_type=n.get("platform_type"),
                code_verification_status=n.get("code_verification_status"),
            )
            for n in (numbers.get("data") or [])
            if n.get("id")
        ),
    )


def generate_pin() -> str:
    """
    A six-digit two-step PIN, from a real CSPRNG.

    Random rather than derived from the property id: a PIN anyone could compute
    from a public identifier is not a second factor. It is stored encrypted
    because re-registering the same number requires presenting the same PIN, and
    one we generated and discarded can only be cleared by the hotel going into
    the WhatsApp app itself.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


async def register(phone_number_id: str, pin: str, token: str) -> bool:
    """
    Turn the number on for the Cloud API.

    Already-registered is success, not an error: a hotel that reconnects has a
    number Meta already knows about, and treating that as a failure would leave
    a working number marked broken.

    A `133005` is different and is surfaced as such. It means the number carries
    a two-step PIN that we did not set — the hotel set one themselves, or it was
    registered by a previous provider — and no PIN we generate will ever match.
    The only ways out are the hotel telling us theirs or clearing it in the app,
    and the error map says exactly that.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            payload = await _post(
                client, f"{phone_number_id}/register", token,
                "registering the number",
                data={"messaging_product": "whatsapp", "pin": pin},
            )
        except OnboardingError as exc:
            if exc.reason == "not_registered":
                raise
            # Meta reports an already-registered number as a plain 400 whose
            # message says so; there is no distinct code for it.
            if "already" in str(exc).lower():
                logger.info("phone %s was already registered", phone_number_id)
                return True
            raise
    return bool(payload.get("success", True))


async def deregister(phone_number_id: str, token: str) -> bool:
    """
    Hand the number back.

    Called on disconnect so a hotel that leaves can take its number to another
    provider without a support ticket. A failure here is logged and swallowed by
    the caller: the disconnect itself must still complete, because the thing
    that actually matters is that our credential stops working.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        payload = await _post(client, f"{phone_number_id}/deregister", token,
                              "releasing the number")
    return bool(payload.get("success", True))
