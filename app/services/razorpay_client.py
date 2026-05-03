"""
Razorpay client wrapper.

We use **manual capture** (`payment_capture=0`) so funds are held until the
host accepts a paid plan. On accept we capture; on reject we refund.

Webhook signature verification uses `RAZORPAY_WEBHOOK_SECRET`.
"""
from typing import Optional
import razorpay

from app.config import settings

_client: Optional[razorpay.Client] = None


def get_client() -> razorpay.Client:
    global _client
    if _client is None:
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            raise RuntimeError("Razorpay not configured (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET)")
        _client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    return _client


def create_order(
    amount_paise: int,
    currency: str = "INR",
    receipt: Optional[str] = None,
    notes: Optional[dict] = None,
) -> dict:
    """Create a Razorpay order with manual capture. Returns the order dict from Razorpay."""
    return get_client().order.create({
        "amount": amount_paise,
        "currency": currency,
        "payment_capture": 0,  # manual: funds held, not auto-captured
        "receipt": receipt or "",
        "notes": notes or {},
    })


def verify_payment_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """True if the (order, payment, signature) tuple is valid."""
    try:
        get_client().utility.verify_payment_signature({
            "razorpay_order_id": order_id,
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        })
        return True
    except Exception:
        return False


def capture_payment(payment_id: str, amount_paise: int, currency: str = "INR") -> dict:
    """Capture an authorized payment. Idempotent on Razorpay's side."""
    return get_client().payment.capture(payment_id, amount_paise, {"currency": currency})


def refund_payment(payment_id: str, amount_paise: Optional[int] = None) -> dict:
    """
    Refund a captured or authorized payment.
    `speed=optimum` uses instant rails when available, falls back to normal (5-7 days).
    """
    payload: dict = {"speed": "optimum"}
    if amount_paise is not None:
        payload["amount"] = amount_paise
    return get_client().payment.refund(payment_id, payload)


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Verify the X-Razorpay-Signature header against the raw request body."""
    if not settings.razorpay_webhook_secret:
        return False
    try:
        get_client().utility.verify_webhook_signature(
            body.decode("utf-8"), signature, settings.razorpay_webhook_secret
        )
        return True
    except Exception:
        return False
