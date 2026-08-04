"""
Plan coupons — the discount codes WE issue against the ₹499 partner plan.

The mirror image of `listing_coupons`, which a partner issues to discount a
traveller's booking. These discount what the partner pays us, so they are the
marketing lever: hand out FOUNDER6, the partner types it at subscribe, and the
first six months are free.

Four shapes, because Razorpay bills them differently:

  free_months   We own the whole mechanic: the plan is granted for N months and
                the mandate is created with `start_at` set past the free period,
                so Razorpay's first charge lands the day the free time runs out.
                No Razorpay configuration involved.

  percent/flat  Razorpay's own Offers discount the real charges for a set number
                of cycles. Offers cannot be created over the API, so the coupon
                row carries the `razorpay_offer_id` of an offer built in the
                Dashboard, and we pass it to subscription create.

  deal          Not a discount at all — a different deal. The code carries its
                own price, billing period and commission rate, and the partner
                is put on a Razorpay plan built for those terms rather than on
                the standard one with money taken off. This is what "₹299/month
                + 5%" is: there is no ₹499 being discounted anywhere.

                It is also the shape to reach for by default, because it needs
                nothing from the Razorpay Dashboard — we mint the plan on first
                use. percent/flat exist for discounting the standard plan; a
                bespoke price is cleaner as its own plan.

WHEN the discount lands is the security-relevant part. Validation happens at
subscribe, but redemption happens only once the mandate is AUTHENTICATED — see
`apply_pending_coupon`. Granting at subscribe time would mean anyone who opens
Checkout and closes it walks away with free months.
"""
# NOTE: deliberately no `from __future__ import annotations` — see the note in
# app/api/v1/admin/endpoint.py.
import logging
import re
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services import partner_terms
from app.services.partner_terms import DEFAULT_COMMISSION_PCT as DEFAULT_AFTER_INTRO

logger = logging.getLogger(__name__)


