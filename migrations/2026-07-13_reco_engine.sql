-- ============================================================================
-- Safar Recommendation Engine — schema migration
-- Design: docs/recommendation_engine.md
-- Idempotent: safe to re-run. Apply on Supabase (SQL editor / psql).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ── hotels (master identity table already exists) — additive columns ──────────
ALTER TABLE public.hotels ADD COLUMN IF NOT EXISTS slug               text;
ALTER TABLE public.hotels ADD COLUMN IF NOT EXISTS subscores          jsonb;
ALTER TABLE public.hotels ADD COLUMN IF NOT EXISTS landmarks          jsonb;
ALTER TABLE public.hotels ADD COLUMN IF NOT EXISTS canonical_hotel_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS hotels_slug_key ON public.hotels (slug) WHERE slug IS NOT NULL;
CREATE INDEX IF NOT EXISTS hotels_dest_idx ON public.hotels (destination);

-- ── hotel_profiles (SEO + semantic understanding, Part 3/12.2) ────────────────
CREATE TABLE IF NOT EXISTS public.hotel_profiles (
  hotel_id            uuid PRIMARY KEY REFERENCES public.hotels(id) ON DELETE CASCADE,
  personality_tags    text[]  NOT NULL DEFAULT '{}',
  one_liner           text,
  seo_summary         text,
  highlights          text[]  DEFAULT '{}',
  best_for            text[]  DEFAULT '{}',
  not_for             text[]  DEFAULT '{}',
  confidence          real,
  source_review_count int,
  generated_by        text,
  refreshed_at        timestamptz DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS hotel_profiles_tags_idx ON public.hotel_profiles USING gin (personality_tags);

-- ── Vector tables (384-dim, Part 6) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.hotel_embeddings (
  hotel_id   uuid PRIMARY KEY REFERENCES public.hotels(id) ON DELETE CASCADE,
  embedding  vector(384) NOT NULL,
  doc_hash   text,
  model      text,
  updated_at timestamptz DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS hotel_emb_hnsw ON public.hotel_embeddings
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS public.personality_centroids (
  tag         text PRIMARY KEY,
  centroid    vector(384) NOT NULL,
  n_exemplars int,
  updated_at  timestamptz DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS public.traveller_profiles (
  user_id    uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  axes       jsonb,
  embedding  vector(384),
  updated_at timestamptz DEFAULT timezone('utc', now())
);

-- ── transport_options (training-data flywheel, Part 12.5) ─────────────────────
CREATE TABLE IF NOT EXISTS public.transport_options (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mode             text NOT NULL,
  source           text,
  origin           text,
  destination      text,
  depart_date      date,
  operator         text,
  operator_code    text,
  service_no       text,
  depart_time      text,
  arrive_time      text,
  duration_min     int,
  stops            int,
  price            numeric,
  currency         text DEFAULT 'INR',
  fare_type        text,
  baggage_kg       int,
  cabin_baggage_kg int,
  layover_airports text[],
  seats_available  int,
  refundable       boolean,
  raw              jsonb,
  scraped_at       timestamptz DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS transport_od_date_idx ON public.transport_options (mode, origin, destination, depart_date);
CREATE INDEX IF NOT EXISTS transport_scraped_idx ON public.transport_options (scraped_at);

-- ── Feedback + learning (Part 9/12.6) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.rec_feedback (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid,
  trip_id    uuid,
  session_id text,
  item_type  text,
  item_id    text,
  event      text NOT NULL,
  position   int,
  dwell_ms   int,
  context    jsonb,
  created_at timestamptz DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS rec_feedback_user_idx ON public.rec_feedback (user_id, created_at);
CREATE INDEX IF NOT EXISTS rec_feedback_item_idx ON public.rec_feedback (item_type, item_id);

CREATE TABLE IF NOT EXISTS public.ranking_weights (
  persona    text,
  feature    text,
  weight     real,
  variant    text DEFAULT 'control',
  updated_at timestamptz DEFAULT timezone('utc', now()),
  PRIMARY KEY (persona, feature, variant)
);

CREATE TABLE IF NOT EXISTS public.expected_price (
  scope       text,
  key         text,
  p10         numeric,
  p50         numeric,
  p90         numeric,
  n           int,
  computed_at timestamptz DEFAULT timezone('utc', now()),
  PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS public.destination_priors (
  destination    text PRIMARY KEY,
  top_personas   text[],
  top_vibes      text[],
  seed_embedding vector(384),
  updated_at     timestamptz DEFAULT timezone('utc', now())
);

-- ── Semantic search RPC (pgvector ANN can't go through PostgREST query builder) ─
CREATE OR REPLACE FUNCTION public.match_hotels(
  query_embedding vector(384),
  match_destination text DEFAULT NULL,
  match_count int DEFAULT 6
)
RETURNS TABLE(hotel_id uuid, name text, slug text, similarity float)
LANGUAGE sql STABLE AS $$
  SELECT h.id, h.name, h.slug, 1 - (e.embedding <=> query_embedding) AS similarity
  FROM public.hotel_embeddings e
  JOIN public.hotels h ON h.id = e.hotel_id
  WHERE match_destination IS NULL OR h.destination = match_destination
  ORDER BY e.embedding <=> query_embedding
  LIMIT match_count;
$$;

-- ── RLS — hotels + hotel_profiles are PUBLIC (SEO pages); the rest is service-only ─
ALTER TABLE public.hotel_profiles     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.hotel_embeddings   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.personality_centroids ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.traveller_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transport_options  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.rec_feedback       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ranking_weights    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.expected_price     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.destination_priors ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS hotel_profiles_public_read ON public.hotel_profiles;
CREATE POLICY hotel_profiles_public_read ON public.hotel_profiles FOR SELECT USING (true);

DROP POLICY IF EXISTS traveller_profiles_own ON public.traveller_profiles;
CREATE POLICY traveller_profiles_own ON public.traveller_profiles FOR SELECT USING (auth.uid() = user_id);
-- All writes + all other tables: service role only (bypasses RLS); no anon policy = denied.
