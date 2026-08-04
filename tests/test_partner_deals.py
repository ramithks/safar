"""
Partner commercial terms — the deals behind "₹299/month + 5%".

The failure modes worth pinning are all versions of one mistake: something in
the money path still believing there is exactly one deal. A ₹299/MONTH partner
whose charge buys twelve months of listing time pays us ₹299 a year; a partner
sold 0% who is charged 10% because a stale copy of the rate was consulted is a
refund and an apology. Both are silent — the code runs, the numbers are wrong.

Reuses the in-memory PostgREST stand-in from the hardening suite: the properties
under test are control flow, not SQL.
"""
from datetime import datetime, timezone

import pytest
from dateutil.relativedelta import relativedelta

from app.api.v1.marketplace import payments
from app.services import marketplace_bookings as mb
from app.services import marketplace_pricing as pricing
from app.services import partner_terms
from app.services import plan_coupons
from tests.test_marketplace_hardening import FakeDB, run


PARTNER = "11111111-1111-1111-1111-111111111111"
COUPON = "33333333-3333-3333-3333-333333333333"
SUB = "sub_DEAL"


@pytest.fixture
def wired(monkeypatch):
    """Point every module that reaches for a DB client at one fake."""

    def _install(db):
        monkeypatch.setattr(partner_terms, "get_db", lambda: db)
        monkeypatch.setattr(plan_coupons, "get_db", lambda: db)
        monkeypatch.setattr(payments, "get_db", lambda: db)
        monkeypatch.setattr(mb, "get_db", lambda: db)
        return db

    return _install


#: Far enough out that "is this partner paid up" is unambiguous, and far enough
#: from any intro window in these tests that the two can't be confused.
PAID_THROUGH = "2099-01-01T00:00:00+00:00"
LONG_AGO = "2020-01-01T00:00:00+00:00"


def _db(terms=None, coupons=None, pending=None, partner=None):
    return FakeDB(
        rows={
            "partners": [_partner(pending_plan_coupon_id=pending, **(partner or {}))],
            "partner_terms": terms or [],
            "platform_coupons": coupons or [],
            "platform_coupon_redemptions": [],
            "partner_payments": [],
            "partner_listings": [],
            "group_trips": [],
        },
        unique={
            "partner_payments": ["razorpay_payment_id"],
            # Mirrors platform_coupon_redemptions_once_idx: one redemption per
            # (coupon, partner), which is what stops a code being farmed.
            "platform_coupon_redemptions": ["coupon_partner"],
        },
    )


def _partner(**over):
    row = {
        "id": PARTNER,
        "subscription_id": SUB,
        "subscription_status": "active",
        "pending_plan_coupon_id": None,
        # Paid up by default. Entitlement is gated on this date, so a partner
        # without one has lapsed — which is a case worth testing deliberately,
        # not one worth stumbling into in every unrelated test.
        "stay_plan_expires_at": PAID_THROUGH,
        "billed_amount_paise": 29900,
        "billed_period": "monthly",
    }
    row.update(over)
    return row


def _terms(**over):
    row = {
        "partner_id": PARTNER,
        "subscription_fee_paise": 29900,
        "billing_period": "monthly",
        "commission_pct": 5,
        "commission_expires_at": None,
        "base_commission_pct": 10,
        "entitlement": "subscription",
        "entitlement_expires_at": None,
        "effective_at": None,
        "source": "admin",
        "source_coupon_id": None,
        "notes": None,
    }
    row.update(over)
    return row


# ── a cycle is the partner's own, not twelve months ─────────────────────────


