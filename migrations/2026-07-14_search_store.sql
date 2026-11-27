-- ============================================================================
-- Safar Persistence v3 — Search Store, View Layer, Reco Index, Trip Memory
-- Design: docs/persistence_architecture.md  (Phase 1: additive, dual-write)
-- Idempotent: safe to re-run. Apply on Supabase (SQL editor / psql).
--
-- Nothing here changes existing reads. It stands up the tables the emitter
-- dual-writes into (search_batches/search_results) plus the read-side tables
-- (trip_result_views, trip_recommendations, trip_memory) that Phase 2 wires up.
-- ============================================================================

-- ── search_batches — one search event (a leg's transport pull, a stop's hotels) ─
CREATE TABLE IF NOT EXISTS public.search_batches (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id      uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  kind         text NOT NULL,                 -- transport | hotel | activity | restaurant | weather | visa | event
  scope_id     text NOT NULL DEFAULT '',      -- leg_id (transport) | stop_id/dest (hotel) | '' (trip-wide)
  mode         text,                          -- flight|train|bus (transport); null otherwise
  params_hash  text NOT NULL,                 -- hash(origin,dest,date,pax,…) — dedup + cache key
  origin       text,
  destination  text,
  depart_date  date,
  status       text NOT NULL DEFAULT 'ready', -- searching | ready | empty | error
  result_count int  NOT NULL DEFAULT 0,
  created_at   timestamptz NOT NULL DEFAULT timezone('utc', now()),
  expires_at   timestamptz
);
CREATE INDEX IF NOT EXISTS idx_batches_trip   ON public.search_batches (trip_id, kind, scope_id);
CREATE INDEX IF NOT EXISTS idx_batches_params ON public.search_batches (params_hash);
CREATE INDEX IF NOT EXISTS idx_batches_live   ON public.search_batches (params_hash, expires_at);

-- ── search_results — one bookable option; thin cols rank/render, raw on demand ──
CREATE TABLE IF NOT EXISTS public.search_results (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  batch_id      uuid NOT NULL REFERENCES public.search_batches(id) ON DELETE CASCADE,
  trip_id       uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  item_type     text NOT NULL,                -- flight|train|bus|hotel|activity|restaurant
  ext_id        text,                         -- provider id (stable within a batch)
  operator      text,
  price         numeric,
  currency      text DEFAULT 'INR',
  departs_at    timestamptz,                  -- resolved date+time (fixes missing-date issue)
  arrives_at    timestamptz,
  duration_min  int,
  stops         int,
  rating        numeric,
  refundable    boolean,
  summary       jsonb NOT NULL DEFAULT '{}',  -- card payload (name, badges, booking_url, …) ~1-2KB
  raw           jsonb,                        -- full provider dict — NEVER in default SELECT
  created_at    timestamptz NOT NULL DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS idx_results_batch       ON public.search_results (batch_id);
CREATE INDEX IF NOT EXISTS idx_results_trip_type   ON public.search_results (trip_id, item_type);
CREATE INDEX IF NOT EXISTS idx_results_batch_price ON public.search_results (batch_id, price);
CREATE INDEX IF NOT EXISTS idx_results_batch_dep   ON public.search_results (batch_id, departs_at);

-- ── trip_result_views — mutable personalization over immutable results ──────────
CREATE TABLE IF NOT EXISTS public.trip_result_views (
  trip_id        uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  result_id      uuid NOT NULL REFERENCES public.search_results(id) ON DELETE CASCADE,
  visible        boolean,                     -- null = default (visible); false = hidden
  favorite       boolean NOT NULL DEFAULT false,
  selected       boolean NOT NULL DEFAULT false,
  score_override real,
  notes          text,
  source         text NOT NULL DEFAULT 'user',-- user | copilot | rule:<memory_key>
  updated_at     timestamptz NOT NULL DEFAULT timezone('utc', now()),
  PRIMARY KEY (trip_id, result_id)
);
CREATE INDEX IF NOT EXISTS idx_result_views_trip ON public.trip_result_views (trip_id);

-- ── trip_recommendations — precomputed scores so chat never re-ranks ────────────
CREATE TABLE IF NOT EXISTS public.trip_recommendations (
  trip_id      uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  scope_id     text NOT NULL DEFAULT '',
  batch_id     uuid REFERENCES public.search_batches(id) ON DELETE CASCADE,
  result_id    uuid NOT NULL REFERENCES public.search_results(id) ON DELETE CASCADE,
  score        real NOT NULL,
  subscores    jsonb NOT NULL DEFAULT '{}',   -- {persona, budget, comfort, hidden_gem, price_fairness}
  explanation  text,
  signals      jsonb NOT NULL DEFAULT '{}',
  persona      text NOT NULL DEFAULT 'default',
  computed_at  timestamptz NOT NULL DEFAULT timezone('utc', now()),
  PRIMARY KEY (trip_id, result_id, persona)
);
CREATE INDEX IF NOT EXISTS idx_trip_recs_scope ON public.trip_recommendations (trip_id, scope_id, score DESC);

-- ── trip_memory — per-trip conversational rules (distinct from user_travel_memory) ─
CREATE TABLE IF NOT EXISTS public.trip_memory (
  trip_id     uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  key         text NOT NULL,                  -- avoid_early_flights | only_refundable | rank_axis | diet | stay_tier
  scope       text NOT NULL DEFAULT '',       -- '' (whole trip) | leg_id | stop_id
  value       jsonb NOT NULL,
  confidence  real NOT NULL DEFAULT 0.6,
  source      text NOT NULL DEFAULT 'copilot',-- copilot | explicit | inferred
  updated_at  timestamptz NOT NULL DEFAULT timezone('utc', now()),
  PRIMARY KEY (trip_id, key, scope)
);

-- ── RLS — read by trip owner / shared viewers; all writes via service role ──────
ALTER TABLE public.search_batches       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.search_results       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_result_views    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_recommendations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_memory          ENABLE ROW LEVEL SECURITY;

-- Shared visibility predicate mirrors schema.sql:472-475 (trip_state_events_own).
DROP POLICY IF EXISTS search_batches_read ON public.search_batches;
CREATE POLICY search_batches_read ON public.search_batches FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = search_batches.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link','public'))));

DROP POLICY IF EXISTS search_results_read ON public.search_results;
CREATE POLICY search_results_read ON public.search_results FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = search_results.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link','public'))));

DROP POLICY IF EXISTS trip_result_views_read ON public.trip_result_views;
CREATE POLICY trip_result_views_read ON public.trip_result_views FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = trip_result_views.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link','public'))));

DROP POLICY IF EXISTS trip_recommendations_read ON public.trip_recommendations;
CREATE POLICY trip_recommendations_read ON public.trip_recommendations FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = trip_recommendations.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link','public'))));

DROP POLICY IF EXISTS trip_memory_read ON public.trip_memory;
CREATE POLICY trip_memory_read ON public.trip_memory FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = trip_memory.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link','public'))));
-- No INSERT/UPDATE/DELETE policies: writes go through the service role (bypasses RLS).
</content>
