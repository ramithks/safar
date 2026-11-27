-- A hotel decides what a late checkout costs.
--
-- `properties.check_in_time` and `check_out_time` are two times, and a guest
-- asking "main 10 baje check-in kar sakta hoon?" is asking a question those two
-- columns cannot answer. Today the AI either guesses or hands the conversation
-- to a person; both are wrong, and the second one is wrong at 2am when there is
-- no person.
--
-- A band is (kind, until_time) → what it costs. They are read in time order and
-- the first band the requested time still falls inside wins:
--
--     early_in   08:00 → 50% of a night
--     early_in   10:00 → ₹500
--     early_in   14:00 → free          (the standard time; always implicit)
--
--     late_out   14:00 → free
--     late_out   17:00 → ₹500
--     late_out   22:00 → 50% of a night
--
-- Hotel-level rather than per room type, because that is how hotels price it and
-- because a per-room ladder is six times the configuration for a distinction no
-- hotelier in the pilot draws. If one ever does, the scope column is the place
-- it goes.

create table if not exists public.stay_time_rules (
  id           uuid primary key default gen_random_uuid(),
  property_id  uuid not null references public.properties(id) on delete cascade,
  partner_id   uuid not null references public.partners(id) on delete cascade,

  -- early_in: arriving before check_in_time. late_out: leaving after check_out_time.
  kind         text not null check (kind in ('early_in','late_out')),

  -- The end of this band. For early_in, arriving at or before this time is
  -- charged at this band's rate. For late_out, leaving at or before it is.
  until_time   time not null,

  charge_kind  text not null check (charge_kind in ('free','fixed','pct_of_night','unavailable')),
  -- Paise for 'fixed'; a whole percentage for 'pct_of_night'; ignored otherwise.
  amount       integer not null default 0 check (amount >= 0),

  -- "Subject to availability" is the honest answer most hotels give, and an
  -- agent that says it unprompted is right more often than one that promises.
  subject_to_availability boolean not null default true,
  note         text,

  is_active    boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  -- One band per time per kind per hotel. A second row for 10:00 would make the
  -- answer depend on insertion order, which is the one thing a policy may not do.
  unique (property_id, kind, until_time)
);

create index if not exists stay_time_rules_property_idx
  on public.stay_time_rules (property_id, kind, until_time);

alter table public.stay_time_rules enable row level security;

do $$ begin
  drop policy if exists stay_time_rules_read on public.stay_time_rules;
create policy stay_time_rules_read on public.stay_time_rules
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;

do $$ begin
  drop policy if exists stay_time_rules_write on public.stay_time_rules;
create policy stay_time_rules_write on public.stay_time_rules
    for all using (has_partner_perm(partner_id, 'rates'))
    with check (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

comment on table public.stay_time_rules is
  'Priced bands for arriving early or leaving late. Read in time order; the '
  'first band the requested time falls inside wins. The property''s own '
  'check_in_time / check_out_time are the free boundary and are never stored here.';