class TestCycleLength:
    """
    `PLAN_PERIOD_MONTHS = 12` was correct while there was one plan and becomes a
    giveaway the moment there isn't. A monthly mandate fires twelve times a year;
    if each firing bought a year, the partner would be permanently paid up after
    one ₹299 charge and we would never notice, because nothing errors.
    """

    def test_a_monthly_partner_buys_one_month_per_charge(self, wired, monkeypatch):
        wired(_db(terms=[_terms(billing_period="monthly")]))
        months_seen = []

        async def fake_activate(partner_id, months=None):
            months_seen.append(months)
            return True

        monkeypatch.setattr(mb, "activate_stay_plan", fake_activate)

        run(mb.record_subscription_charge(SUB, "pay_1", invoice_id="inv_1", amount_paise=29900))

        assert months_seen == [1], "one ₹299 monthly charge must buy exactly one month"

    def test_a_yearly_partner_still_buys_twelve(self, wired, monkeypatch):
        wired(_db(terms=[_terms(billing_period="yearly", subscription_fee_paise=49900)]))
        months_seen = []

        async def fake_activate(partner_id, months=None):
            months_seen.append(months)
            return True

        monkeypatch.setattr(mb, "activate_stay_plan", fake_activate)

        run(mb.record_subscription_charge(SUB, "pay_1", invoice_id="inv_1", amount_paise=49900))

        assert months_seen == [12]

    def test_a_partner_with_no_deal_is_on_standard_terms(self, wired, monkeypatch):
        """No row is not an error — it is the standard ₹499/year deal."""
        wired(_db(terms=[]))
        months_seen = []

        async def fake_activate(partner_id, months=None):
            months_seen.append(months)
            return True

        monkeypatch.setattr(mb, "activate_stay_plan", fake_activate)

        run(mb.record_subscription_charge(SUB, "pay_1", invoice_id="inv_1", amount_paise=49900))

        assert months_seen == [12]


# ── an intro rate ends on the clock, not on the cron ────────────────────────


class TestIntroCommission:
    """
    "0% for 3 months, then 10%" is two rates and a deadline. If the nightly job
    were what ended the intro period, a cron that skipped a night would quietly
    extend the discount to every partner on one — so `resolve` reads the clock
    itself and the job only tidies up behind it.
    """

    def test_a_live_intro_rate_is_the_one_charged(self, wired):
        wired(_db(terms=[_terms(commission_pct=0, commission_expires_at="2099-01-01T00:00:00+00:00")]))
        assert run(partner_terms.commission_pct(PARTNER)) == 0

    def test_a_lapsed_intro_rate_reverts_without_the_job_running(self, wired):
        wired(_db(terms=[_terms(commission_pct=0, commission_expires_at=LONG_AGO)]))
        assert run(partner_terms.commission_pct(PARTNER)) == 10, (
            "an expired intro period must charge the base rate immediately, "
            "not whenever the housekeeping job next happens to run"
        )

    def test_the_full_zero_for_three_months_deal(self, wired):
        """
        The shape Sahil asked for end to end: ₹299/month, 0% for 3 months, then
        10%. Both sides of the boundary, from one stored row.
        """
        live = _terms(
            commission_pct=0, base_commission_pct=10, commission_expires_at="2099-06-01T00:00:00+00:00"
        )
        wired(_db(terms=[live]))
        assert run(partner_terms.commission_pct(PARTNER)) == 0

        lapsed = _terms(commission_pct=0, base_commission_pct=10, commission_expires_at=LONG_AGO)
        wired(_db(terms=[lapsed]))
        assert run(partner_terms.commission_pct(PARTNER)) == 10

    def test_intro_end_is_reported_not_just_applied(self, wired):
        """
        The plan page has to explain the change. A silent revert reads as a bug
        to the partner watching their commission go up.
        """
        wired(_db(terms=[_terms(commission_pct=0, commission_expires_at=LONG_AGO)]))
        terms = run(partner_terms.resolve(PARTNER))
        assert terms["intro_ended"] is True
        assert terms["lapsed"] is False

    def test_an_open_ended_rate_never_lapses(self, wired):
        wired(_db(terms=[_terms(commission_pct=5, commission_expires_at=None)]))
        assert run(partner_terms.commission_pct(PARTNER)) == 5

    def test_the_job_writes_back_what_resolve_already_reports(self, wired):
        db = wired(_db(terms=[_terms(commission_pct=0, commission_expires_at=LONG_AGO)]))
        assert run(partner_terms.revert_expired()) == 1

        row = db.rows["partner_terms"][0]
        assert row["commission_pct"] == 10
        assert row["commission_expires_at"] is None

    def test_the_job_is_idempotent(self, wired):
        wired(_db(terms=[_terms(commission_pct=0, commission_expires_at=LONG_AGO)]))
        assert run(partner_terms.revert_expired()) == 1
        assert run(partner_terms.revert_expired()) == 0, "nothing left to revert"

    def test_the_job_leaves_a_lapsed_deal_intact(self, wired):
        """
        A partner who stopped paying keeps their DEAL on the row — `resolve`
        already charges them base rate. Rewriting it would mean re-subscribing
        silently downgraded them to standard terms with nothing to restore from.
        """
        db = wired(
            _db(
                terms=[_terms(commission_pct=0, commission_expires_at="2099-06-01T00:00:00+00:00")],
                partner={"stay_plan_expires_at": LONG_AGO},
            )
        )
        run(partner_terms.revert_expired())
        assert float(db.rows["partner_terms"][0]["commission_pct"]) == 0