def _parse(value):
    """A timestamptz from PostgREST as an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

# Same shape as the booking coupons: ^[A-Z0-9]{3,20}$, stored upper-cased. The
# regex is not cosmetic — see normalise_code.
_CODE_RE = re.compile(r"^[A-Z0-9]{3,20}$")

# What a partner is told when a code doesn't work. Deliberately uniform across
# "no such code", "expired" and "already used by you": a code prober should not
# be able to tell a real-but-spent code from one we never issued.
GENERIC_REJECTION = "That code isn't valid. Check it and try again."


def normalise_code(code):
    """
    A submitted code as it can safely be matched, or None if it isn't one.

    Same guard as the booking-coupon path, for the same reason: `%` and `_` are
    wildcards to Postgres, and a lookup that ever becomes a LIKE would let
    `LAUNCH_` match LAUNCH1 and hand out a discount nobody was given. Rejecting
    non-codes here means the matcher below only ever sees literals.
    """
    if not code:
        return None
    candidate = str(code).strip().upper()
    return candidate if _CODE_RE.match(candidate) else None


def describe(coupon):
    """The one line the partner sees when a code is accepted."""
    if coupon.get("description"):
        return coupon["description"]

    kind = coupon.get("kind")
    value = coupon.get("value") or 0
    months = coupon.get("duration_months")

    if kind == "deal":
        return _describe_deal(coupon)

    if kind == "free_months":
        return "1 month free" if value == 1 else f"{value} months free"

    amount = f"{value}%" if kind == "percent" else f"₹{value}"
    if not months:
        return f"{amount} off"
    return f"{amount} off for {'1 month' if months == 1 else f'{months} months'}"


def _describe_deal(coupon):
    """
    "₹299/month · 5% commission", or the two-part version when the rate is
    introductory: the partner has to be told what happens after it lapses, at
    the moment they type the code rather than in the month it changes.
    """
    fee = int(coupon.get("plan_amount_paise") or 0)
    period = coupon.get("plan_period") or "yearly"
    money = f"₹{fee // 100:,}/{'month' if period == 'monthly' else 'year'}" if fee else "Free"

    pct = coupon.get("commission_pct")
    if pct is None:
        return money

    pct = float(pct)
    rate = "no commission" if pct == 0 else f"{pct:g}% commission"
    intro = coupon.get("commission_months")
    if not intro:
        return f"{money} · {rate}"

    window = "1 month" if intro == 1 else f"{intro} months"
    return f"{money} · {rate} for {window}, then {DEFAULT_AFTER_INTRO:g}%"


def deal_terms(coupon):
    """
    The terms a deal code puts the partner on, or None for the other kinds.

    Shaped for `partner_terms.apply`, so the subscribe path can hand it straight
    over without knowing what a coupon is.
    """
    if not coupon or coupon.get("kind") != "deal":
        return None
    fee = int(coupon.get("plan_amount_paise") or 0)
    return {
        "fee_paise": fee,
        "period": coupon.get("plan_period") or "yearly",
        "commission": (
            float(coupon["commission_pct"])
            if coupon.get("commission_pct") is not None
            else DEFAULT_AFTER_INTRO
        ),
        "commission_months": coupon.get("commission_months"),
        # A paid deal lives as long as the plan is paid up; a ₹0 one has no
        # subscription to be conditional on, so gating it on one would revoke
        # the rate the first time the granted period lapsed with nothing able
        # to renew it. `apply` derives the same thing from the fee, but stating
        # it here keeps the coupon's meaning readable at the call site.
        "entitlement": "subscription" if fee > 0 else "permanent",
    }


async def resolve(code, partner_id):
    """
    The coupon row for a submitted code, or (None, reason).

    Everything a partner could influence is checked here: the code exists, it's
    switched on, we're inside its window, the campaign hasn't been exhausted,
    and this partner hasn't already redeemed it. What is NOT decided here is
    whether the discount gets granted — `redeem_plan_coupon` re-checks the
    window and the cap under a row lock, because between this read and the
    mandate authenticating, the last slot of a campaign can go to someone else.
    """
    normalised = normalise_code(code)
    if not normalised:
        return None, GENERIC_REJECTION

    # Exact match, never LIKE — see normalise_code. Codes are stored upper-cased
    # and the unique index is on upper(code), so equality is both correct and
    # the shape the index is built for.
    res = await run_db(
        lambda: get_db()
        .table("platform_coupons")
        .select("*")
        .eq("code", normalised)
        .eq("active", True)
        .maybe_single()
        .execute()
    )
    coupon = res.data if res else None
    if not coupon:
        return None, GENERIC_REJECTION

    now = datetime.now(timezone.utc)
    starts_at = _parse(coupon.get("starts_at"))
    ends_at = _parse(coupon.get("ends_at"))
    if starts_at and now < starts_at:
        return None, GENERIC_REJECTION
    if ends_at and now > ends_at:
        return None, "That code has expired."

    limit = coupon.get("usage_limit")
    if limit is not None and (coupon.get("used_count") or 0) >= limit:
        return None, "That code has been fully claimed."

    # A plan coupon is a joining offer, not a monthly one.
    if partner_id:
        seen = await run_db(
            lambda: get_db()
            .table("platform_coupon_redemptions")
            .select("id")
            .eq("coupon_id", coupon["id"])
            .eq("partner_id", partner_id)
            .maybe_single()
            .execute()
        )
        if seen and seen.data:
            return None, "You've already used that code."

    # An offer-backed coupon with no offer id would validate here and then
    # charge full price. The DB constraint forbids storing one, so this is
    # belt-and-braces against a row edited around it.
    if coupon["kind"] in ("percent", "flat") and not coupon.get("razorpay_offer_id"):
        logger.error("[plan-coupon] %s has no razorpay_offer_id", coupon.get("code"))
        return None, GENERIC_REJECTION

    # Same reasoning for a deal code: no price means we cannot build a plan for
    # it, and accepting it would subscribe the partner to standard ₹499 terms
    # while telling them they were getting ₹299.
    if coupon["kind"] == "deal" and not coupon.get("plan_period"):
        logger.error("[plan-coupon] deal %s has no plan_period", coupon.get("code"))
        return None, GENERIC_REJECTION

    return coupon, None


def free_months(coupon):
    """Months of plan this coupon grants outright, or 0 for a Razorpay offer."""
    if not coupon or coupon.get("kind") != "free_months":
        return 0
    return int(coupon.get("value") or 0)


async def park_pending(partner_id, coupon_id):
    """
    Remember the validated coupon against the partner until the mandate
    authenticates. `pending_plan_coupon_id` is a guarded column — a partner
    cannot write it themselves, which is the point.
    """
    await run_db(
        lambda: get_db()
        .table("partners")
        .update({"pending_plan_coupon_id": coupon_id})
        .eq("id", partner_id)
        .execute()
    )


async def clear_pending(partner_id):
    await run_db(
        lambda: get_db()
        .table("partners")
        .update({"pending_plan_coupon_id": None})
        .eq("id", partner_id)
        .execute()
    )


async def granted_free_period(subscription_id):
    """
    True when this mandate already had a free-months coupon redeemed against
    it. The settle path needs this to tell "no coupon here" apart from "the
    other path got the coupon first" — the two look identical from
    `apply_pending`, and treating the second as the first would ledger a charge
    that never happened.
    """
    if not subscription_id:
        return False
    res = await run_db(
        lambda: get_db()
        .table("platform_coupon_redemptions")
        .select("months_granted")
        .eq("subscription_id", subscription_id)
        .limit(1)
        .execute()
    )
    rows = (res.data or []) if res else []
    return bool(rows) and bool(rows[0].get("months_granted"))


async def apply_pending(partner_id, subscription_id):
    """
    Redeem the parked coupon now that the mandate is real. Returns the months
    of plan granted (0 when there was no coupon, or it was a Razorpay offer
    that needs nothing from us).

    Idempotent by construction: `redeem_plan_coupon` returns false on the
    second call for the same (coupon, partner), so a replayed webhook grants
    nothing. The pending pointer is cleared either way, so a coupon that lost
    its last slot to someone else doesn't sit there being retried forever.
    """
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("pending_plan_coupon_id")
        .eq("id", partner_id)
        .maybe_single()
        .execute()
    )
    coupon_id = (res.data or {}).get("pending_plan_coupon_id") if res else None
    if not coupon_id:
        return 0

    coupon_res = await run_db(
        lambda: get_db()
        .table("platform_coupons")
        .select("*")
        .eq("id", coupon_id)
        .maybe_single()
        .execute()
    )
    coupon = coupon_res.data if coupon_res else None
    if not coupon:
        await clear_pending(partner_id)
        return 0

    months = free_months(coupon)

    # One statement does the cap check, the redemption insert and the counter
    # bump under a row lock — see the migration. False means the code was spent
    # or already redeemed by this partner; nothing is granted on false.
    redeemed = await run_db(
        lambda: get_db()
        .rpc(
            "redeem_plan_coupon",
            {
                "p_coupon_id": coupon_id,
                "p_partner_id": partner_id,
                "p_subscription_id": subscription_id,
                "p_months_granted": months or None,
            },
        )
        .execute()
    )
    await clear_pending(partner_id)

    granted = bool(redeemed.data) if redeemed else False
    if not granted:
        logger.info(
            "[plan-coupon] %s not redeemable for partner %s (spent or already used)",
            coupon.get("code"),
            partner_id,
        )
        return 0

    # A deal code's real payload is the terms, not the months. Written here
    # rather than at subscribe for the same reason the redemption is: until the
    # mandate authenticates, nobody has agreed to anything, and a partner who
    # opened Checkout and closed it must not walk away on 0% commission.
    #
    # Behind the one-redemption-per-partner guard above, so a replayed webhook
    # cannot re-apply terms an admin has since corrected by hand.
    terms = deal_terms(coupon)
    if terms:
        try:
            await partner_terms.apply(
                partner_id,
                source="coupon",
                coupon_id=coupon["id"],
                notes=f"Redeemed {coupon.get('code')}",
                **terms,
            )
        except Exception:  # noqa: BLE001
            # The redemption is spent and cannot be replayed, so failing quietly
            # would leave the partner on standard terms holding a used code.
            # Loud, and recoverable by hand from the redemption row.
            logger.exception(
                "[plan-coupon] TERMS NOT APPLIED for partner %s after redeeming %s",
                partner_id,
                coupon.get("code"),
            )

    logger.info(
        "[plan-coupon] %s redeemed by partner %s (%s months)",
        coupon.get("code"),
        partner_id,
        months,
    )
    return months
