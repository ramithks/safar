"""
Partner commercial terms — what a partner pays us, and what we take.

The chain, and the direction is the whole point:

    partners → partner_terms → listings → bookings.commission_pct

`partner_terms` is the authority. `partner_listings.commission_pct` is a copy
kept in step so the website and the partner's own dashboard can show a rate
without a join; it is never consulted to decide what to charge. A partner sold
"₹499/month, 0% commission" must not discover that the hotel they listed in June
is still quietly at 10%, so `apply` rewrites every listing they own.

`bookings.commission_pct` is the opposite of a copy — it is a snapshot, written
once at creation and never touched again. Renegotiating a rate changes what the
NEXT booking costs; a stay already sold at 10% owes 10% for the rest of time.

A partner with no row is on standard terms. That is deliberate: the default is
not seeded into every partner at signup, so changing what "standard" means is a
constant here rather than a migration over the whole table.

Two things a deal is NOT:

  It is not unconditional. `entitlement` says what keeps it alive — usually a
  paid-up plan. A partner given "0% while subscribed" who then cancels reverts
  to standard commission on their NEXT booking; the ones they already sold keep
  what they were sold at. Without this a terms row outlives the subscription
  that justified it and the discount becomes permanent by accident.

  It is not what Razorpay is charging. There is no API to reprice a live
  mandate, so changing terms changes what we have AGREED and nothing about what
  is being COLLECTED. `partners.billed_amount_paise` records the latter and
  `billing_mismatch` compares them. Pretending the mandate followed the deal is
  how a partner ends up told they're on ₹299/month while ₹499/year leaves their
  account.

Nothing here depends on a cron job for correctness. Every lapse — intro period,
entitlement, plan expiry — is decided by comparing stored timestamps to now, so
a job that fails to run makes the stored row stale and never makes the money
wrong. The hourly task only tidies.
"""
# NOTE: deliberately no `from __future__ import annotations` — see the note in
# app/api/v1/admin/endpoint.py.
import logging
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from app.db.client import get_db, run_db, run_db_resilient

logger = logging.getLogger(__name__)

# Standard terms: the founding-partner deal everyone gets without a code.
# Mirrors KIND_RULES in the website's lib/marketplace/types.ts and the
# COMMISSION_PCT table in marketplace_pricing.py — all three must agree on 10.
DEFAULT_FEE_PAISE = 49_900
DEFAULT_PERIOD = "yearly"
DEFAULT_COMMISSION_PCT = 10.0

PERIOD_MONTHS = {"monthly": 1, "yearly": 12}


def period_months(period):
    """
    Months in one billing cycle.

    This is the number that used to be the constant `PLAN_PERIOD_MONTHS = 12`,
    and getting it from the partner instead of from a module is the difference
    between a ₹299/month partner buying one month and buying twelve.
    """
    return PERIOD_MONTHS.get(period or DEFAULT_PERIOD, 12)


def default_terms():
    """
    Standard terms, shaped like a row so callers need no special case.

    Entitlement is `permanent` here, and that is not a loophole: the standard
    deal has no discount to lose. Gating it on a subscription would mean a
    lapsed partner's bookings revert from 10% to... 10%.
    """
    return {
        "subscription_fee_paise": DEFAULT_FEE_PAISE,
        "billing_period": DEFAULT_PERIOD,
        "commission_pct": DEFAULT_COMMISSION_PCT,
        "commission_expires_at": None,
        "base_commission_pct": DEFAULT_COMMISSION_PCT,
        "entitlement": "permanent",
        "entitlement_expires_at": None,
        "effective_at": None,
        "source": "default",
        "source_coupon_id": None,
        "notes": None,
    }


async def _plan_paid_through(partner_id):
    """
    When this partner's plan is paid up to, or None.

    Reads the TIMESTAMP rather than `stay_plan_status`, on purpose. The status
    is moved to `past_due` by the nightly `expire_stay_plans()` job, so trusting
    it would make a partner's commission depend on whether a cron ran — exactly
    what this module refuses to do. It also tells the truth in the case that
    matters most: a recurring charge that fails marks the mandate halted and
    leaves this date alone, so the partner keeps the rate they paid for until
    the period they bought genuinely ends.
    """
    res = await run_db(
        lambda: get_db()
        .table("partners")
        .select("stay_plan_expires_at")
        .eq("id", partner_id)
        .maybe_single()
        .execute()
    )
    row = (res.data if res else None) or {}
    return _parse(row.get("stay_plan_expires_at"))