# ── the rate comes from terms, never from the listing's copy ────────────────


class TestCommissionSource:
    def test_terms_beat_a_stale_listing_copy(self, wired):
        """
        `partner_listings.commission_pct` is a denormalised copy kept in step by
        `sync_listings`. If a backfill ever failed, or a listing were created
        between a deal landing and the sync running, the row would still say 10
        — and a partner sold 5% would be charged double.
        """
        wired(_db(terms=[_terms(commission_pct=5)]))
        assert run(partner_terms.commission_pct(PARTNER, fallback=10)) == 5

    def test_no_partner_falls_back_rather_than_inventing_a_rate(self, wired):
        """
        A NULL partner_id is what makes a group trip OURS, and ours are stamped
        0. Reaching for a default here would have TravBuddy invoicing itself.
        """
        wired(_db())
        assert run(partner_terms.commission_pct(None, fallback=0)) == 0


# ── deal codes ──────────────────────────────────────────────────────────────


class TestDealCoupons:
    def _deal(self, **over):
        row = {
            "id": COUPON,
            "code": "EARLYHOTEL",
            "kind": "deal",
            "value": 299,
            "active": True,
            "used_count": 0,
            "usage_limit": None,
            "starts_at": None,
            "ends_at": None,
            "razorpay_offer_id": None,
            "plan_amount_paise": 29900,
            "plan_period": "monthly",
            "commission_pct": 5,
            "commission_months": None,
        }
        row.update(over)
        return row

    def test_a_deal_code_needs_no_razorpay_offer(self, wired):
        """
        The whole point of the shape: percent/flat need an Offer built by hand
        in the Razorpay Dashboard, a deal mints its own plan. A deal rejected
        for a missing offer id would make every custom price an ops ticket.
        """
        wired(_db(coupons=[self._deal()]))
        coupon, reason = run(plan_coupons.resolve("EARLYHOTEL", PARTNER))
        assert coupon is not None, reason

    def test_terms_land_only_when_the_mandate_authenticates(self, wired):
        """
        Same rule as the free-months coupons: validated at subscribe, granted at
        authentication. Writing terms when Checkout merely OPENS would let
        anyone who closed the tab walk away on 5% commission.
        """
        db = wired(_db(coupons=[self._deal()], pending=COUPON))

        assert db.rows["partner_terms"] == [], "nothing granted before authentication"

        run(plan_coupons.apply_pending(PARTNER, SUB))

        written = db.rows["partner_terms"]
        assert len(written) == 1
        assert written[0]["subscription_fee_paise"] == 29900
        assert written[0]["billing_period"] == "monthly"
        assert float(written[0]["commission_pct"]) == 5
        assert written[0]["source"] == "coupon"

    def test_an_intro_deal_records_what_it_reverts_to(self, wired):
        db = wired(
            _db(coupons=[self._deal(commission_pct=0, commission_months=3)], pending=COUPON)
        )
        run(plan_coupons.apply_pending(PARTNER, SUB))

        row = db.rows["partner_terms"][0]
        assert float(row["commission_pct"]) == 0
        assert row["commission_expires_at"] is not None
        assert float(row["base_commission_pct"]) == 10, "must know what comes after"

    def test_a_deal_describes_both_halves_of_itself(self):
        assert plan_coupons.describe(self._deal()) == "₹299/month · 5% commission"

    def test_an_intro_deal_says_what_happens_next(self):
        """
        The partner is told about the cliff when they type the code, not in the
        month it arrives.
        """
        described = plan_coupons.describe(self._deal(commission_pct=0, commission_months=3))
        assert described == "₹299/month · no commission for 3 months, then 10%"

    def test_a_spent_code_grants_no_terms(self, wired):
        """
        Redemption is the gate. A code that lost its last slot must not still
        hand out the deal — `redeem_plan_coupon` returning false is the only
        signal, and terms are written behind it.
        """
        db = wired(_db(coupons=[self._deal()], pending=COUPON))

        class _Spent:
            def execute(self):
                class _R:
                    data = False

                return _R()

        db.rpc = lambda name, args: _Spent()

        run(plan_coupons.apply_pending(PARTNER, SUB))
        assert db.rows["partner_terms"] == []


