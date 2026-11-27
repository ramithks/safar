-- Safar fresh-project fix: upstream migrations ALTER/REFERENCE these StaySuite-partner
-- domain tables but never committed their base definitions (same pattern as
-- 2026-07-12_hotels_master.sql). Stubs reconstructed from ALTER/UPDATE/policy/JOIN/
-- CHECK/INSERT usage across migrations; types as declared or inferred from names.
-- Stubs are empty; the upgrade section drops and re-adds columns so reruns always
-- converge on these types.

CREATE TABLE IF NOT EXISTS public.agent_actions (
  created_at timestamptz NOT NULL DEFAULT now(),
  decision_source text,
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  idempotency_key text,
  result_type text
);

ALTER TABLE public.agent_actions DROP COLUMN IF EXISTS decision_source CASCADE;
ALTER TABLE public.agent_actions ADD COLUMN decision_source text;
ALTER TABLE public.agent_actions DROP COLUMN IF EXISTS idempotency_key CASCADE;
ALTER TABLE public.agent_actions ADD COLUMN idempotency_key text;
ALTER TABLE public.agent_actions DROP COLUMN IF EXISTS result_type CASCADE;
ALTER TABLE public.agent_actions ADD COLUMN result_type text;

CREATE TABLE IF NOT EXISTS public.agent_runs (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  intent text,
  model_tier text,
  required_action_status text
);

ALTER TABLE public.agent_runs DROP COLUMN IF EXISTS intent CASCADE;
ALTER TABLE public.agent_runs ADD COLUMN intent text;
ALTER TABLE public.agent_runs DROP COLUMN IF EXISTS model_tier CASCADE;
ALTER TABLE public.agent_runs ADD COLUMN model_tier text;
ALTER TABLE public.agent_runs DROP COLUMN IF EXISTS required_action_status CASCADE;
ALTER TABLE public.agent_runs ADD COLUMN required_action_status text;

CREATE TABLE IF NOT EXISTS public.agent_tools (
  agent_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  enabled boolean,
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tool_name text
);

ALTER TABLE public.agent_tools DROP COLUMN IF EXISTS agent_id CASCADE;
ALTER TABLE public.agent_tools ADD COLUMN agent_id uuid;
ALTER TABLE public.agent_tools DROP COLUMN IF EXISTS enabled CASCADE;
ALTER TABLE public.agent_tools ADD COLUMN enabled boolean;
ALTER TABLE public.agent_tools DROP COLUMN IF EXISTS tool_name CASCADE;
ALTER TABLE public.agent_tools ADD COLUMN tool_name text;

