"""
The ₹999 mandate: what a hotel buys, and how it stays bought.

    plan (subscription_plans)  ──► Razorpay Plan, created once
        └── start()            ──► Razorpay Subscription, status 'pending'
              └── owner authorises in Checkout
                    └── webhook 'subscription.authenticated'  ──► active
                          └── webhook 'subscription.charged'  ──► credits, per cycle
                                └── … monthly, without anyone logging in

**Nothing here trusts the browser.** The price comes from `subscription_plans`,
the discount from `plan_coupons`, and the status from a signature-verified
webhook. The Checkout callback the browser returns with is verified too, and it
still only ever *narrows* the state to what Razorpay already said — see
`confirm_from_browser`, which cannot activate anything on its own.

**Credits are granted by the charge, never by the authorisation.** A mandate
can be authenticated and then fail to collect; granting on authentication would
hand a month of AI to anyone who can approve a UPI mandate and cancel it. The
grant key is the Razorpay payment id, so a redelivered `subscription.charged`
grants nothing and a genuine renewal next month grants again.

**This lives in `services/billing`, not `services/pms`.** The rule
`tests/test_billing.py::test_the_pms_never_imports_billing` protects is that a
hotel's front desk keeps working when its credits run out — and it caught this
file the moment it was written under `pms/`. It was right to: a subscription
service *is* billing, and putting it beside `frontdesk.py` would have made the
one directory that must never depend on a wallet depend on a wallet.

**A failed payment does not switch the hotel off.** `GRACE_DAYS` is written
onto the row when a charge fails, and the AI keeps answering until it passes.
The alternative — a hotel's guests hitting a dead assistant because a card
expired on a Sunday — is a worse outcome than a few days of unpaid service, and
it is the outcome an automated system produces by default.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.client import get_db, run_db
from app.services import razorpay
from app.services.billing import plan_coupons
from app.services.billing import service as billing

logger = logging.getLogger(__name__)

PLAN_CODE = "starter"

# How many cycles the mandate is authorised for. Razorpay requires a number —
# there is no "until cancelled" — and a monthly plan is capped at 100. Eight
# years is functionally forever and still shows the owner a bounded
# authorisation on the consent screen, which is the point of the cap existing.
TOTAL_CYCLES = 96

# How long a hotel keeps working after a payment fails.
#
# Seven days is two weekends' worth of chances for somebody to notice an email
# and update a card. It is also short enough that a hotel which has genuinely
# stopped paying is not running for a month on our costs.
GRACE_DAYS = 7

# Razorpay's vocabulary → ours. Their `halted` (retries exhausted) and
# `pending` (a charge failed, retries continuing) are both "we have not been
# paid", and both are `past_due` here: the hotel sees one state and one thing
# to do about it.
STATUS_MAP = {
    "created":       "pending",
    "authenticated": "active",
    "active":        "active",
    "pending":       "past_due",
    "halted":        "past_due",
    "paused":        "paused",
    "cancelled":     "cancelled",
    "completed":     "expired",
    "expired":       "expired",
}

# The states in which a hotel's AI keeps working.
LIVE_STATUSES = ("trialing", "active", "past_due", "paused")


class SubscriptionError(Exception):
    """A refusal already written for a hotel owner to read."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Checkout:
    """Everything the browser needs, and nothing it could lie with."""

    razorpay_subscription_id: str
    razorpay_key_id: str
    plan_name: str
    amount_paise: int
    first_payment_paise: int
    currency: str
    coupon_code: str | None = None
    coupon_label: str | None = None


# ── the plan ─────────────────────────────────────────────────────────────────


def _plan_row() -> dict:
    rows = (
        get_db().table("subscription_plans").select("*")
        .eq("code", PLAN_CODE).is_("effective_to", "null").limit(1)
        .execute().data or []
    )
    if not rows:
        raise SubscriptionError("StayOS isn't available to subscribe to right now.")
    return rows[0]


async def plan() -> dict:
    """
    The one plan, from the database.

    There is exactly one source of truth for ₹999 and this is it. The landing
    page, the checkout, the billing screen and Razorpay all resolve to this row
    — which is the fix for having found ₹999, ₹2,499 and ₹0 on three screens of
    the same product.
    """
    return await run_db(_plan_row)