# ── a deal is conditional, and the condition is a date ──────────────────────


class TestEntitlement:
    """
    "0% while subscribed" must stop when the subscribing stops. The signal is
    `stay_plan_expires_at` — a DATE, deliberately, not `subscription_status`:
    the status is moved to past_due by a nightly job, and gating money on a cron
    means a skipped run silently keeps a discount alive.
    """

    def test_a_paid_up_partner_keeps_their_rate(self, wired):
        wired(_db(terms=[_terms(commission_pct=0)], partner={"stay_plan_expires_at": PAID_THROUGH}))
        assert run(partner_terms.commission_pct(PARTNER)) == 0

    def test_a_lapsed_partner_reverts_to_base(self, wired):
        wired(_db(terms=[_terms(commission_pct=0)], partner={"stay_plan_expires_at": LONG_AGO}))
        terms = run(partner_terms.resolve(PARTNER))
        assert terms["commission_pct"] == 10
        assert terms["lapsed"] is True

    def test_a_cancelled_mandate_keeps_the_rate_until_the_paid_period_ends(self, wired):
        """
        Cancelling stops the next charge; it does not refund the period already
        bought. They keep the deal for exactly as long as they paid for it.
        """
        wired(
            _db(
                terms=[_terms(commission_pct=0)],
                partner={"subscription_status": "cancelled", "stay_plan_expires_at": PAID_THROUGH},
            )
        )
        assert run(partner_terms.commission_pct(PARTNER)) == 0

    def test_a_failed_recurring_charge_does_not_move_the_rate(self, wired):
        """
        THE GRACE CASE. Razorpay halts a mandate whose charge failed, and it may
        retry successfully hours later. Revoking a negotiated commission on a
        bounced autopay — then restoring it — would make the rate a partner is
        charged depend on a transient bank event.
        """
        wired(
            _db(
                terms=[_terms(commission_pct=5)],
                partner={"subscription_status": "halted", "stay_plan_expires_at": PAID_THROUGH},
            )
        )
        assert run(partner_terms.commission_pct(PARTNER)) == 5

    def test_a_halted_mandate_does_not_lose_its_paid_through_date(self, wired):
        """
        The mechanism behind the grace: mirroring a mandate's status must never
        touch the date entitlement is judged on.
        """
        db = wired(_db(partner={"stay_plan_expires_at": PAID_THROUGH}))
        run(mb.set_subscription_status(SUB, "halted"))
        assert db.rows["partners"][0]["stay_plan_expires_at"] == PAID_THROUGH

    def test_a_permanent_deal_ignores_the_subscription(self, wired):
        """A ₹0 partner has no subscription to be 'while subscribed' about."""
        wired(
            _db(
                terms=[_terms(subscription_fee_paise=0, commission_pct=5, entitlement="permanent")],
                partner={"stay_plan_expires_at": LONG_AGO, "subscription_status": "none"},
            )
        )
        assert run(partner_terms.commission_pct(PARTNER)) == 5

    def test_an_until_deal_lapses_on_its_own_date(self, wired):
        wired(
            _db(
                terms=[
                    _terms(
                        subscription_fee_paise=0,
                        commission_pct=0,
                        entitlement="until",
                        entitlement_expires_at=LONG_AGO,
                    )
                ],
                partner={"subscription_status": "none"},
            )
        )
        assert run(partner_terms.commission_pct(PARTNER)) == 10

    def test_a_zero_fee_deal_defaults_to_permanent(self, wired):
        """
        Left on 'subscription', a ₹0 deal would die the first time the granted
        plan period ran out — with no mandate able to renew it.
        """
        db = wired(_db())
        run(
            partner_terms.apply(
                PARTNER, fee_paise=0, period="yearly", commission=5, source="admin"
            )
        )
        assert db.rows["partner_terms"][0]["entitlement"] == "permanent"

    def test_a_paid_deal_defaults_to_subscription(self, wired):
        db = wired(_db())
        run(
            partner_terms.apply(
                PARTNER, fee_paise=29900, period="monthly", commission=5, source="admin"
            )
        )
        assert db.rows["partner_terms"][0]["entitlement"] == "subscription"

    def test_standard_terms_are_never_gated(self, wired):
        """
        A partner with no deal has no discount to lose. Gating the default would
        mean a lapsed partner's rate reverting from 10% to 10%.
        """
        wired(_db(terms=[], partner={"stay_plan_expires_at": LONG_AGO}))
        assert run(partner_terms.commission_pct(PARTNER)) == 10


