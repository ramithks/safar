-- ============================================================================
-- Safar Growth OS — referral codes + travel-wallet credit ledger +
-- public group trips (docs/growth_os_plan.md Phase 2)
-- Idempotent: safe to re-run.
--
-- Design rules:
--   * Wallet balance = SUM(credit_ledger.amount) — NEVER a mutable column.
--   * Referral reward lands on the referred user's FIRST COMPLETED action
--     (trip generated / group joined), not at signup — the fraud guard.
--   * One redemption per user (referred_by set once); self-referral blocked
--     application-side.
-- ============================================================================

-- ── referral_codes — one shareable code per member ───────────────────────────
CREATE TABLE IF NOT EXISTS public.referral_codes (
  user_id      uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  code         text NOT NULL UNIQUE,             -- e.g. "TB-SAHIL-4X2"
  member_no    bigint GENERATED ALWAYS AS IDENTITY,  -- "Member #1024" on the card
  display_name text,
  referred_by  uuid REFERENCES auth.users(id),   -- who referred THIS user (once)
  created_at   timestamptz NOT NULL DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS idx_referral_codes_code ON public.referral_codes (code);
CREATE INDEX IF NOT EXISTS idx_referral_codes_referred_by ON public.referral_codes (referred_by);

-- ── credit_ledger — append-only travel wallet ────────────────────────────────
CREATE TABLE IF NOT EXISTS public.credit_ledger (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  amount      int  NOT NULL,                     -- +credit / -redemption, in ₹
  reason      text NOT NULL,                     -- referral_signup | referral_reward | group_trip_bonus | redemption | adjustment
  ref_id      text,                              -- related entity (referred user id, trip id…)
  note        text,
  created_at  timestamptz NOT NULL DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user ON public.credit_ledger (user_id, created_at DESC);
-- One reward per (reason, ref) pair per user — idempotent awarding.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_ledger_once
  ON public.credit_ledger (user_id, reason, ref_id) WHERE ref_id IS NOT NULL;

-- ── public group trips (community/homepage showcase) ─────────────────────────
ALTER TABLE public.trip_groups ADD COLUMN IF NOT EXISTS is_public boolean NOT NULL DEFAULT false;
ALTER TABLE public.trip_groups ADD COLUMN IF NOT EXISTS trip_date date;
ALTER TABLE public.trip_groups ADD COLUMN IF NOT EXISTS max_members int;
CREATE INDEX IF NOT EXISTS idx_trip_groups_public
  ON public.trip_groups (is_public, created_at DESC) WHERE is_public = true;

-- ── RLS: service-role only (all access via the backend API) ─────────────────
ALTER TABLE public.referral_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_ledger  ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS referral_codes_service ON public.referral_codes;
CREATE POLICY referral_codes_service ON public.referral_codes
  FOR ALL USING (auth.role() = 'service_role') WITH CHECK (auth.role() = 'service_role');

DROP POLICY IF EXISTS credit_ledger_service ON public.credit_ledger;
CREATE POLICY credit_ledger_service ON public.credit_ledger
  FOR ALL USING (auth.role() = 'service_role') WITH CHECK (auth.role() = 'service_role');
