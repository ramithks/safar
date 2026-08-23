-- A room has a floor, and not just a ceiling.
--
-- Negotiation today is one number per hotel: `settings.max_discount_pct`, a
-- percentage the AI's discount request is clamped to inside `price_stay()`.
-- That is a *ceiling on generosity* and it works, but it cannot express the
-- three things a hotel selling a room actually thinks in:
--
--     public   ₹5,000     what the guest is shown
--     target   ₹4,700     where the hotel would like to land
--     floor    ₹4,300     below which it would rather lose the booking
--
-- and it is one number for a hotel whose suite and whose dorm bed have nothing
-- in common commercially.
--
-- **Scope, most specific wins.** rate_plan → room_type → property. A hotel that
-- sets nothing at all keeps exactly the behaviour it has today, because there is
-- deliberately **no backfill**: where no row exists at any scope, the resolver
-- falls back to `properties.settings.max_discount_pct` as it always has. Copying
-- that number into a row here would have created a second home for it, and the
-- day a hotelier edited one of the two, quotes and the dashboard would start
-- disagreeing about the same hotel.
--
-- **A floor may be a percentage or a price, and the price wins.** A percentage
-- floor moves with the calendar, which is what a hotel wants over a season. An
-- absolute floor does not, which is what a hotel wants on a specific room it
-- refuses to sell below cost. Where both are set the harder of the two is the
-- one applied — see services/pms/negotiation.py, which resolves it once.

create table if not exists public.negotiation_rules (
  id            uuid primary key default gen_random_uuid(),
  property_id   uuid not null references public.properties(id) on delete cascade,
  partner_id    uuid not null references public.partners(id) on delete cascade,

  scope         text not null check (scope in ('property','room_type','rate_plan')),
  room_type_id  uuid references public.room_types(id) on delete cascade,
  rate_plan_id  uuid references public.rate_plans(id) on delete cascade,

  enabled       boolean not null default true,

  -- The floor. Below this the answer is no, whoever is asking.
  floor_pct     integer check (floor_pct is null or floor_pct between 0 and 100),
  floor_paise   integer check (floor_paise is null or floor_paise >= 0),

  -- Where the hotel would like to land. Never shown to a guest.
  target_pct    integer check (target_pct is null or target_pct between 0 and 100),
  target_paise  integer check (target_paise is null or target_paise >= 0),

  -- The most a rate may ever be raised to, for the seasonal/surge case.
  ceiling_paise integer check (ceiling_paise is null or ceiling_paise >= 0),

  -- Up to auto_pct the agent commits on its own. Between auto_pct and
  -- approval_pct it may only *propose*, and a human approves. Above
  -- approval_pct nobody may go, approval or not.
  auto_pct      integer check (auto_pct is null or auto_pct between 0 and 100),
  approval_pct  integer check (approval_pct is null or approval_pct between 0 and 100),

  -- When negotiating is open at all.
  valid_from    date,
  valid_to      date,
  -- ISO weekday numbers, 1 = Monday … 7 = Sunday. Empty means every day.
  days_of_week  smallint[] not null default '{}',
  blackout_dates date[] not null default '{}',

  -- Negotiation only opens above these. "One night, one room, discount please"
  -- is not a negotiation a hotel has to entertain.
  min_nights    integer not null default 1 check (min_nights >= 1),
  min_units     integer not null default 1 check (min_units >= 1),

  note          text,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  -- A scope names exactly the thing it is scoped to, and nothing else.
  constraint negotiation_rules_scope_target check (
    (scope = 'property'  and room_type_id is null and rate_plan_id is null) or
    (scope = 'room_type' and room_type_id is not null and rate_plan_id is null) or
    (scope = 'rate_plan' and rate_plan_id is not null and room_type_id is null)
  ),
  -- An approval ceiling below the automatic floor is a contradiction: it would
  -- mean a manager may authorise less than the agent already may.
  constraint negotiation_rules_ladder check (
    auto_pct is null or approval_pct is null or approval_pct >= auto_pct
  )
);

-- One rule per scoped thing. Partial uniques rather than one composite, because
-- two of the three columns are null in every row and NULLs do not collide.
create unique index if not exists negotiation_rules_property_uniq
  on public.negotiation_rules (property_id) where scope = 'property';
create unique index if not exists negotiation_rules_room_type_uniq
  on public.negotiation_rules (room_type_id) where scope = 'room_type';
create unique index if not exists negotiation_rules_rate_plan_uniq
  on public.negotiation_rules (rate_plan_id) where scope = 'rate_plan';

create index if not exists negotiation_rules_lookup_idx
  on public.negotiation_rules (property_id, scope);

alter table public.negotiation_rules enable row level security;

-- Read is 'rates', not 'owns_partner'.
--
-- Everywhere else in this schema a member may read what their hotel holds. Not
-- here: the floor is the one number whose leak costs the hotel money on every
-- future negotiation, and a receptionist who knows it will eventually say it.
-- The agent reads through the service role and shows nobody.
do $$ begin
  create policy negotiation_rules_read on public.negotiation_rules
    for select using (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

do $$ begin
  create policy negotiation_rules_write on public.negotiation_rules
    for all using (has_partner_perm(partner_id, 'rates'))
    with check (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

comment on table public.negotiation_rules is
  'How far a price may move and who has to say so, per hotel, room type or rate '
  'plan. Most specific scope wins. No row anywhere means the property''s '
  'settings.max_discount_pct still applies, unchanged.';
comment on column public.negotiation_rules.floor_paise is
  'Absolute minimum nightly rate per unit. Never leaves the server: no API '
  'projection, no agent tool output and no prompt may contain this number.';