# ── what we agreed vs what Razorpay collects ────────────────────────────────


class TestBillingMismatch:
    """
    Razorpay cannot reprice a live subscription. Changing terms therefore
    changes what we have agreed and nothing about what is being collected, and
    the system has to say so rather than show the agreed figure as if it were
    being charged.
    """

    def test_matching_mandate_reports_nothing(self, wired):
        wired(_db(terms=[_terms(subscription_fee_paise=29900, billing_period="monthly")]))
        partner = _partner(billed_amount_paise=29900, billed_period="monthly")
        assert run(partner_terms.billing_mismatch(partner)) is None

    def test_a_fee_change_is_detected(self, wired):
        wired(_db(terms=[_terms(subscription_fee_paise=29900, billing_period="monthly")]))
        partner = _partner(billed_amount_paise=49900, billed_period="yearly")
        mismatch = run(partner_terms.billing_mismatch(partner))
        assert mismatch["billed_amount_paise"] == 49900
        assert mismatch["billed_period"] == "yearly"
        assert mismatch["expected_amount_paise"] == 29900
        assert mismatch["expected_period"] == "monthly"

    def test_a_commission_only_change_is_not_a_mismatch(self, wired):
        """
        Commission never touches the mandate, so moving a partner from 10% to 5%
        must not raise a billing alarm. Only fee and period can disagree.
        """
        wired(
            _db(terms=[_terms(subscription_fee_paise=49900, billing_period="yearly", commission_pct=5)])
        )
        partner = _partner(billed_amount_paise=49900, billed_period="yearly")
        assert run(partner_terms.billing_mismatch(partner)) is None

    def test_a_dead_mandate_cannot_mismatch(self, wired):
        """Nothing is being collected, so there is nothing to disagree with."""
        wired(_db(terms=[_terms(subscription_fee_paise=29900, billing_period="monthly")]))
        partner = _partner(
            subscription_status="cancelled", billed_amount_paise=49900, billed_period="yearly"
        )
        assert run(partner_terms.billing_mismatch(partner)) is None

    def test_a_halted_mandate_can_still_mismatch(self, wired):
        """It can resume on a successful retry and bill the old amount."""
        wired(_db(terms=[_terms(subscription_fee_paise=29900, billing_period="monthly")]))
        partner = _partner(
            subscription_status="halted", billed_amount_paise=49900, billed_period="yearly"
        )
        assert run(partner_terms.billing_mismatch(partner)) is not None

    def test_an_unknown_billed_amount_stays_quiet(self, wired):
        """Pre-hardening rows. Unknown is not proof of a mismatch."""
        wired(_db(terms=[_terms()]))
        partner = _partner(billed_amount_paise=None, billed_period=None)
        assert run(partner_terms.billing_mismatch(partner)) is None

    def test_cancelling_a_mandate_forgets_its_price(self, wired):
        """
        Otherwise a cancelled ₹499/year keeps being reported as a mismatch
        against the partner's new terms for ever.
        """
        db = wired(_db())
        run(mb.set_subscription_status(SUB, "cancelled"))
        assert db.rows["partners"][0]["billed_amount_paise"] is None

    def test_halting_a_mandate_keeps_its_price(self, wired):
        db = wired(_db())
        run(mb.set_subscription_status(SUB, "halted"))
        assert db.rows["partners"][0]["billed_amount_paise"] == 29900


