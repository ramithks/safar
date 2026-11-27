"""
Discount codes against the ₹999 StaySuite plan.

The marketplace has a table of the same name against its own ₹499 partner plan,
in a different Supabase project. This is deliberately the same shape: the
reasoning in `services/plan_coupons.py` about *when* a coupon is redeemed was
learned expensively and copying it is cheaper than relearning it.

**Two kinds, and the difference is who performs the discount.**

    free_days   We own it end to end. The mandate is created with `start_at`
                past the free period, so Razorpay's first charge lands the day
                the free time runs out. Nothing to configure anywhere, and the
                hotel is never charged an amount we then have to refund.

    percent     Razorpay's own Offers discount the real charge. **Offers have
                no create API** — they are built in the Razorpay Dashboard — so
                the coupon row carries an offer id and we pass it to
                subscription create. A percent coupon without one cannot
                discount anything, and the table refuses to store it: a code
                that says 50% off and charges ₹999 is worse than no code.

**Validation happens at checkout; redemption happens at authentication.**
That distinction is the security property. Granting at checkout would mean
anyone who opens Razorpay and closes the tab has spent a redemption — and on a
capped code, has taken it from somebody who would have paid. So `resolve()` only
ever reads, and `redeem()` is called from the webhook once Razorpay says the
mandate is real.

Every rejection says the same sentence. "No such code", "expired", "you have
already used it" and "all gone" are four different facts about our marketing,
and a code prober who can tell them apart can map the campaign.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# Uppercase alphanumeric, 3–20. Not cosmetic: the code goes into a `like`-free
# equality lookup on `upper(code)`, and anything outside this set is refused
# before it reaches the database rather than escaped on the way in.
_CODE_RE = re.compile(r"^[A-Z0-9]{3,20}$")

# What a hotel owner is told when a code does not work, whatever the reason.
GENERIC_REJECTION = "That code isn't valid. Check it and try again."


class CouponError(Exception):
    """A refusal already written for a hotel owner to read."""

    def __init__(self, message: str = GENERIC_REJECTION):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Discount:
    """What a coupon is worth against one plan price, resolved server-side."""

    coupon_id: str
    code: str
    kind: str
    # What the hotel pays for the first cycle, in paise. Never negative and
    # never above the list price — both are asserted, not assumed.
    first_payment_paise: int
    # What comes off, in paise. Zero for free_days, where the discount is time.
    discount_paise: int
    # What every later cycle costs. The launch coupons discount the first
    # payment only, so this is the list price — and the checkout screen says so
    # rather than letting an owner discover it on the second invoice.
    renewal_paise: int
    free_days: int = 0
    razorpay_offer_id: str | None = None
    description: str = ""

    @property
    def label(self) -> str:
        if self.kind == "free_days":
            return f"{self.free_days} days free"
        return f"{round(self.discount_paise * 100 / max(self.renewal_paise, 1))}% off"


def normalise_code(code: str | None) -> str:
    """
    Upper-cased and stripped, or a refusal.

    Owners paste codes out of emails with a trailing space and in the case they
    were written in. Both are the same code.
    """
    cleaned = (code or "").strip().upper().replace(" ", "")
    if not _CODE_RE.match(cleaned):
        raise CouponError()
    return cleaned


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load(code: str) -> dict | None:
    rows = (
        get_db().table("plan_coupons").select("*")
        .eq("code", code).limit(1).execute().data or []
    )
    if rows:
        return rows[0]
    # `code` is stored as typed but matched case-insensitively by the unique
    # index on upper(code). A row created as 'launch50' must still resolve.
    rows = (
        get_db().table("plan_coupons").select("*")
        .ilike("code", code).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _redemptions(coupon_id: str, partner_id: str) -> int:
    return len(
        get_db().table("plan_coupon_redemptions").select("id")
        .eq("coupon_id", coupon_id).eq("partner_id", partner_id)
        .execute().data or []
    )


def _check(coupon: dict, partner_id: str) -> None:
    """
    Every reason a live-looking coupon may not be used, in one place.

    Order does not matter to the caller — they all raise the same sentence —
    but it matters to the log line, which is how an operator answers "why did
    my code not work for this hotel".
    """
    now = datetime.now(timezone.utc)

    if not coupon.get("active"):
        raise CouponError()

    starts = _parse(coupon.get("starts_at"))
    if starts and starts > now:
        raise CouponError()

    expires = _parse(coupon.get("expires_at"))
    if expires and expires <= now:
        raise CouponError()

    eligible = coupon.get("eligible_partners") or []
    if eligible and str(partner_id) not in [str(p) for p in eligible]:
        raise CouponError()

    cap = coupon.get("max_redemptions")
    if cap is not None and int(coupon.get("redeemed_count") or 0) >= int(cap):
        raise CouponError()

    per_partner = int(coupon.get("max_per_partner") or 1)
    if _redemptions(coupon["id"], partner_id) >= per_partner:
        raise CouponError()


def quote(coupon: dict, price_paise: int) -> Discount:
    """
    What this coupon is worth against this price. Pure, and the only arithmetic.

    The clamp is not defensive decoration. `percent_off` is bounded 1–90 by the
    table, but this function is also what an admin preview calls against a row
    that may have been edited a moment ago, and a first payment of zero is a
    mandate Razorpay will not authorise — a hotel would reach Checkout and be
    told, by Razorpay, that something is wrong with the hotel.
    """
    if coupon["kind"] == "free_days":
        return Discount(
            coupon_id=coupon["id"], code=coupon["code"], kind="free_days",
            first_payment_paise=price_paise, discount_paise=0,
            renewal_paise=price_paise, free_days=int(coupon["free_days"] or 0),
            description=coupon.get("description") or "",
        )

    percent = int(coupon.get("percent_off") or 0)
    discount = (price_paise * percent) // 100
    # Never below ₹1 and never a negative discount, whatever the row says.
    discount = max(0, min(discount, price_paise - 100))
    return Discount(
        coupon_id=coupon["id"], code=coupon["code"], kind="percent",
        first_payment_paise=price_paise - discount,
        discount_paise=discount,
        renewal_paise=price_paise,
        razorpay_offer_id=coupon.get("razorpay_offer_id"),
        description=coupon.get("description") or "",
    )


async def resolve(code: str, *, partner_id: str, price_paise: int) -> Discount:
    """
    Validate a code for one hotel and price it. **Reads only.**

    Nothing here records anything. A hotel that types a code and closes the tab
    has used nothing; see `redeem`.
    """
    normalised = normalise_code(code)
    coupon = await run_db(_load, normalised)
    if not coupon:
        raise CouponError()
    await run_db(_check, coupon, partner_id)
    return quote(coupon, price_paise)


def _redeem(coupon_id: str, partner_id: str, property_id: str | None,
            subscription_id: str | None, razorpay_subscription_id: str | None,
            value_paise: int) -> bool:
    db = get_db()
    try:
        db.table("plan_coupon_redemptions").insert({
            "coupon_id": coupon_id,
            "partner_id": partner_id,
            "property_id": property_id,
            "subscription_id": subscription_id,
            "razorpay_subscription_id": razorpay_subscription_id,
            "value_paise": value_paise,
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        # The unique index on `razorpay_subscription_id` is what makes a
        # redelivered `subscription.authenticated` a no-op. Catching it here
        # rather than checking first is deliberate: a check-then-insert has a
        # window between the two, and Razorpay's retries are exactly the
        # traffic that finds it.
        logger.info("coupon %s already redeemed for mandate %s (%s)",
                    coupon_id, razorpay_subscription_id, type(exc).__name__)
        return False


async def redeem(
    *,
    coupon_id: str,
    partner_id: str,
    property_id: str | None,
    subscription_id: str | None,
    razorpay_subscription_id: str | None,
    value_paise: int,
) -> bool:
    """
    Record that this mandate used this coupon. Returns False if it already had.

    Called from the webhook on `subscription.authenticated`, which is the first
    moment Razorpay has told us the mandate is real. The unique index does the
    work; this returns a boolean so the caller can log the difference between
    "redeemed" and "redeemed again", which is the difference between a bug and
    a retry.
    """
    return await run_db(
        _redeem, coupon_id, partner_id, property_id, subscription_id,
        razorpay_subscription_id, value_paise,
    )


def start_at_for(discount: Discount | None) -> int | None:
    """
    Unix seconds for Razorpay's `start_at`, or None for "charge now".

    Only `free_days` defers anything. A percent coupon charges immediately at
    the discounted amount, which is what the offer is for.
    """
    if not discount or discount.kind != "free_days" or discount.free_days <= 0:
        return None
    return int((datetime.now(timezone.utc)
                + timedelta(days=discount.free_days)).timestamp())
