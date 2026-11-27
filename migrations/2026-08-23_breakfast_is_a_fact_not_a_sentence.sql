-- Breakfast is a fact, not a sentence.
--
-- "Breakfast included hai?" is the second-most-asked question in the live
-- Instagram transcripts and StaySuite cannot answer it from structured data. What
-- exists is `rate_plans.meal_plan` — EP / CP / MAP / AP — which says what a rate
-- *includes* and nothing about what the meal is, when it is served, what it
-- costs if it is not included, or whether a vegetarian guest will find anything
-- to eat.
--
-- **What a rate plan includes stays derived from meal_plan.** This table does not
-- repeat it. EP includes nothing, CP includes breakfast, MAP breakfast and one
-- other, AP all three — one vocabulary, in one place, and no way for the two to
-- disagree about a booking somebody already paid for.

create table if not exists public.meal_options (
  id           uuid primary key default gen_random_uuid(),
  property_id  uuid not null references public.properties(id) on delete cascade,
  partner_id   uuid not null references public.partners(id) on delete cascade,

  meal         text not null check (meal in ('breakfast','lunch','dinner')),

  -- 'not_available' is an answer. A hotel with no kitchen at lunchtime should be
  -- able to say so once and have its AI say so every time, rather than leaving
  -- the row out and having the AI say it does not know.
  availability text not null default 'available'
               check (availability in ('available','on_request','not_available')),

  -- What it costs a guest whose rate does not already include it.
  price_paise  integer check (price_paise is null or price_paise >= 0),
  price_unit   text not null default 'per_person'
               check (price_unit in ('per_person','per_room','fixed')),

  starts_at    time,
  ends_at      time,

  diet         text check (diet is null or diet in ('veg','nonveg','both','jain','vegan')),
  service_style text check (service_style is null or service_style in ('buffet','set_menu','a_la_carte','room_service')),

  cuisine      text,
  note         text,

  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),

  -- One statement per meal per hotel. Two answers to "when is breakfast" is the
  -- failure this table exists to end.
  unique (property_id, meal)
);

create index if not exists meal_options_property_idx on public.meal_options (property_id);

alter table public.meal_options enable row level security;

do $$ begin
  drop policy if exists meal_options_read on public.meal_options;
create policy meal_options_read on public.meal_options
    for select using (owns_partner(partner_id));
exception when duplicate_object then null; end $$;

do $$ begin
  drop policy if exists meal_options_write on public.meal_options;
create policy meal_options_write on public.meal_options
    for all using (has_partner_perm(partner_id, 'rates'))
    with check (has_partner_perm(partner_id, 'rates'));
exception when duplicate_object then null; end $$;

comment on table public.meal_options is
  'What each meal is, when it is served and what it costs when it is not '
  'included. Whether a given rate includes it is derived from '
  'rate_plans.meal_plan and is deliberately not repeated here.';