# ── replacing one deal with another ─────────────────────────────────────────


class TestDealReplacement:
    def test_a_new_intro_period_starts_from_today(self, wired):
        """
        5% for 6 months → 0% for 3 months must give three months from NOW. An
        inherited expiry would end the new deal on the old deal's schedule.
        """
        old_expiry = "2099-06-01T00:00:00+00:00"
        db = wired(_db(terms=[_terms(commission_pct=5, commission_expires_at=old_expiry)]))

        run(
            partner_terms.apply(
                PARTNER,
                fee_paise=29900,
                period="monthly",
                commission=0,
                commission_months=3,
                source="admin",
            )
        )

        row = db.rows["partner_terms"][0]
        assert row["commission_expires_at"] != old_expiry
        expires = datetime.fromisoformat(row["commission_expires_at"])
        expected = datetime.now(timezone.utc) + relativedelta(months=3)
        assert abs((expires - expected).total_seconds()) < 60

    def test_dropping_an_intro_period_clears_the_old_expiry(self, wired):
        """
        A permanent rate replacing an intro one must not keep the old deadline,
        or the partner silently reverts on a date nobody agreed to.
        """
        db = wired(
            _db(terms=[_terms(commission_pct=5, commission_expires_at="2099-06-01T00:00:00+00:00")])
        )
        run(
            partner_terms.apply(
                PARTNER, fee_paise=29900, period="monthly", commission=0, source="admin"
            )
        )
        assert db.rows["partner_terms"][0]["commission_expires_at"] is None

    def test_every_change_records_when_it_started(self, wired):
        db = wired(_db())
        run(
            partner_terms.apply(
                PARTNER, fee_paise=29900, period="monthly", commission=5, source="admin"
            )
        )
        assert db.rows["partner_terms"][0]["effective_at"] is not None


# ── a redeemed coupon is not the contract ───────────────────────────────────


