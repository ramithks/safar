"""
Razorpay REST client — the money path.

Ported from the website's lib/marketplace/razorpay.ts as part of moving
privileged writes off the web tier. Deliberately plain httpx + hmac rather than
the SDK: we need three calls (create order, refund, fetch payment) and two HMAC
checks, and raw-body webhook verification is easier to get right without a
wrapper in the way.

The secret never leaves this module. Only `razorpay_key_id` is ever sent to a
browser, and only so Checkout can identify the merchant.
"""
import hashlib
import hmac
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

API = "https://api.razorpay.com/v1"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def configured() -> bool:
    """True when checkout is usable. Callers 503 cleanly instead of throwing."""
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def webhook_configured() -> bool:
    return bool(settings.razorpay_webhook_secret)


def public_key_id() -> str | None:
    return settings.razorpay_key_id


def _auth() -> tuple[str, str]:
    return (settings.razorpay_key_id or "", settings.razorpay_key_secret or "")


async def create_order(
    *,
    amount_paise: int,
    receipt: str,
    notes: dict | None = None,
) -> dict:
    """
    Create an order. `amount_paise` is always computed server-side from a DB
    row — a client-supplied amount is never passed through.
    """
    if not configured():
        raise RuntimeError("Razorpay is not configured.")
    if not isinstance(amount_paise, int) or amount_paise < 100:
        raise ValueError("Invalid amount.")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API}/orders",
            auth=_auth(),
            json={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt[:40],
                "notes": notes or {},
            },
        )

    if res.status_code >= 400:
        logger.error("[razorpay] create_order failed: %s %s", res.status_code, res.text[:400])
        raise RuntimeError("Could not start the payment.")
    return res.json()


def _timing_safe_equal(expected: str, given: str) -> bool:
    # compare_digest is length-safe, unlike Node's timingSafeEqual which throws
    # on a length mismatch — hence no explicit length check here.
    return hmac.compare_digest(expected, given)


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Verify a Checkout callback: HMAC-SHA256 of `order_id|payment_id` keyed with
    the secret must equal the signature Razorpay handed the browser.
    """
    secret = settings.razorpay_key_secret
    if not secret or not order_id or not payment_id or not signature:
        return False

    expected = hmac.new(
        secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
    ).hexdigest()
    return _timing_safe_equal(expected, signature)


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Verify a webhook against the RAW request body — never re-serialised JSON."""
    secret = settings.razorpay_webhook_secret
    if not secret or not signature:
        return False

    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return _timing_safe_equal(expected, signature)


async def refund_payment(
    payment_id: str,
    amount_paise: int | None = None,
    notes: dict | None = None,
) -> bool:
    """
    Refund a captured payment. Used when we take money and then can't honour it
    — the classic case being a seat that sold out between checkout and capture.
    """
    if not configured():
        return False
    body: dict = {"speed": "normal", "notes": notes or {}}
    if amount_paise:
        body["amount"] = amount_paise

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.post(
                f"{API}/payments/{payment_id}/refund", auth=_auth(), json=body
            )
        if res.status_code >= 400:
            logger.error("[razorpay] refund failed: %s %s", res.status_code, res.text[:300])
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("[razorpay] refund threw")
        return False


# ── subscriptions (Autopay) ──────────────────────────────────────────────────
#
# The ₹499 plan is a mandate, not a monthly invoice a partner has to remember.
# Razorpay's model is Plan (what we sell) → Subscription (one partner's mandate)
# → Invoice per cycle, and the charge fires server-side from then on.


async def create_plan(*, amount_paise: int, name: str, period: str = "monthly") -> dict:
    """
    Create a billing plan. Called once, ever — the id is stored in
    `platform_plans` so subsequent subscriptions reuse it. Creating a second
    identical plan is harmless to Razorpay but would fragment our reporting.
    """
    if not configured():
        raise RuntimeError("Razorpay is not configured.")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(
            f"{API}/plans",
            auth=_auth(),
            json={
                "period": period,
                "interval": 1,
                "item": {"name": name, "amount": amount_paise, "currency": "INR"},
            },
        )

    if res.status_code >= 400:
        logger.error("[razorpay] create_plan failed: %s %s", res.status_code, res.text[:400])
        raise RuntimeError("Could not create the billing plan.")
    return res.json()


