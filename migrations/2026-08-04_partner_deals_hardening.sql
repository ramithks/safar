-- Partner deals, hardening pass. Follows 2026-08-04_partner_deals.sql.
--
-- Three concrete correctness holes, none of which change the architecture:
--
-- 1. ENTITLEMENT. `partner_terms` said what a partner's commission was but not
--    what it was CONDITIONAL ON. "₹499/month + 0% while subscribed" survived
--    cancellation forever, because a terms row outlives the subscription that
--    justified it. Now stated: `entitlement` says what keeps the deal alive.
--
-- 2. BILLING vs TERMS. An admin moving a partner from ₹499/year to ₹299/month
--    changed what we believed and nothing about what Razorpay collects — there
--    is no API to reprice a live mandate. The two are now recorded separately
--    (`partners.billed_*` is what the mandate actually charges) so the
--    disagreement is visible instead of assumed away.
--
-- 3. EFFECTIVE DATE. An intro period needs a start, not just an end, or
--    replacing "5% for 6 months" with "0% for 3 months" is impossible to audit
--    afterwards — you can see when it ends but not which deal set it.

begin;

-- ── what keeps a deal alive ─────────────────────────────────────────────────
--
--   subscription  The commission holds while their plan is paid up. The signal
--                 is `partners.stay_plan_expires_at`, which is a TIMESTAMP and
--                 not a status: a failed recurring charge marks the mandate
--                 halted but leaves the paid-through date alone, so a transient
--                 payment failure cannot revoke a rate. That is the grace
--                 behaviour, and it needs no job to be correct.
--
--   permanent     Never lapses. The only sane default for a ₹0 deal, which has
--                 no mandate to be "subscribed" to, and the right answer for
--                 "5% indefinitely, we agreed it on a call".
--
--   until         Lapses at `entitlement_expires_at`. A dated deal for a
--                 partner who is not paying us a subscription.

alter table partner_terms
  add column if not exists entitlement text not null default 'subscription'
    check (entitlement in ('subscription', 'permanent', 'until')),
  add column if not exists entitlement_expires_at timestamptz,
  -- When THIS deal started. Also the intro clock's origin, which is what makes
  -- replacing one intro deal with another unambiguous.
  add column if not exists effective_at timestamptz not null default now();

-- An `until` entitlement with no date never lapses, which is `permanent` under
-- a name that promises otherwise.
alter table partner_terms drop constraint if exists partner_terms_entitlement_check;
alter table partner_terms add constraint partner_terms_entitlement_check
  check (entitlement <> 'until' or entitlement_expires_at is not null);

comment on column partner_terms.entitlement is
  'What keeps this deal alive: subscription (while their plan is paid up), '
  'permanent (never lapses — the default for ₹0 deals), until (a fixed date).';

-- ── what Razorpay is actually collecting ────────────────────────────────────
--
-- Deliberately on `partners`, beside `subscription_id`: this describes the
-- MANDATE, not the deal. Razorpay has no API to reprice a subscription in
-- place, so changing terms cannot change what is being charged — the mandate
-- has to be cancelled and reopened. Recording the billed figures is what lets
-- the dashboard say "commercial terms ₹299/month, active billing ₹499/year"
-- instead of quietly showing the number nobody is being charged.

alter table partners
  add column if not exists billed_amount_paise integer
    check (billed_amount_paise is null or billed_amount_paise >= 0),
  add column if not exists billed_period text
    check (billed_period is null or billed_period in ('monthly', 'yearly'));

comment on column partners.billed_amount_paise is
  'What the LIVE Razorpay mandate charges. Compare against partner_terms to '
  'detect a mandate that no longer matches the agreed deal. NULL = no mandate.';

-- Backfill from what they were last actually charged, where we know it. The
-- ledger is the honest source: `platform_plans` only says what the plan costs
-- today, and a partner on the pre-2026-08-03 monthly plan was charged something
-- else. Falls back to the standard yearly price for a mandate with no ledger
-- row yet (authorised but not charged).
update partners p
   set billed_amount_paise = coalesce(
         (select pp.amount_paise
            from partner_payments pp
           where pp.razorpay_subscription_id = p.subscription_id
             and pp.kind = 'stay_subscription'
             and pp.status = 'paid'
             and pp.amount_paise > 0
           order by pp.created_at desc
           limit 1),
         49900
       ),
       billed_period = 'yearly'
 where p.subscription_id is not null
   and p.billed_amount_paise is null
   and p.subscription_status in ('active', 'authenticated', 'pending');

commit;
