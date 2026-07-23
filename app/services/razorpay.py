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
