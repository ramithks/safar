-- An agent rate can be a discount from the public one.
--
-- A B2B rate today is a rate plan with `audience = 'b2b'` and a base price of
-- its own, and that is right for a hotel that negotiates a flat agency rate. It
-- is wrong for the far more common case — "agents get 15% off whatever we are
-- charging" — because the agency rate then has to be re-entered by hand every
-- time the public calendar moves, and the day somebody forgets, agents are
-- selling last season's price.
--
-- A derived plan carries no price of its own. `nightly_for_plan` resolves its
-- parent's rate for that night and takes the percentage off, so a seasonal
-- public rate moves its agency rate with it, per night, automatically.
--
-- The validity columns exist for the same reason: an agency contract runs from
-- a date to a date, on named days, against an allocation. All four were things
-- a hotel could only express in an email.

alter table public.rate_plans
  add column if not exists derived_from_plan_id uuid references public.rate_plans(id) on delete restrict,
  add column if not exists derive_discount_pct integer,
  add column if not exists valid_from date,
  add column if not exists valid_to date,
  add column if not exists days_of_week smallint[] not null default '{}',
  add column if not exists allocation_units integer;

do $$ begin
  alter table public.rate_plans
    add constraint rate_plans_derivation_check check (
      -- Either it derives and says by how much, or it does neither.
      (derived_from_plan_id is null and derive_discount_pct is null)
      or (derived_from_plan_id is not null and derive_discount_pct between 0 and 100)
    );
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.rate_plans
    add constraint rate_plans_not_self_derived check (derived_from_plan_id is distinct from id);
exception when duplicate_object then null; end $$;

do $$ begin
  alter table public.rate_plans
    add constraint rate_plans_allocation_check
    check (allocation_units is null or allocation_units >= 0);
exception when duplicate_object then null; end $$;

create index if not exists rate_plans_derived_idx
  on public.rate_plans (derived_from_plan_id) where derived_from_plan_id is not null;

comment on column public.rate_plans.derived_from_plan_id is
  'When set, this plan has no price of its own: each night is the parent plan''s '
  'rate for that night less derive_discount_pct. One level only — a derived plan '
  'may not itself be derived from, enforced in services/pms/rates.py.';
comment on column public.rate_plans.allocation_units is
  'How many units of the room type this plan may sell. Null means the whole '
  'pool. Enforced at quote time, never by a second inventory table.';