def _store_razorpay_plan(plan_id: str, razorpay_plan_id: str, price_paise: int) -> None:
    get_db().table("subscription_plans").update({
        "razorpay_plan_id": razorpay_plan_id,
        "razorpay_plan_price_paise": price_paise,
    }).eq("id", plan_id).execute()


async def ensure_razorpay_plan(plan_row: dict) -> str:
    """
    The Razorpay Plan id for this plan, creating it the first time.

    **A price that has moved invalidates the id.** A Razorpay Plan's amount is
    immutable; changing `price_paise` here and reusing the old plan id would
    charge every new hotel the old amount while every screen showed the new
    one. So the price the plan was minted at is stored beside the id, and a
    mismatch mints a new plan rather than trusting the stale one.
    """
    price = int(plan_row["price_paise"])
    existing = plan_row.get("razorpay_plan_id")
    minted_at = plan_row.get("razorpay_plan_price_paise")

    if existing and minted_at == price:
        return existing

    if existing:
        logger.warning(
            "plan price moved %s → %s paise; minting a new Razorpay plan",
            minted_at, price,
        )

    created = await razorpay.create_plan(
        amount_paise=price,
        name=plan_row.get("name") or "StayOS",
        period=plan_row.get("billing_period") or "monthly",
    )
    razorpay_plan_id = created["id"]
    await run_db(_store_razorpay_plan, plan_row["id"], razorpay_plan_id, price)
    logger.info("minted Razorpay plan %s at %s paise", razorpay_plan_id, price)
    return razorpay_plan_id


# ── the hotel's mandate ──────────────────────────────────────────────────────


def _latest_subscription(property_id: str) -> dict | None:
    """
    The most recent mandate, **whatever state it is in**.

    It used to filter to the live states, and a hotel that had just cancelled
    therefore read as having no subscription at all — so the screen said "Your
    StayOS plan is ready" to somebody who had cancelled thirty seconds earlier,
    and never once said "cancelled". Every state has a sentence in `HEADLINE`;
    the way to show them is to fetch the row that has one.

    Only property-scoped rows. The nine account-level rows the seed left behind
    carry a null `property_id` and no Razorpay mandate, and treating one of
    those as this hotel's subscription would be inventing a plan nobody bought.
    """
    rows = (
        get_db().table("subscriptions")
        .select("*, subscription_plans(code,name,price_paise,billing_period,credit_grant,signup_bonus_credits,signup_bonus_label)")
        .eq("property_id", property_id)
        .order("created_at", desc=True).limit(1)
        .execute().data or []
    )
    return rows[0] if rows else None


