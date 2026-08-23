-- Three rooms is a different price from one.
--
-- `price_stay()` multiplies: four rooms cost four times one room, always. Every
-- hotel in the pilot prices a group differently and has no way to say so, so the
-- answer to "we are twenty people, what's your best price" is currently a human
-- or a guess.
--
-- Two tables, because they answer two different questions:
--
--   group_rate_tiers   how much comes off, by how many rooms
--   group_policies     what else changes when a booking is a group —
--                      deposit, deadline, cancellation, blackout
--
-- A tier may name a discount OR a fixed nightly rate. Fixed wins where both are
-- set. `negotiable` is what makes "10+ rooms → let's talk" expressible without
-- inventing a null-means-negotiable convention nobody would remember.

create table if not exists public.group_rate_tiers (
  id              uuid primary key default gen_random_uuid(),
  property_id     uuid not null references public.properties(id) on delete cascade,
  partner_id      uuid not null references public.partners(id) on delete cascade,

  -- Null means every room type. A hotel that groups only its cheapest rooms
  -- names it; most do not.
  room_type_id    uuid references public.room_types(id) on delete cascade,

  min_units       integer not null check (min_units >= 1),
  -- Null means "and above". Exactly one open-ended tier is expected per set.
  max_units       integer check (max_units is null or max_units >= 1),

  discount_pct    integer check (discount_pct is null or discount_pct between 0 and 100),
  fixed_price_paise integer check (fixed_price_paise is null or fixed_price_paise >= 0),

  -- The tier where the answer is "let me check with the hotel" rather than a
  -- number. The agent may open a negotiation here; it may not invent a rate.
  negotiable      boolean not null default false,

  is_active       boolean not null default true,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  constraint group_rate_tiers_range check (max_units is null or max_units >= min_units),
  -- A tier that says nothing is a tier that changes nothing, which is the same
  -- as not existing and reads to a hotelier as a bug.
  constraint group_rate_tiers_says_something check (
    discount_pct is not null or fixed_price_paise is not null or negotiable
  )
);

create index if not exists group_rate_tiers_lookup_idx
  on public.group_rate_tiers (property_id, min_units);

create table if not exists public.group_policies (
  property_id     uuid primary key references public.properties(id) on delete cascade,
  partner_id      uuid not null references public.partners(id) on delete cascade,

  -- Below this a booking is not a group and none of this applies.
  min_units       integer not null default 3 check (min_units >= 1),

  deposit_pct     integer check (deposit_pct is null or deposit_pct between 0 and 100),
  -- Days before check-in by which the balance is due.
  payment_due_days integer check (payment_due_days is null or payment_due_days >= 0),

  -- Structured, in the same shape as rate_plans.cancellation_policy, so one
  -- resolver reads both. See services/pms/policies.py.
  cancellation    jsonb not null default '{}'::jsonb,

  meal_package    text,
  blackout_dates  date[] not null default '{}',
  terms           text,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

alter table public.group_rate_tiers enable row level security;
alter table public.group_policies enable row level security;

do $$ begin
  create policy group_rate_tiers_read on public.group_rate_tiers
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  create policy group_rate_tiers_write on public.group_rate_tiers
    for all using (has_partner_perm(partner_id, 'rates'))
    with check (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

do $$ begin
  create policy group_policies_read on public.group_policies
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  create policy group_policies_write on public.group_policies
    for all using (has_partner_perm(partner_id, 'rates'))
    with check (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

comment on table public.group_rate_tiers is
  'How much comes off, by how many rooms. Overlapping tiers resolve to the one '
  'with the highest min_units that the request satisfies — see services/pms/rates.py.';