CREATE TABLE IF NOT EXISTS public.agents (
  agent_type text,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

ALTER TABLE public.agents DROP COLUMN IF EXISTS agent_type CASCADE;
ALTER TABLE public.agents ADD COLUMN agent_type text;

CREATE TABLE IF NOT EXISTS public.bookings (
  commission_pct numeric,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  item_id uuid
);

ALTER TABLE public.bookings DROP COLUMN IF EXISTS commission_pct CASCADE;
ALTER TABLE public.bookings ADD COLUMN commission_pct numeric;
ALTER TABLE public.bookings DROP COLUMN IF EXISTS item_id CASCADE;
ALTER TABLE public.bookings ADD COLUMN item_id uuid;

CREATE TABLE IF NOT EXISTS public.conversations (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);


CREATE TABLE IF NOT EXISTS public.hotel_websites (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);


CREATE TABLE IF NOT EXISTS public.meal_plan (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);


CREATE TABLE IF NOT EXISTS public.partner_listings (
  commission_pct numeric,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

ALTER TABLE public.partner_listings DROP COLUMN IF EXISTS commission_pct CASCADE;
ALTER TABLE public.partner_listings ADD COLUMN commission_pct numeric;

CREATE TABLE IF NOT EXISTS public.partner_members (
  accepted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  partner_id uuid,
  permissions jsonb,
  profile_id uuid,
  property_ids jsonb,
  role text,
  status text
);

ALTER TABLE public.partner_members DROP COLUMN IF EXISTS accepted_at CASCADE;
ALTER TABLE public.partner_members ADD COLUMN accepted_at timestamptz;
ALTER TABLE public.partner_members DROP COLUMN IF EXISTS partner_id CASCADE;
ALTER TABLE public.partner_members ADD COLUMN partner_id uuid;
ALTER TABLE public.partner_members DROP COLUMN IF EXISTS permissions CASCADE;
ALTER TABLE public.partner_members ADD COLUMN permissions jsonb;
ALTER TABLE public.partner_members DROP COLUMN IF EXISTS profile_id CASCADE;
ALTER TABLE public.partner_members ADD COLUMN profile_id uuid;
ALTER TABLE public.partner_members DROP COLUMN IF EXISTS property_ids CASCADE;
ALTER TABLE public.partner_members ADD COLUMN property_ids jsonb;
ALTER TABLE public.partner_members DROP COLUMN IF EXISTS role CASCADE;
ALTER TABLE public.partner_members ADD COLUMN role text;
ALTER TABLE public.partner_members DROP COLUMN IF EXISTS status CASCADE;
ALTER TABLE public.partner_members ADD COLUMN status text;

CREATE TABLE IF NOT EXISTS public.partner_payments (
  amount_paise integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text,
  razorpay_subscription_id text,
  status text
);

ALTER TABLE public.partner_payments DROP COLUMN IF EXISTS amount_paise CASCADE;
ALTER TABLE public.partner_payments ADD COLUMN amount_paise integer;
ALTER TABLE public.partner_payments DROP COLUMN IF EXISTS kind CASCADE;
ALTER TABLE public.partner_payments ADD COLUMN kind text;
ALTER TABLE public.partner_payments DROP COLUMN IF EXISTS razorpay_subscription_id CASCADE;
ALTER TABLE public.partner_payments ADD COLUMN razorpay_subscription_id text;
ALTER TABLE public.partner_payments DROP COLUMN IF EXISTS status CASCADE;
ALTER TABLE public.partner_payments ADD COLUMN status text;

CREATE TABLE IF NOT EXISTS public.partners (
  billed_amount_paise integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id uuid,
  platform_fee_bps numeric,
  subscription_id text,
  subscription_status text
);

ALTER TABLE public.partners DROP COLUMN IF EXISTS billed_amount_paise CASCADE;
ALTER TABLE public.partners ADD COLUMN billed_amount_paise integer;
ALTER TABLE public.partners DROP COLUMN IF EXISTS owner_id CASCADE;
ALTER TABLE public.partners ADD COLUMN owner_id uuid;
ALTER TABLE public.partners DROP COLUMN IF EXISTS platform_fee_bps CASCADE;
ALTER TABLE public.partners ADD COLUMN platform_fee_bps numeric;
ALTER TABLE public.partners DROP COLUMN IF EXISTS subscription_id CASCADE;
ALTER TABLE public.partners ADD COLUMN subscription_id text;
ALTER TABLE public.partners DROP COLUMN IF EXISTS subscription_status CASCADE;
ALTER TABLE public.partners ADD COLUMN subscription_status text;

CREATE TABLE IF NOT EXISTS public.platform_coupons (
  code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  kind text,
  plan_amount_paise integer,
  plan_period text,
  razorpay_offer_id uuid,
  value numeric
);

ALTER TABLE public.platform_coupons DROP COLUMN IF EXISTS code CASCADE;
ALTER TABLE public.platform_coupons ADD COLUMN code text;
ALTER TABLE public.platform_coupons DROP COLUMN IF EXISTS kind CASCADE;
ALTER TABLE public.platform_coupons ADD COLUMN kind text;
ALTER TABLE public.platform_coupons DROP COLUMN IF EXISTS plan_amount_paise CASCADE;
ALTER TABLE public.platform_coupons ADD COLUMN plan_amount_paise integer;
ALTER TABLE public.platform_coupons DROP COLUMN IF EXISTS plan_period CASCADE;
ALTER TABLE public.platform_coupons ADD COLUMN plan_period text;
ALTER TABLE public.platform_coupons DROP COLUMN IF EXISTS razorpay_offer_id CASCADE;
ALTER TABLE public.platform_coupons ADD COLUMN razorpay_offer_id uuid;
ALTER TABLE public.platform_coupons DROP COLUMN IF EXISTS value CASCADE;
ALTER TABLE public.platform_coupons ADD COLUMN value numeric;

CREATE TABLE IF NOT EXISTS public.properties (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);


CREATE TABLE IF NOT EXISTS public.provider_cost_rates (
  cost_per_unit_micros bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  currency text,
  effective_to timestamptz,
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model text,
  provider text,
  source text,
  unit text
);

ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS cost_per_unit_micros CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN cost_per_unit_micros bigint;
ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS currency CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN currency text;
ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS effective_to CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN effective_to timestamptz;
ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS model CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN model text;
ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS provider CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN provider text;
ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS source CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN source text;
ALTER TABLE public.provider_cost_rates DROP COLUMN IF EXISTS unit CASCADE;
ALTER TABLE public.provider_cost_rates ADD COLUMN unit text;

CREATE TABLE IF NOT EXISTS public.quotes (
  created_at timestamptz NOT NULL DEFAULT now(),
  group_tier_id uuid,
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

ALTER TABLE public.quotes DROP COLUMN IF EXISTS group_tier_id CASCADE;
ALTER TABLE public.quotes ADD COLUMN group_tier_id uuid;

CREATE TABLE IF NOT EXISTS public.rate_plans (
  allocation_units integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  derive_discount_pct numeric,
  derived_from_plan_id uuid,
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

ALTER TABLE public.rate_plans DROP COLUMN IF EXISTS allocation_units CASCADE;
ALTER TABLE public.rate_plans ADD COLUMN allocation_units integer;
ALTER TABLE public.rate_plans DROP COLUMN IF EXISTS derive_discount_pct CASCADE;
ALTER TABLE public.rate_plans ADD COLUMN derive_discount_pct numeric;
ALTER TABLE public.rate_plans DROP COLUMN IF EXISTS derived_from_plan_id CASCADE;
ALTER TABLE public.rate_plans ADD COLUMN derived_from_plan_id uuid;

CREATE TABLE IF NOT EXISTS public.razorpay_webhook_events (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);


CREATE TABLE IF NOT EXISTS public.reservations (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  partner_id uuid,
  payout_paise integer,
  platform_fee_bps integer,
  platform_fee_paise integer,
  total_paise numeric
);

ALTER TABLE public.reservations DROP COLUMN IF EXISTS partner_id CASCADE;
ALTER TABLE public.reservations ADD COLUMN partner_id uuid;
ALTER TABLE public.reservations DROP COLUMN IF EXISTS payout_paise CASCADE;
ALTER TABLE public.reservations ADD COLUMN payout_paise integer;
ALTER TABLE public.reservations DROP COLUMN IF EXISTS platform_fee_bps CASCADE;
ALTER TABLE public.reservations ADD COLUMN platform_fee_bps integer;
ALTER TABLE public.reservations DROP COLUMN IF EXISTS platform_fee_paise CASCADE;
ALTER TABLE public.reservations ADD COLUMN platform_fee_paise integer;
ALTER TABLE public.reservations DROP COLUMN IF EXISTS total_paise CASCADE;
ALTER TABLE public.reservations ADD COLUMN total_paise numeric;

CREATE TABLE IF NOT EXISTS public.room_types (
  accessible text,
  bathroom text,
  bed_count integer,
  children_allowed text,
  connecting text,
  created_at timestamptz NOT NULL DEFAULT now(),
  crib text,
  crib_paise integer,
  extra_bed text,
  extra_bed_paise integer,
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  max_adults integer,
  max_children integer,
  photo_meta jsonb not null default '{}'::jsonb,
  smoking text
);

ALTER TABLE public.room_types DROP COLUMN IF EXISTS accessible CASCADE;
ALTER TABLE public.room_types ADD COLUMN accessible text;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS bathroom CASCADE;
ALTER TABLE public.room_types ADD COLUMN bathroom text;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS bed_count CASCADE;
ALTER TABLE public.room_types ADD COLUMN bed_count integer;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS children_allowed CASCADE;
ALTER TABLE public.room_types ADD COLUMN children_allowed text;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS connecting CASCADE;
ALTER TABLE public.room_types ADD COLUMN connecting text;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS crib CASCADE;
ALTER TABLE public.room_types ADD COLUMN crib text;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS crib_paise CASCADE;
ALTER TABLE public.room_types ADD COLUMN crib_paise integer;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS extra_bed CASCADE;
ALTER TABLE public.room_types ADD COLUMN extra_bed text;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS extra_bed_paise CASCADE;
ALTER TABLE public.room_types ADD COLUMN extra_bed_paise integer;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS max_adults CASCADE;
ALTER TABLE public.room_types ADD COLUMN max_adults integer;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS max_children CASCADE;
ALTER TABLE public.room_types ADD COLUMN max_children integer;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS photo_meta CASCADE;
ALTER TABLE public.room_types ADD COLUMN photo_meta jsonb not null default '{}'::jsonb;
ALTER TABLE public.room_types DROP COLUMN IF EXISTS smoking CASCADE;
ALTER TABLE public.room_types ADD COLUMN smoking text;

CREATE TABLE IF NOT EXISTS public.services (
  availability text,
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  max_quantity integer,
  price_unit text not null default 'fixed'
);

ALTER TABLE public.services DROP COLUMN IF EXISTS availability CASCADE;
ALTER TABLE public.services ADD COLUMN availability text;
ALTER TABLE public.services DROP COLUMN IF EXISTS max_quantity CASCADE;
ALTER TABLE public.services ADD COLUMN max_quantity integer;
ALTER TABLE public.services DROP COLUMN IF EXISTS price_unit CASCADE;
ALTER TABLE public.services ADD COLUMN price_unit text not null default 'fixed';

CREATE TABLE IF NOT EXISTS public.tasks (
  created_at timestamptz NOT NULL DEFAULT now(),
  id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);

-- Unique constraints the app's upserts rely on.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'partner_members_partner_profile_key') THEN
    ALTER TABLE public.partner_members ADD CONSTRAINT partner_members_partner_profile_key UNIQUE (partner_id, profile_id);
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_tools_agent_tool_key') THEN
    ALTER TABLE public.agent_tools ADD CONSTRAINT agent_tools_agent_tool_key UNIQUE (agent_id, tool_name);
  END IF;
END $$;

-- Safar fresh-project fix: upstream RLS policies call these partner-auth helpers
-- but never committed their definitions. Reconstructed from app/deps/property.py
-- and app/services/pms/workspace.py membership semantics. DEFAULT DENY: anything
-- ambiguous (no active member row, no owner row, missing permission key) is false.
CREATE OR REPLACE FUNCTION public.owns_partner(pid uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.partners p
    WHERE p.id = pid AND p.owner_id = auth.uid()
  ) OR EXISTS (
    SELECT 1 FROM public.partner_members pm
    WHERE pm.partner_id = pid
      AND pm.profile_id = auth.uid()
      AND pm.status = 'active'
  );
$$;

CREATE OR REPLACE FUNCTION public.has_partner_perm(pid uuid, perm text)
RETURNS boolean LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $$
DECLARE
  m record;
BEGIN
  IF EXISTS (SELECT 1 FROM public.partners p WHERE p.id = pid AND p.owner_id = auth.uid()) THEN
    RETURN true;
  END IF;
  SELECT role, permissions INTO m FROM public.partner_members
  WHERE partner_id = pid AND profile_id = auth.uid() AND status = 'active'
  LIMIT 1;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  IF m.role = 'owner' THEN
    RETURN true;
  END IF;
  RETURN coalesce((m.permissions ->> perm)::boolean, false);
END;
$$;

-- Ordering fix: 2026-08-23_marketing_that_can_be_reused.sql references
-- public.experiences but sorts before 2026-08-23_what_a_hotel_sells_besides_a_bed.sql,
-- which creates it. Definition copied verbatim from the latter (its
-- CREATE TABLE IF NOT EXISTS becomes a no-op).

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

-- Index-referenced columns.
ALTER TABLE public.agent_runs DROP COLUMN IF EXISTS started_at CASCADE;
ALTER TABLE public.agent_runs ADD COLUMN started_at timestamptz;
ALTER TABLE public.agent_actions DROP COLUMN IF EXISTS property_id CASCADE;
ALTER TABLE public.agent_actions ADD COLUMN property_id uuid;