def _by_razorpay_id(razorpay_subscription_id: str) -> dict | None:
    rows = (
        get_db().table("subscriptions")
        .select("*, subscription_plans(code,name,price_paise,credit_grant,signup_bonus_credits,signup_bonus_label)")
        .eq("razorpay_subscription_id", razorpay_subscription_id)
        .limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _billing_account(property_id: str) -> str | None:
    rows = (
        get_db().table("properties").select("billing_account_id")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return (rows[0] or {}).get("billing_account_id") if rows else None


async def current(property_id: str) -> dict | None:
    return await run_db(_latest_subscription, property_id)


def _write_pending(row: dict) -> dict:
    db = get_db()
    # An abandoned checkout leaves a `pending` row, and the partial unique index
    # allows only one live mandate per property. So a hotel that opens Checkout,
    # closes it, and comes back tomorrow updates its own pending row rather than
    # colliding with it.
    existing = (
        db.table("subscriptions").select("id")
        .eq("property_id", row["property_id"]).eq("status", "pending")
        .limit(1).execute().data or []
    )
    if existing:
        return (
            db.table("subscriptions").update(row)
            .eq("id", existing[0]["id"]).execute().data or [{}]
        )[0]
    return db.table("subscriptions").insert(row).execute().data[0]


async def start(
    *,
    property_id: str,
    partner_id: str,
    coupon_code: str | None = None,
) -> Checkout:
    """
    Open a mandate and hand the browser what it needs to authorise it.

    Nothing is active when this returns and no credits have been granted. The
    row is written `pending` **before** the id reaches the browser, for the same
    reason the booking engine writes its order first: Razorpay can call the
    webhook while the owner is still looking at the consent screen, and a
    webhook that finds no row has nothing to update.
    """
    if not razorpay.configured():
        raise SubscriptionError(
            "Card payments aren't switched on yet. Please try again shortly."
        )

    live = await current(property_id)
    if live and live["status"] in LIVE_STATUSES:
        raise SubscriptionError("StayOS is already running on this hotel.")

    plan_row = await plan()
    price = int(plan_row["price_paise"])

    discount = None
    if coupon_code:
        # Raises `CouponError`, whose message is already written for an owner.
        discount = await plan_coupons.resolve(
            coupon_code, partner_id=partner_id, price_paise=price,
        )

    razorpay_plan_id = await ensure_razorpay_plan(plan_row)

    created = await razorpay.create_subscription(
        plan_id=razorpay_plan_id,
        total_count=TOTAL_CYCLES,
        notes={
            # These ride on every Razorpay event and are how a dashboard row is
            # traced back to a hotel without a lookup in our database.
            "property_id": str(property_id),
            "partner_id": str(partner_id),
            "plan": PLAN_CODE,
        },
        start_at=plan_coupons.start_at_for(discount),
        offer_id=discount.razorpay_offer_id if discount else None,
    )

    await run_db(_write_pending, {
        "property_id": property_id,
        "billing_account_id": await run_db(_billing_account, property_id),
        "plan_id": plan_row["id"],
        "status": "pending",
        "razorpay_subscription_id": created["id"],
        "amount_paise": price,
        "currency": "INR",
        "coupon_id": discount.coupon_id if discount else None,
        "coupon_code": discount.code if discount else None,
        "current_period_start": datetime.now(timezone.utc).isoformat(),
        "current_period_end": (
            datetime.now(timezone.utc) + timedelta(days=31)
        ).isoformat(),
        "last_event_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"createdVia": "owner_checkout"},
    })

    logger.info("mandate %s opened for property %s", created["id"], property_id)
    return Checkout(
        razorpay_subscription_id=created["id"],
        razorpay_key_id=razorpay.public_key_id() or "",
        plan_name=plan_row.get("name") or "StayOS",
        amount_paise=price,
        first_payment_paise=discount.first_payment_paise if discount else price,
        currency="INR",
        coupon_code=discount.code if discount else None,
        coupon_label=discount.label if discount else None,
    )


# ── what the webhook does ────────────────────────────────────────────────────


def _update(subscription_id: str, patch: dict) -> dict | None:
    patch = {**patch, "last_event_at": datetime.now(timezone.utc).isoformat()}
    rows = (
        get_db().table("subscriptions").update(patch)
        .eq("id", subscription_id).execute().data or []
    )
    return rows[0] if rows else None


async def apply_status(razorpay_subscription_id: str, razorpay_status: str,
                       entity: dict | None = None) -> dict | None:
    """
    Move the hotel to whatever Razorpay says it is.

    The entity's own `status` is trusted over the event name, because Razorpay
    sends both and the entity is what its dashboard shows. An unknown status is
    left alone rather than guessed at — a state we do not understand is not a
    reason to switch a hotel off.
    """
    row = await run_db(_by_razorpay_id, razorpay_subscription_id)
    if not row:
        logger.info("event for unknown mandate %s", razorpay_subscription_id)
        return None

    mapped = STATUS_MAP.get((razorpay_status or "").lower())
    if not mapped:
        logger.warning("unmapped Razorpay status %r on %s",
                       razorpay_status, razorpay_subscription_id)
        return row

    patch: dict = {"status": mapped}
    entity = entity or {}

    if mapped == "active":
        patch["failure_reason"] = None
        patch["grace_until"] = None
        if not row.get("started_at"):
            patch["started_at"] = datetime.now(timezone.utc).isoformat()
    elif mapped == "past_due":
        # Written once and then left alone: a second failed charge inside the
        # same grace window must not extend it, or a hotel that never pays
        # keeps buying another week with every retry.
        if not row.get("grace_until"):
            patch["grace_until"] = (
                datetime.now(timezone.utc) + timedelta(days=GRACE_DAYS)
            ).isoformat()
        patch["failure_reason"] = "Your last payment didn't go through."
    elif mapped == "cancelled":
        patch["cancelled_at"] = datetime.now(timezone.utc).isoformat()
        patch["grace_until"] = None

    for ours, theirs in (("current_period_start", "current_start"),
                         ("current_period_end", "current_end")):
        stamp = entity.get(theirs)
        if stamp:
            patch[ours] = datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat()

    # `current_period_end > current_period_start` is a CHECK on the table, and
    # Razorpay sends a null `current_end` on a mandate that has not charged yet.
    if patch.get("current_period_end") and not patch.get("current_period_start"):
        patch.pop("current_period_end")

    updated = await run_db(_update, row["id"], patch)
    logger.info("mandate %s → %s", razorpay_subscription_id, mapped)
    return updated


async def record_charge(
    *,
    razorpay_subscription_id: str,
    payment_id: str,
    amount_paise: int | None = None,
    invoice_id: str | None = None,
) -> dict:
    """
    A cycle was paid for. Mark it active and grant the month's credits.

    **The payment id is the grant key**, so this is safe to call as many times
    as Razorpay sends it. A renewal next month arrives with a different payment
    id and grants again, which is exactly the behaviour wanted: the credits are
    per cycle, not per subscription.
    """
    row = await run_db(_by_razorpay_id, razorpay_subscription_id)
    if not row:
        logger.info("charge for unknown mandate %s", razorpay_subscription_id)
        return {"status": "unknown"}

    plan_row = row.get("subscription_plans") or {}
    grant_credits = int(plan_row.get("credit_grant") or 0)
    account = row.get("billing_account_id")

    await run_db(_update, row["id"], {
        "status": "active",
        "razorpay_payment_id": payment_id,
        "failure_reason": None,
        "grace_until": None,
        "started_at": row.get("started_at") or datetime.now(timezone.utc).isoformat(),
        "amount_paise": amount_paise or row.get("amount_paise"),
    })

    granted = {}
    if account and grant_credits > 0:
        granted = await billing.grant(
            billing_account_id=account,
            kind="subscription",
            credits=grant_credits,
            # The whole idempotency argument, in one string. Keyed on the
            # payment id, so a redelivered event grants nothing and next
            # month's genuine charge — a different payment — grants again.
            idempotency_key=f"subscription:{razorpay_subscription_id}:{payment_id}",
            source_type="subscription",
            source_id=row["id"],
            actor_type="system",
            metadata={
                "reason": f"{plan_row.get('name') or 'StayOS'} subscription",
                "paymentId": payment_id,
                "invoiceId": invoice_id,
            },
        )
        logger.info(
            "granted %s credits to %s for payment %s (duplicate=%s)",
            grant_credits, account, payment_id, granted.get("duplicate"),
        )

    # ── the launch bonus ────────────────────────────────────────────────────
    #
    # Once per hotel, on the first cycle it pays for. The key is the **billing
    # account**, not the payment, so a hotel that cancels in October and comes
    # back in December does not collect it a second time — and a redelivered
    # webhook, which is the common case, grants nothing either.
    #
    # Its own bucket kind and its own label, so the hotel's credit history
    # reads "+2,000 Launch bonus" beside "+3,000 StayOS subscription" rather
    # than one unexplained 5,000. The two have different lifetimes and an
    # owner asking "what did I actually buy" needs to see which is which.
    bonus_amount = int(plan_row.get("signup_bonus_credits") or 0)
    bonus_label = plan_row.get("signup_bonus_label") or "Launch bonus"
    bonus = {}
    if account and bonus_amount > 0:
        bonus = await billing.grant(
            billing_account_id=account,
            kind="promotional",
            credits=bonus_amount,
            idempotency_key=f"signup-bonus:{account}",
            source_type="subscription",
            source_id=row["id"],
            actor_type="system",
            metadata={"reason": bonus_label, "paymentId": payment_id},
        )
        if not bonus.get("duplicate"):
            logger.info("granted %s %s credits to %s", bonus_amount, bonus_label, account)

    return {
        "status": "charged",
        "subscription_id": row["id"],
        "credits_granted": 0 if granted.get("duplicate") else grant_credits,
        "bonus_granted": 0 if (bonus.get("duplicate") or not bonus) else bonus_amount,
        "duplicate": bool(granted.get("duplicate")),
    }


async def cancel(property_id: str, *, at_cycle_end: bool = True) -> dict:
    """
    Stop the mandate. Razorpay first, then our row.

    In that order deliberately: if Razorpay refuses, the hotel keeps a
    subscription that is still going to charge them, and a row saying
    'cancelled' beside a live mandate is the one state nobody could debug.
    """
    row = await current(property_id)
    if not row or row["status"] not in LIVE_STATUSES:
        raise SubscriptionError("There's no active plan to cancel.")

    mandate = row.get("razorpay_subscription_id")
    if mandate:
        result = await razorpay.cancel_subscription(mandate, at_cycle_end=at_cycle_end)
        if result is None:
            raise SubscriptionError(
                "We couldn't cancel with our payments provider. Please try again."
            )

    patch = {"cancel_at_period_end": at_cycle_end}
    if not at_cycle_end:
        patch |= {"status": "cancelled",
                  "cancelled_at": datetime.now(timezone.utc).isoformat()}
    return await run_db(_update, row["id"], patch) or {}


# ── what the owner is shown ──────────────────────────────────────────────────


def is_serving(row: dict | None) -> bool:
    """
    Should this hotel's AI still answer guests?

    True for no subscription at all, deliberately. A hotel that has not
    subscribed yet is in onboarding, and cutting its AI off before it has seen
    the thing work is how a trial becomes a refund. What stops that hotel is
    the credit balance, which is a number they can see.
    """
    if not row:
        return True
    if row["status"] in ("trialing", "active", "paused"):
        return True
    if row["status"] == "past_due":
        grace = row.get("grace_until")
        if not grace:
            return True
        try:
            return datetime.fromisoformat(str(grace).replace("Z", "+00:00")) > \
                datetime.now(timezone.utc)
        except ValueError:
            return True
    return False


# One sentence per state, written for a hotel owner. No status string reaches a
# screen — `past_due` is not a thing anybody outside this file should read.
HEADLINE = {
    "none":     ("Your StayOS plan is ready", "Start it whenever you're ready."),
    "pending":  ("Waiting for your approval",
                 "Finish approving the payment and your AI employee starts working."),
    "trialing": ("StayOS is active", "You're on a free period."),
    "active":   ("StayOS is active", ""),
    "past_due": ("Your last payment didn't go through",
                 "Update your payment method to keep your AI employee running."),
    "paused":   ("StayOS is paused", "Resume whenever you're ready."),
    "cancelled": ("Your StayOS plan is cancelled",
                  "Your AI employee has stopped answering guests."),
    "expired":  ("Your StayOS plan has ended", "Start it again whenever you like."),
}


def project(row: dict | None, plan_row: dict) -> dict:
    """One shape, so the billing screen and the copilot cannot disagree."""
    status = (row or {}).get("status") or "none"
    headline, detail = HEADLINE.get(status, HEADLINE["none"])
    return {
        "status": status,
        "headline": headline,
        "detail": detail,
        "planName": plan_row.get("name") or "StayOS",
        "pricePaise": int(plan_row.get("price_paise") or 0),
        "billingPeriod": plan_row.get("billing_period") or "monthly",
        "includedCredits": int(plan_row.get("credit_grant") or 0),
        "signupBonusCredits": int(plan_row.get("signup_bonus_credits") or 0),
        "signupBonusLabel": plan_row.get("signup_bonus_label") or "Launch bonus",
        "currentPeriodEnd": (row or {}).get("current_period_end"),
        "cancelAtPeriodEnd": bool((row or {}).get("cancel_at_period_end")),
        "couponCode": (row or {}).get("coupon_code"),
        "graceUntil": (row or {}).get("grace_until"),
        "serving": is_serving(row),
        "canStart": status in ("none", "cancelled", "expired"),
    }
