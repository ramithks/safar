-- Partner commercial terms — the deal a partner is actually on.
--
-- Until now there was exactly one deal: ₹499/year + 10% commission, hard-coded
-- in three places (COMMISSION_PCT in marketplace_pricing.py, KIND_RULES in the
-- website, PLAN_KEY/PLAN_PERIOD_MONTHS in marketplace_bookings.py). Sales needs
-- more than one: ₹299/month + 5%, ₹499/month + 0% for three months, ₹0 + 12%
-- for a partner who won't pay a subscription at all.
--
-- The shape is a chain, and the direction matters:
--
--     partners → partner_terms → listings → bookings.commission_pct
--
-- `partner_terms` is the SOURCE OF TRUTH. `partner_listings.commission_pct` is
-- a denormalised copy kept in step for display and for legacy read paths — it
-- is never the authority, because a partner who is sold 0% must not discover
-- that the hotel they listed last month is still quietly at 10%.
--
-- `bookings.commission_pct` is the opposite: a SNAPSHOT, frozen at creation and
-- never rewritten. A ₹10,000 stay booked at 10% owes ₹1,000 for the rest of
-- time, whatever the partner renegotiates tomorrow. The rupee amount was
-- already frozen in bookings.commission_paise; this column records the rate
-- that produced it, so a dispute two quarters from now can be reconstructed
-- rather than argued about.

begin;

-- ── the terms ───────────────────────────────────────────────────────────────

create table if not exists partner_terms (
  partner_id uuid primary key references partners(id) on delete cascade,

  -- What they pay us, and how often. Zero is legitimate and load-bearing: a
  -- commission-only deal has no subscription and therefore no Razorpay mandate
  -- at all, which the subscribe path has to handle rather than sending them to
  -- a Checkout for ₹0.
  subscription_fee_paise integer not null default 49900 check (subscription_fee_paise >= 0),
  billing_period text not null default 'yearly' check (billing_period in ('monthly', 'yearly')),

  -- What we take on bookings we process.
  commission_pct numeric not null default 10 check (commission_pct >= 0 and commission_pct <= 50),

  -- NULL = this rate holds for as long as the deal does. A timestamp makes it
  -- introductory: the nightly job reverts to `base_commission_pct` when it
  -- passes. Splitting "the rate now" from "the rate after" is what lets one
  -- code mean "0% for 3 months, then 10%" without a second coupon.
  commission_expires_at timestamptz,
  base_commission_pct numeric not null default 10 check (base_commission_pct >= 0 and base_commission_pct <= 50),

  -- Where this deal came from. `default` never appears as a row — a partner
  -- with no row IS the default — but the column keeps the enum honest for a
  -- row written back to standard terms.
  source text not null default 'default' check (source in ('default', 'coupon', 'admin')),
  source_coupon_id uuid references platform_coupons(id) on delete set null,

  -- The WhatsApp context. B2B deals are negotiated in a chat window and the
  -- reason for a 5% rate lives in someone's head otherwise.
  notes text,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  -- An intro period with nothing to revert to is a rate that silently becomes
  -- permanent. Either both or neither.
  constraint partner_terms_intro_check
    check (commission_expires_at is null or commission_pct <> base_commission_pct)
);

create index if not exists partner_terms_expiring_idx
  on partner_terms (commission_expires_at)
  where commission_expires_at is not null;

comment on table partner_terms is
  'The commercial deal a partner is on. Source of truth for commission; '
  'listings hold a denormalised copy, bookings hold an immutable snapshot.';

-- ── the snapshot ────────────────────────────────────────────────────────────

alter table bookings
  add column if not exists commission_pct numeric;

comment on column bookings.commission_pct is
  'The rate that produced commission_paise, frozen at creation. Never rewritten '
  'when a partner renegotiates — historical bookings keep the deal they were made under.';

-- Backfill what we can prove.
--
-- ⚠ THE ASSUMPTION, stated so nobody reuses it: every booking made before this
-- migration was priced off the listing's stamped rate, under a single global
-- 10% regime with no per-partner deals in existence. Under those conditions —
-- and ONLY those — the listing's current commission_pct IS the rate that
-- produced the booking's commission_paise.
--
-- That equivalence dies the moment this migration lands. From here on a
-- listing's rate is a mutable copy of the partner's CURRENT terms, while a
-- booking's is a frozen record of the terms it was SOLD under, and the two are
-- expected to disagree. Nothing downstream may infer one from the other: read
-- bookings.commission_pct for anything historical, and partner_terms for
-- anything current. This statement runs once and must never be generalised
-- into recurring logic.
update bookings b
   set commission_pct = coalesce(l.commission_pct, 10)
  from partner_listings l
 where b.item_id = l.id
   and b.commission_pct is null;

update bookings
   set commission_pct = 10
 where commission_pct is null;

-- ── coupons that carry a deal ───────────────────────────────────────────────
--
-- The existing kinds discount the ₹499 plan. `deal` REPLACES it: the partner is
-- put on a different plan entirely, at a price and period we choose, with a
-- commission rate attached. That also sidesteps the awkward part of percent/flat
-- — Razorpay Offers have no create API, so those codes need an offer built by
-- hand in the Dashboard first. A deal code needs nothing: we mint the Razorpay
-- plan ourselves on first use.

alter table platform_coupons
  add column if not exists plan_amount_paise integer check (plan_amount_paise is null or plan_amount_paise >= 0),
  add column if not exists plan_period text check (plan_period is null or plan_period in ('monthly', 'yearly')),
  add column if not exists commission_pct numeric check (commission_pct is null or (commission_pct >= 0 and commission_pct <= 50)),
  add column if not exists commission_months integer check (commission_months is null or (commission_months > 0 and commission_months <= 60));

alter table platform_coupons drop constraint if exists platform_coupons_kind_check;
alter table platform_coupons add constraint platform_coupons_kind_check
  check (kind in ('free_months', 'percent', 'flat', 'deal'));

-- A deal code with no price is not a deal. `value` stays >0 per the existing
-- column check, so a deal carries its monthly/yearly rupee figure there too and
-- plan_amount_paise is the authoritative paise version.
alter table platform_coupons drop constraint if exists platform_coupons_deal_check;
alter table platform_coupons add constraint platform_coupons_deal_check
  check (
    kind <> 'deal'
    or (plan_amount_paise is not null and plan_period is not null)
  );

-- Only percent/flat need a Dashboard offer. `deal` and `free_months` are ours
-- end to end; the old constraint would have rejected every deal code.
alter table platform_coupons drop constraint if exists platform_coupons_offer_required_check;
alter table platform_coupons add constraint platform_coupons_offer_required_check
  check (kind in ('free_months', 'deal') or razorpay_offer_id is not null);

-- A deal code's `value` is its rupee price, which blows past the free_months
-- ceiling of 24 — that check is scoped to free_months already, so nothing to do.

commit;
