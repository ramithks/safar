-- ============================================================================
-- Safar Trip Engine v4 — pre-change snapshots (integrity layer)
-- Design: docs/trip_engine_architecture.md §3, §6
-- Idempotent: safe to re-run.
--
-- The full committed trip state is archived here BEFORE every regeneration /
-- structural rebuild, so a bad rebuild is restorable through the normal
-- CAS write path (app.trip_state.integrity.restore_snapshot). Bounded
-- retention (newest N per trip) is enforced application-side.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.trip_snapshots (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id        uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  generation_id  uuid,                          -- manifest that superseded this state (if any)
  reason         text NOT NULL DEFAULT '',      -- pre-regenerate | pre-structural-edit | manual
  spec_version   int  NOT NULL DEFAULT 1,
  last_seq       int,                           -- snapshot of trip_states.last_seq at archive time
  state          jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE INDEX IF NOT EXISTS idx_trip_snapshots_trip
  ON public.trip_snapshots (trip_id, created_at DESC);

ALTER TABLE public.trip_snapshots ENABLE ROW LEVEL SECURITY;

-- Service role only (backend writes/reads through the service key; snapshots
-- are never read directly by clients).
DROP POLICY IF EXISTS trip_snapshots_service ON public.trip_snapshots;
CREATE POLICY trip_snapshots_service ON public.trip_snapshots
  FOR ALL USING (auth.role() = 'service_role')
  WITH CHECK (auth.role() = 'service_role');
