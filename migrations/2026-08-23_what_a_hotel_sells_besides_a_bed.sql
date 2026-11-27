-- What a hotel sells besides a bed.
--
-- `services` had a name, a price and a category, which is enough for a
-- receptionist adding a line to a folio and not enough for anyone selling. It
-- could not say whether ₹1,500 for an airport pickup was per person or per
-- vehicle — the difference between ₹1,500 and ₹9,000 for a family of six — and
-- an AI quoting it had no way to know it was guessing.
--
-- Experiences are their own table rather than a service with extra columns. A
-- transfer and a sunrise trek differ in what has to be true before either can be
-- sold: a trek has a meeting point, a duration, a minimum number of people, an
-- age limit and its own cancellation terms, none of which a laundry service has.
-- One table carrying both would be a table where two thirds of the columns are
-- null in every row, and a booking flow full of "if this is really an experience".

alter table public.services
  add column if not exists price_unit text not null default 'fixed',
  add column if not exists availability text not null default 'available',
  add column if not exists opens_at time,
  add column if not exists closes_at time,
  add column if not exists max_quantity integer,
  add column if not exists available_from date,
  add column if not exists available_to date,
  add column if not exists photo_url text,
  add column if not exists terms text,
  add column if not exists advance_required boolean not null default false;

do $$ begin
  alter table public.services add constraint services_price_unit_check
    check (price_unit in ('per_person','per_room','per_vehicle','per_trip','per_night','per_item','fixed'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.services add constraint services_availability_check
    check (availability in ('available','on_request','not_available'));
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.services add constraint services_max_quantity_check
    check (max_quantity is null or max_quantity >= 1);
exception when duplicate_object then null; end $$;

create table if not exists public.experiences (
  id             uuid primary key default gen_random_uuid(),
  property_id    uuid not null references public.properties(id) on delete cascade,
  partner_id     uuid not null references public.partners(id) on delete cascade,

  title          text not null,
  description    text,
  category       text not null default 'other',

  duration_minutes integer check (duration_minutes is null or duration_minutes > 0),
  price_paise    integer not null default 0 check (price_paise >= 0),
  price_unit     text not null default 'per_person'
                 check (price_unit in ('per_person','per_group','per_vehicle','fixed')),

  meeting_point  text,
  starts_at      time,

  min_participants integer not null default 1 check (min_participants >= 1),
  max_participants integer check (max_participants is null or max_participants >= 1),

  min_age        integer check (min_age is null or min_age between 0 and 120),
  max_age        integer check (max_age is null or max_age between 0 and 120),
  requirements   text,

  -- Same three-value vocabulary as services and room amenities.
  availability   text not null default 'available'
                 check (availability in ('available','on_request','seasonal','not_available')),
  available_from date,
  available_to   date,
  days_of_week   smallint[] not null default '{}',

  -- Same shape as rate_plans.cancellation_policy; one resolver reads all three.
  cancellation   jsonb not null default '{}'::jsonb,

  photos         text[] not null default '{}',
  photo_meta     jsonb not null default '{}'::jsonb,

  lead_time_hours integer not null default 0 check (lead_time_hours >= 0),
  daily_capacity integer check (daily_capacity is null or daily_capacity >= 0),

  is_active      boolean not null default true,
  sort_order     integer not null default 0,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),

  constraint experiences_participants check (
    max_participants is null or max_participants >= min_participants
  ),
  constraint experiences_ages check (
    min_age is null or max_age is null or max_age >= min_age
  )
);

create index if not exists experiences_property_idx
  on public.experiences (property_id, sort_order) where is_active;

alter table public.experiences enable row level security;

do $$ begin
  drop policy if exists experiences_read on public.experiences;
create policy experiences_read on public.experiences
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;
do $$ begin
  drop policy if exists experiences_write on public.experiences;
create policy experiences_write on public.experiences
    for all using (has_partner_perm(partner_id, 'rates'))
    with check (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

comment on table public.experiences is
  'What a guest can do, as opposed to what they can order. Separate from '
  'services because an experience has a meeting point, a duration, a party size '
  'and an age limit, and a laundry service has none of those.';
comment on column public.services.price_unit is
  'per_person | per_room | per_vehicle | per_trip | per_night | per_item | fixed. '
  'The difference between quoting a family ₹1,500 and ₹9,000 for the same transfer.';