async def is_entitled(partner_id, terms):
    """
    Whether this partner still has the deal their terms describe.

    False means the deal has lapsed and its commission no longer applies —
    `resolve` falls back to `base_commission_pct`. It does NOT mean the row is
    wrong or should be deleted: a lapsed partner who pays again is entitled to
    the same deal, so the terms stay where they are.
    """
    kind = (terms or {}).get("entitlement") or "subscription"

    if kind == "permanent":
        return True

    now = datetime.now(timezone.utc)
    if kind == "until":
        expires = _parse(terms.get("entitlement_expires_at"))
        return bool(expires and now < expires)

    # subscription
    paid_through = await _plan_paid_through(partner_id)
    return bool(paid_through and now < paid_through)


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def resolve(partner_id):
    """
    The EFFECTIVE terms a partner is on right now, always a full dict.

    Effective, not stored. Two things can make the stored rate untrue, and both
    are decided here by reading the clock rather than by trusting a job:

      the intro period ran out   → the rate is `base_commission_pct`
      the deal lapsed entirely   → the rate is `base_commission_pct`

    A booking made one minute after either boundary must be charged the real
    rate. If the hourly tidy-up job never ran again, every number this function
    returns would still be correct — the stored row would simply disagree with
    it, which is a cosmetic problem and not a financial one.

    `lapsed` is reported rather than hidden so callers can explain themselves:
    the plan page needs to say "your 0% ended when the plan lapsed", not
    silently start charging 10%.
    """
    if not partner_id:
        return default_terms()

    res = await run_db(
        lambda: get_db()
        .table("partner_terms")
        .select("*")
        .eq("partner_id", partner_id)
        .maybe_single()
        .execute()
    )
    row = (res.data if res else None) or None
    if not row:
        return default_terms()

    terms = dict(row)
    terms.setdefault("entitlement", "subscription")
    base = float(terms.get("base_commission_pct") or DEFAULT_COMMISSION_PCT)
    terms["commission_pct"] = float(terms.get("commission_pct") or 0)
    terms["lapsed"] = False
    terms["intro_ended"] = False

    # 1. Has the deal itself lapsed? Checked first: an intro rate on a dead deal
    #    is moot, and reporting "intro ended" for a cancelled partner would send
    #    the plan page chasing the wrong explanation.
    if not await is_entitled(partner_id, terms):
        terms["commission_pct"] = base
        terms["commission_expires_at"] = None
        terms["lapsed"] = True
        return terms

    # 2. Has the introductory period run out?
    expires = _parse(terms.get("commission_expires_at"))
    if expires and datetime.now(timezone.utc) >= expires:
        terms["commission_pct"] = base
        terms["commission_expires_at"] = None
        terms["intro_ended"] = True

    return terms


async def commission_pct(partner_id, fallback=None):
    """
    The rate to charge a booking being created right now.

    `fallback` is the kind's default, used only when there is no partner at all
    (our own trips, an orphaned listing). A partner who exists always has a
    rate, even if it is the standard one.
    """
    if not partner_id:
        return float(fallback if fallback is not None else DEFAULT_COMMISSION_PCT)
    terms = await resolve(partner_id)
    return float(terms["commission_pct"])


def describe(terms):
    """
    One line for the partner's plan page and the admin table.

    Takes RESOLVED terms, so the rate it names is the rate that will actually be
    charged — including after an intro period or a whole deal has lapsed. A
    description that still advertises 0% to a partner being charged 10% is worse
    than no description.
    """
    fee = int(terms.get("subscription_fee_paise") or 0)
    period = terms.get("billing_period") or DEFAULT_PERIOD
    pct = float(terms.get("commission_pct") or 0)

    if fee <= 0:
        money = "No subscription"
    else:
        money = f"₹{fee // 100:,}/{'month' if period == 'monthly' else 'year'}"

    rate = "no commission" if pct == 0 else f"{pct:g}% commission"

    if terms.get("lapsed"):
        return f"{money} · {rate} — deal lapsed, on standard commission"

    expires = _parse(terms.get("commission_expires_at"))
    if expires:
        base = float(terms.get("base_commission_pct") or DEFAULT_COMMISSION_PCT)
        after = "no commission" if base == 0 else f"{base:g}%"
        return f"{money} · {rate} till {expires.date().isoformat()}, then {after}"
    return f"{money} · {rate}"


# ── writing a deal ───────────────────────────────────────────────────────────