async def create_subscription(
    *,
    plan_id: str,
    total_count: int = 120,
    notes: dict | None = None,
    notify: bool = True,
    start_at: int | None = None,
    offer_id: str | None = None,
) -> dict:
    """
    Open a mandate against a plan.

    `total_count` is the number of cycles the mandate is authorised for, and
    Razorpay requires one — there is no "forever". 120 months is ten years,
    which is functionally forever for a monthly plan and still gives the
    customer a bounded authorisation to see when they approve it.

    `customer_notify` lets Razorpay send its own pre-debit notification, which
    is mandatory for UPI Autopay anyway — better their compliant template than
    our reimplementation of it.

    `start_at` (unix seconds) is how a free period is expressed: the mandate is
    authorised now and the FIRST charge is deferred to that moment. That is the
    whole mechanic behind a free_months plan coupon — we grant the plan time
    ourselves and Razorpay starts billing when it runs out.

    `offer_id` applies a Dashboard-created Razorpay Offer to the charges, which
    is how percent/flat plan coupons discount real money for a set number of
    cycles. Offers have no create API, hence the id rather than a spec.
    """
    if not configured():
        raise RuntimeError("Razorpay is not configured.")

    payload: dict = {
        "plan_id": plan_id,
        "total_count": total_count,
        "customer_notify": 1 if notify else 0,
        "notes": notes or {},
    }
    if start_at:
        payload["start_at"] = start_at
    if offer_id:
        payload["offer_id"] = offer_id

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        res = await client.post(f"{API}/subscriptions", auth=_auth(), json=payload)

    if res.status_code >= 400:
        logger.error(
            "[razorpay] create_subscription failed: %s %s", res.status_code, res.text[:400]
        )
        raise RuntimeError("Could not start the subscription.")
    return res.json()


async def fetch_subscription(subscription_id: str) -> dict | None:
    """The live state of a mandate. Used to reconcile when a webhook is missed."""
    if not configured():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            res = await client.get(f"{API}/subscriptions/{subscription_id}", auth=_auth())
        if res.status_code >= 400:
            logger.error("[razorpay] fetch_subscription: %s %s", res.status_code, res.text[:300])
            return None
        return res.json()
    except Exception:  # noqa: BLE001
        logger.exception("[razorpay] fetch_subscription threw")
        return None


async def cancel_subscription(subscription_id: str, *, at_cycle_end: bool = True) -> dict | None:
    """
    Cancel a mandate.

    `at_cycle_end` by default: the partner has already paid for the month they
    are in, and ending it immediately would take away days they bought. It also
    keeps the cancellation reversible right up to the last day.

    Razorpay rejects a cycle-end cancel when no billing cycle is running — the
    case where a partner opened Checkout, never authorised the mandate, and then
    hit Cancel. There is nothing to preserve there, so we fall back to an
    immediate cancel rather than telling them it failed.
    """
    if not configured():
        return None

    async def _post(cycle_end: bool):
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return await client.post(
                f"{API}/subscriptions/{subscription_id}/cancel",
                auth=_auth(),
                json={"cancel_at_cycle_end": 1 if cycle_end else 0},
            )

    try:
        res = await _post(at_cycle_end)
        if res.status_code >= 400 and at_cycle_end and "billing cycle" in res.text:
            logger.info(
                "[razorpay] %s has no live cycle — cancelling immediately", subscription_id
            )
            res = await _post(False)

        if res.status_code >= 400:
            logger.error("[razorpay] cancel_subscription: %s %s", res.status_code, res.text[:300])
            return None
        return res.json()
    except Exception:  # noqa: BLE001
        logger.exception("[razorpay] cancel_subscription threw")
        return None


def verify_subscription_signature(
    subscription_id: str, payment_id: str, signature: str
) -> bool:
    """
    Verify the Checkout callback for a subscription.

    ⚠ The signed string is `payment_id|subscription_id` — the REVERSE order of
    the order flow's `order_id|payment_id`. Getting this backwards fails every
    genuine subscription silently, which is exactly the sort of bug that looks
    like "Razorpay is broken".
    """
    secret = settings.razorpay_key_secret
    if not secret or not subscription_id or not payment_id or not signature:
        return False

    expected = hmac.new(
        secret.encode(), f"{payment_id}|{subscription_id}".encode(), hashlib.sha256
    ).hexdigest()
    return _timing_safe_equal(expected, signature)