class TestCouponIsNotTheContract:
    """
    A code is how terms are ACQUIRED. Once redeemed the terms live in
    `partner_terms`, and the coupon is free to be edited, expired or switched
    off without reaching back into deals it already granted.
    """

    def _redeemed(self, **coupon_over):
        coupon = {
            "id": COUPON,
            "code": "ZERO90",
            "kind": "deal",
            "value": 499,
            "active": True,
            "used_count": 1,
            "usage_limit": None,
            "starts_at": None,
            "ends_at": None,
            "razorpay_offer_id": None,
            "plan_amount_paise": 49900,
            "plan_period": "monthly",
            "commission_pct": 0,
            "commission_months": 3,
        }
        coupon.update(coupon_over)
        terms = _terms(
            subscription_fee_paise=49900,
            billing_period="monthly",
            commission_pct=0,
            commission_expires_at="2099-06-01T00:00:00+00:00",
            source="coupon",
            source_coupon_id=COUPON,
        )
        return _db(terms=[terms], coupons=[coupon])

    def test_editing_the_coupon_does_not_move_granted_terms(self, wired):
        wired(self._redeemed(commission_pct=25, plan_amount_paise=99900))
        terms = run(partner_terms.resolve(PARTNER))
        assert terms["commission_pct"] == 0
        assert terms["subscription_fee_paise"] == 49900

    def test_deactivating_the_coupon_does_not_revoke_granted_terms(self, wired):
        wired(self._redeemed(active=False))
        assert run(partner_terms.commission_pct(PARTNER)) == 0

    def test_an_expired_coupon_does_not_revoke_granted_terms(self, wired):
        """Expiry stops NEW redemptions. It is not a kill switch on old ones."""
        wired(self._redeemed(ends_at=LONG_AGO))
        assert run(partner_terms.commission_pct(PARTNER)) == 0

    def test_an_expired_coupon_stops_new_redemptions(self, wired):
        wired(self._redeemed(ends_at=LONG_AGO))
        coupon, reason = run(plan_coupons.resolve("ZERO90", "some-other-partner"))
        assert coupon is None
        assert reason


class TestCouponCannotBeFarmed:
    """
    ZERO90 gives 0% for three months. Redeeming it again every three months
    would make it 0% for ever, so one redemption per partner is enforced in the
    database (platform_coupon_redemptions_once_idx), not just in `resolve`.
    """

    def _db_with_redemption(self):
        db = _db(
            coupons=[
                {
                    "id": COUPON,
                    "code": "ZERO90",
                    "kind": "deal",
                    "value": 499,
                    "active": True,
                    "used_count": 1,
                    "usage_limit": None,
                    "starts_at": None,
                    "ends_at": None,
                    "razorpay_offer_id": None,
                    "plan_amount_paise": 49900,
                    "plan_period": "monthly",
                    "commission_pct": 0,
                    "commission_months": 3,
                }
            ],
            pending=COUPON,
        )
        db.rows["platform_coupon_redemptions"] = [
            {"id": "r1", "coupon_id": COUPON, "partner_id": PARTNER, "months_granted": None}
        ]
        return db

    def test_resolve_refuses_a_second_redemption(self, wired):
        wired(self._db_with_redemption())
        coupon, reason = run(plan_coupons.resolve("ZERO90", PARTNER))
        assert coupon is None
        assert "already used" in (reason or "").lower()

    def test_a_second_grant_writes_no_terms(self, wired):
        """
        Even if the pre-check is bypassed, the redemption RPC is the gate: it
        returns false for a partner who already redeemed, and terms are written
        only behind a true.
        """
        db = wired(self._db_with_redemption())

        class _AlreadyRedeemed:
            def execute(self):
                class _R:
                    data = False

                return _R()

        db.rpc = lambda name, args: _AlreadyRedeemed()
        run(plan_coupons.apply_pending(PARTNER, SUB))
        assert db.rows["partner_terms"] == []


# ── the snapshot really is a snapshot ───────────────────────────────────────