async def apply(
    partner_id,
    *,
    fee_paise,
    period,
    commission,
    commission_months=None,
    base_commission=None,
    entitlement=None,
    entitlement_expires_at=None,
    source="admin",
    coupon_id=None,
    notes=None,
    effective_at=None,
):
    """
    Put a partner on a deal, and bring their inventory with them.

    `commission_months` is what makes an intro rate: None means the rate holds
    for as long as the deal does, a number means it reverts to `base_commission`
    (standard 10% unless told otherwise) when it runs out.

    EVERY CALL RESETS THE CLOCK. `effective_at` defaults to now and the intro
    window is measured from it, so replacing "5% for 6 months" with "0% for 3
    months" gives three months from today — never three months from whenever the
    old deal started. The upsert overwrites the whole row rather than patching
    it, which is what stops a stale `commission_expires_at` surviving under a
    new deal that never mentioned one.

    Terms are written before listings are rewritten, and the listing rewrite is
    allowed to fail without taking the deal down with it: terms are what the
    money path reads, so a partner whose backfill failed is charged correctly
    and merely displays a stale number until the next write. The other order
    would charge them correctly on paper and wrongly in fact.
    """
    commission = float(commission)
    base = float(base_commission if base_commission is not None else DEFAULT_COMMISSION_PCT)
    starts = effective_at or datetime.now(timezone.utc)

    # A ₹0 deal has no mandate, so "while subscribed" would gate it on a
    # subscription that does not and will never exist — the partner would lose
    # their rate the moment `stay_plan_expires_at` ran out with nothing able to
    # renew it. Free deals are permanent unless given an explicit end date.
    if entitlement is None:
        entitlement = "subscription" if int(fee_paise) > 0 else "permanent"
    if entitlement_expires_at:
        entitlement = "until"

    expires_at = None
    if commission_months:
        if commission == base:
            # An intro period that reverts to itself is a permanent rate wearing
            # a costume, and the DB constraint rejects it. Treat it as what the
            # admin meant rather than erroring on a harmless contradiction.
            commission_months = None
        else:
            expires_at = (starts + relativedelta(months=int(commission_months))).isoformat()

    row = {
        "partner_id": partner_id,
        "subscription_fee_paise": int(fee_paise),
        "billing_period": period,
        "commission_pct": commission,
        "commission_expires_at": expires_at,
        "base_commission_pct": base,
        "entitlement": entitlement,
        "entitlement_expires_at": (
            entitlement_expires_at if entitlement == "until" else None
        ),
        "effective_at": starts.isoformat(),
        "source": source,
        "source_coupon_id": coupon_id,
        "notes": notes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    await run_db(
        lambda: get_db().table("partner_terms").upsert(row, on_conflict="partner_id").execute()
    )

    stale = await sync_listings(partner_id, commission)

    logger.info(
        "[terms] partner %s → ₹%s/%s at %s%% (entitlement=%s, source=%s)",
        partner_id,
        int(fee_paise) // 100,
        period,
        commission,
        entitlement,
        source,
    )

    # The resolved view, plus which display copies are now stale. Returned
    # rather than logged-and-forgotten so the caller can show an operator that
    # the deal landed but the listings didn't.
    applied = await resolve(partner_id)
    applied["stale_listings"] = stale
    return applied


async def sync_listings(partner_id, pct):
    """
    Push the partner's rate onto everything they have listed.

    Returns the tables that could NOT be written, so a caller can surface a
    partial failure instead of it living only in the logs.

    Three properties, because this is the one part of a deal allowed to fail:

      idempotent   It sets a column to a constant. Running it again is free,
                   and running it twice is indistinguishable from once.
      retryable    Through `run_db_resilient`, which retries an idempotent write
                   across the TLS-corruption windows the scrapers induce rather
                   than giving up on the first transport error.
      observable   Failures are logged loudly AND returned. They are not
                   silent, because "the partner's dashboard shows 10% but we
                   charge 5%" is a support conversation waiting to happen.

    What a failure here CANNOT do is overcharge anyone. Bookings are priced from
    `partner_terms`, which was written before this ran; the listing column is a
    display copy. That ordering is the whole safety argument.

    Bookings are untouched by design — `bookings.commission_pct` is a snapshot,
    and rewriting it would retroactively change what a completed stay earned.
    """
    failed = []
    for table in ("partner_listings", "group_trips"):
        try:
            await run_db_resilient(
                lambda t=table: get_db()
                .table(t)
                .update({"commission_pct": pct})
                .eq("partner_id", partner_id)
                .execute(),
                label=f"terms-sync-{table}",
                max_seconds=30.0,
            )
        except Exception:  # noqa: BLE001
            failed.append(table)
            logger.error(
                "[terms] LISTING BACKFILL FAILED: %s for partner %s is stale at "
                "a rate other than %s%%. Bookings are unaffected (they price "
                "from partner_terms); re-run by saving the terms again.",
                table,
                partner_id,
                pct,
                exc_info=True,
            )
    return failed


# ── what we agreed vs what Razorpay collects ─────────────────────────────────


async def billing_mismatch(partner, terms=None):
    """
    How the live mandate differs from the agreed deal, or None when it doesn't.

    Razorpay has no API to reprice a running subscription. Changing terms
    therefore changes what we have AGREED and nothing at all about what is being
    COLLECTED — the mandate keeps charging its original plan until somebody
    cancels it and the partner authorises a new one. This function exists so
    that gap is displayed rather than discovered on a bank statement.

    Returns None when there is no live mandate: a partner who has not subscribed
    cannot be mis-billed, and neither can one whose mandate is already cancelled.
    """
    if not partner:
        return None
    if (partner.get("subscription_status") or "none") not in _LIVE_MANDATE:
        return None

    billed_amount = partner.get("billed_amount_paise")
    billed_period = partner.get("billed_period")
    if billed_amount is None or not billed_period:
        # A mandate we have no billing record for — pre-hardening rows that the
        # migration could not resolve. Unknown is not the same as matching, but
        # claiming a mismatch we cannot prove would cry wolf on every one of
        # them, so this stays quiet and the dashboard shows "unknown".
        return None

    terms = terms or await resolve(partner["id"])
    expected_amount = int(terms["subscription_fee_paise"])
    expected_period = terms["billing_period"]

    if int(billed_amount) == expected_amount and billed_period == expected_period:
        return None

    return {
        "billed_amount_paise": int(billed_amount),
        "billed_period": billed_period,
        "expected_amount_paise": expected_amount,
        "expected_period": expected_period,
        "subscription_id": partner.get("subscription_id"),
    }


#: Mandate states that are actually collecting money, or about to. `halted` is
#: included on purpose — a halted mandate is a live agreement whose last charge
#: failed, and Razorpay resumes it on a successful retry, so it can still bill
#: the wrong amount. `cancelled`/`completed` cannot.
_LIVE_MANDATE = {"active", "authenticated", "pending", "halted"}


async def record_billing(partner_id, amount_paise, period):
    """
    Remember what the mandate we just opened will charge.

    Written at subscribe rather than inferred later from `platform_plans`,
    because a plan's price is what it costs TODAY and a mandate charges what it
    was opened at. Conflating the two is how a partner grandfathered on an old
    price silently appears to be on the current one.
    """
    await run_db(
        lambda: get_db()
        .table("partners")
        .update({"billed_amount_paise": int(amount_paise), "billed_period": period})
        .eq("id", partner_id)
        .execute()
    )


async def clear_billing(partner_id):
    """Forget the mandate's price — it is no longer collecting anything."""
    await run_db(
        lambda: get_db()
        .table("partners")
        .update({"billed_amount_paise": None, "billed_period": None})
        .eq("id", partner_id)
        .execute()
    )


async def clear(partner_id):
    """Put a partner back on standard terms."""
    await run_db(
        lambda: get_db().table("partner_terms").delete().eq("partner_id", partner_id).execute()
    )
    await sync_listings(partner_id, DEFAULT_COMMISSION_PCT)


# ── the intro period running out ─────────────────────────────────────────────


async def revert_expired():
    """
    Move every partner whose intro commission has run out onto their base rate.

    `resolve` already reports the reverted rate the moment the clock passes, so
    this job is not what keeps billing correct — it exists so the stored row and
    the partner's listings stop disagreeing with what they are actually charged.

    Deliberately scoped to INTRO EXPIRY only. A deal that has lapsed because the
    partner stopped paying is left exactly as written: `resolve` already charges
    them the base rate, and a partner who pays again is entitled to the deal
    they were given, with whatever intro time was left on it. Rewriting the row
    on lapse would destroy that and there would be nothing to restore it from.

    Returns how many partners moved.
    """
    now = datetime.now(timezone.utc).isoformat()
    res = await run_db(
        lambda: get_db()
        .table("partner_terms")
        .select("partner_id, base_commission_pct")
        .not_.is_("commission_expires_at", "null")
        .lte("commission_expires_at", now)
        .execute()
    )
    rows = (res.data or []) if res else []

    moved = 0
    for row in rows:
        partner_id = row["partner_id"]
        base = float(row.get("base_commission_pct") or DEFAULT_COMMISSION_PCT)
        try:
            await run_db(
                lambda p=partner_id, b=base: get_db()
                .table("partner_terms")
                .update(
                    {
                        "commission_pct": b,
                        "commission_expires_at": None,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                .eq("partner_id", p)
                .execute()
            )
            await sync_listings(partner_id, base)
            moved += 1
            logger.info("[terms] intro period ended for partner %s → %s%%", partner_id, base)
        except Exception:  # noqa: BLE001
            logger.exception("[terms] could not revert partner %s", partner_id)

    return moved
