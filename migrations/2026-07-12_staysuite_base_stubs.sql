-- Safar fresh-project fix: upstream migrations ALTER/REFERENCE these StaySuite-partner
-- domain tables but never committed their base definitions (same pattern as
-- 2026-07-12_hotels_master.sql). Minimal stubs reconstructed from the ALTER, UPDATE,
-- policy and JOIN column usage across migrations; types as declared or inferred.

CREATE TABLE IF NOT EXISTS public.agent_actions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  decision_source text,
  idempotency_key text
);

ALTER TABLE public.agent_actions ADD COLUMN IF NOT EXISTS decision_source text;
ALTER TABLE public.agent_actions ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE TABLE IF NOT EXISTS public.agent_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  intent text
);

ALTER TABLE public.agent_runs ADD COLUMN IF NOT EXISTS intent text;

CREATE TABLE IF NOT EXISTS public.agent_tools (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_type text,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.agents ADD COLUMN IF NOT EXISTS agent_type text;

CREATE TABLE IF NOT EXISTS public.bookings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  commission_pct numeric,
  item_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.bookings ADD COLUMN IF NOT EXISTS commission_pct numeric;
ALTER TABLE public.bookings ADD COLUMN IF NOT EXISTS item_id uuid;

CREATE TABLE IF NOT EXISTS public.conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.hotel_websites (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.meal_plan (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.partner_listings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  commission_pct numeric,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.partner_listings ADD COLUMN IF NOT EXISTS commission_pct numeric;

CREATE TABLE IF NOT EXISTS public.partner_members (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.partner_payments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  amount_paise integer,
  created_at timestamptz NOT NULL DEFAULT now(),
  kind text,
  razorpay_subscription_id uuid,
  status text
);

ALTER TABLE public.partner_payments ADD COLUMN IF NOT EXISTS amount_paise integer;
ALTER TABLE public.partner_payments ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE public.partner_payments ADD COLUMN IF NOT EXISTS razorpay_subscription_id uuid;
ALTER TABLE public.partner_payments ADD COLUMN IF NOT EXISTS status text;

CREATE TABLE IF NOT EXISTS public.partners (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  billed_amount_paise integer,
  platform_fee_bps numeri,
  subscription_id uuid,
  subscription_status text,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.partners ADD COLUMN IF NOT EXISTS billed_amount_paise integer;
ALTER TABLE public.partners ADD COLUMN IF NOT EXISTS platform_fee_bps numeri;
ALTER TABLE public.partners ADD COLUMN IF NOT EXISTS subscription_id uuid;
ALTER TABLE public.partners ADD COLUMN IF NOT EXISTS subscription_status text;

CREATE TABLE IF NOT EXISTS public.platform_coupons (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  plan_amount_paise integer check (plan_amount_paise is null or plan_amount_paise >= 0)
);

ALTER TABLE public.platform_coupons ADD COLUMN IF NOT EXISTS plan_amount_paise integer check (plan_amount_paise is null or plan_amount_paise >= 0);

CREATE TABLE IF NOT EXISTS public.properties (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.provider_cost_rates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  effective_to text,
  model text,
  provider text,
  unit text,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS effective_to text;
ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS unit text;

CREATE TABLE IF NOT EXISTS public.quotes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  group_tier_id uuid
);

ALTER TABLE public.quotes ADD COLUMN IF NOT EXISTS group_tier_id uuid;

CREATE TABLE IF NOT EXISTS public.rate_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  derived_from_plan_id uuid
);

ALTER TABLE public.rate_plans ADD COLUMN IF NOT EXISTS derived_from_plan_id uuid;

CREATE TABLE IF NOT EXISTS public.razorpay_webhook_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.reservations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  partner_id integer,
  platform_fee_paise integer,
  total_paise numeric,
  created_at timestamptz NOT NULL DEFAULT now(),
  platform_fee_bps integer
);

ALTER TABLE public.reservations ADD COLUMN IF NOT EXISTS partner_id integer;
ALTER TABLE public.reservations ADD COLUMN IF NOT EXISTS platform_fee_paise integer;
ALTER TABLE public.reservations ADD COLUMN IF NOT EXISTS total_paise numeric;
ALTER TABLE public.reservations ADD COLUMN IF NOT EXISTS platform_fee_bps integer;

CREATE TABLE IF NOT EXISTS public.room_types (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  max_adults integer,
  photo_meta jsonb not null default '{}'::jsonb
);

ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS max_adults integer;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS photo_meta jsonb not null default '{}'::jsonb;

CREATE TABLE IF NOT EXISTS public.services (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now(),
  price_unit text not null default 'fixed'
);

ALTER TABLE public.services ADD COLUMN IF NOT EXISTS price_unit text not null default 'fixed';

CREATE TABLE IF NOT EXISTS public.tasks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Bare columns referenced by later CHECK constraints on stub tables.
ALTER TABLE public.platform_coupons ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE public.platform_coupons ADD COLUMN IF NOT EXISTS razorpay_offer_id text;
ALTER TABLE public.platform_coupons ADD COLUMN IF NOT EXISTS value numeric;
ALTER TABLE public.platform_coupons ADD COLUMN IF NOT EXISTS code text;

-- Columns the app reads/writes on partner stubs (app/services/pms/*, app/deps/property.py).
ALTER TABLE public.partners ADD COLUMN IF NOT EXISTS owner_id uuid;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS partner_id uuid;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS profile_id uuid;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS property_ids jsonb;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS permissions jsonb;
ALTER TABLE public.partner_members ADD COLUMN IF NOT EXISTS accepted_at timestamptz;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'partner_members_partner_profile_key'
  ) THEN
    ALTER TABLE public.partner_members
      ADD CONSTRAINT partner_members_partner_profile_key UNIQUE (partner_id, profile_id);
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

-- agent_tools provisioning rows (2026-08-23_a_selling_agent...) and
-- provider_cost_rates pricing rows (2026-08-24_a_model_nobody_priced_is_free).
ALTER TABLE public.agent_tools ADD COLUMN IF NOT EXISTS agent_id uuid;
ALTER TABLE public.agent_tools ADD COLUMN IF NOT EXISTS tool_name text;
ALTER TABLE public.agent_tools ADD COLUMN IF NOT EXISTS enabled boolean;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_tools_agent_tool_key') THEN
    ALTER TABLE public.agent_tools ADD CONSTRAINT agent_tools_agent_tool_key UNIQUE (agent_id, tool_name);
  END IF;
END $$;
ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS cost_per_unit_micros bigint;
ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS currency text;
ALTER TABLE public.provider_cost_rates ADD COLUMN IF NOT EXISTS source text;

-- Bare columns from later CHECK constraints and INSERT column lists.
ALTER TABLE public.platform_coupons ADD COLUMN IF NOT EXISTS plan_period text;
ALTER TABLE public.rate_plans ADD COLUMN IF NOT EXISTS allocation_units text;
ALTER TABLE public.rate_plans ADD COLUMN IF NOT EXISTS derive_discount_pct text;
ALTER TABLE public.agent_actions ADD COLUMN IF NOT EXISTS result_type text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS accessible text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS bathroom text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS bed_count text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS children_allowed text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS connecting text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS crib text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS crib_paise text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS extra_bed text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS extra_bed_paise text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS max_children text;
ALTER TABLE public.room_types ADD COLUMN IF NOT EXISTS smoking text;
ALTER TABLE public.reservations ADD COLUMN IF NOT EXISTS payout_paise text;
ALTER TABLE public.services ADD COLUMN IF NOT EXISTS availability text;
ALTER TABLE public.services ADD COLUMN IF NOT EXISTS max_quantity text;
ALTER TABLE public.agent_runs ADD COLUMN IF NOT EXISTS model_tier text;
ALTER TABLE public.agent_runs ADD COLUMN IF NOT EXISTS required_action_status text;