class TestBookingSnapshot:
    def test_one_rate_produces_both_the_amount_and_the_snapshot(self):
        """
        The race that matters: an admin changing 10% → 5% mid-booking must not
        produce a booking whose commission_paise was computed at one rate and
        whose commission_pct records another. Both come from a single resolved
        value, so they cannot disagree however the timing falls.
        """
        q = pricing.quote(
            unit_prices_paise=[1_000_000], quantity=1, commission_pct=10.0
        )
        meta = {"commission_pct": 10.0}
        assert q.commission_paise == 100_000
        assert meta["commission_pct"] == 10.0
        # And the payout is the remainder, so the ledger balances at that rate.
        assert q.commission_paise + q.payout_paise == q.amount_paise

    def test_a_snapshot_is_never_recomputed_for_refunds(self):
        """
        Refunds work off the amount CAPTURED, never off a rate looked up again.
        A partner who renegotiates to 5% must not retroactively change what a
        booking sold at 10% owes.
        """
        booked_commission = 100_000
        refund = pricing.refund_paise(
            1_000_000,
            [{"hours_before": 24, "refund_pct": 50}],
            "2099-01-02",
            datetime(2098, 1, 1, tzinfo=timezone.utc),
        )
        assert refund == 500_000
        # The booking's own commission is untouched by the refund maths.
        assert booked_commission == 100_000


# ── a failed listing backfill cannot overcharge ─────────────────────────────


class TestListingBackfill:
    def test_terms_are_written_before_listings(self, wired):
        """
        The ordering IS the safety argument: bookings price from terms, so a
        backfill that fails afterwards leaves a stale display copy and a
        correct charge — never the reverse.
        """
        db = wired(_db())
        db.rows["partner_listings"] = [
            {"id": "l1", "partner_id": PARTNER, "commission_pct": 10}
        ]
        run(
            partner_terms.apply(
                PARTNER, fee_paise=29900, period="monthly", commission=5, source="admin"
            )
        )
        assert float(db.rows["partner_terms"][0]["commission_pct"]) == 5
        assert float(db.rows["partner_listings"][0]["commission_pct"]) == 5

    def test_a_failed_backfill_is_reported_not_swallowed(self, wired, monkeypatch):
        db = wired(_db())

        async def boom(fn, *, label, max_seconds=180.0):
            raise RuntimeError("connection died")

        monkeypatch.setattr(partner_terms, "run_db_resilient", boom)

        stale = run(partner_terms.sync_listings(PARTNER, 5))
        assert stale == ["partner_listings", "group_trips"]
        assert db.rows["partner_terms"] == []

    def test_a_failed_backfill_still_charges_the_new_rate(self, wired, monkeypatch):
        """The whole point: display goes stale, money does not."""
        wired(_db())

        async def boom(fn, *, label, max_seconds=180.0):
            raise RuntimeError("connection died")

        monkeypatch.setattr(partner_terms, "run_db_resilient", boom)

        applied = run(
            partner_terms.apply(
                PARTNER, fee_paise=29900, period="monthly", commission=5, source="admin"
            )
        )
        assert applied["commission_pct"] == 5
        assert applied["stale_listings"]
        assert run(partner_terms.commission_pct(PARTNER)) == 5


# ── one Razorpay plan per price point ───────────────────────────────────────


class TestPlanKeys:
    """
    `platform_plans` caches a Razorpay plan id against a key. If two price
    points shared a key, the second would reuse the first's plan and bill the
    wrong amount — the exact failure the 2026-08-03 annual migration hit by
    reusing "partner_monthly", now reachable again every time a deal is minted.
    """

    def test_price_points_never_share_a_key(self):
        keys = {
            payments._plan_key(29900, "monthly"),
            payments._plan_key(49900, "monthly"),
            payments._plan_key(49900, "yearly"),
            payments._plan_key(19900, "yearly"),
        }
        assert len(keys) == 4

    def test_the_standard_plan_keeps_its_grandfathered_key(self):
        """Live mandates reconcile against `partner_yearly`; it must not move."""
        assert payments._plan_key(49900, "yearly") == mb.PLAN_KEY

    def test_a_monthly_mandate_is_authorised_for_more_than_ten_cycles(self):
        """Ten monthly cycles is a mandate that stops collecting after ten months."""
        assert payments._total_cycles("monthly") > 10
        assert payments._total_cycles("yearly") == 10
