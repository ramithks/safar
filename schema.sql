-- Safar — single source of truth
-- Run this on a fresh Supabase project to recreate the full schema.

-- ============================================================
-- HELPER FUNCTIONS
-- ============================================================

CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.profiles WHERE id = auth.uid() AND role = 'admin'
  );
$$;

CREATE OR REPLACE FUNCTION public.is_group_member(gid uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.group_members WHERE group_id = gid AND user_id = auth.uid()
  );
$$;

CREATE OR REPLACE FUNCTION public.is_group_admin(gid uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.group_members WHERE group_id = gid AND user_id = auth.uid() AND role = 'admin'
  );
$$;

-- ============================================================
-- TABLES
-- ============================================================

CREATE TABLE IF NOT EXISTS public.profiles (
  id                   uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  full_name            text,
  username             text,
  email                text,
  bio                  text,
  avatar_url           text,
  city                 text,
  country              text,
  gender               text,
  age                  integer,
  phone_number         text,
  language             text DEFAULT 'en',
  currency             text DEFAULT 'inr',
  role                 text DEFAULT 'user',
  vibes                text[] DEFAULT '{}',
  preferences          jsonb DEFAULT '{}',
  social_links         jsonb DEFAULT '{}',
  has_early_access     boolean DEFAULT false,
  onboarding_completed boolean DEFAULT false,
  updated_at           timestamptz,
  created_at           timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS public.trips (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title          text NOT NULL,
  destination    text,
  start_date     date,
  end_date       date,
  budget         numeric,
  currency       text DEFAULT 'INR',
  description    text,
  image_url      text,
  itinerary_data jsonb DEFAULT '{}',
  is_public      boolean DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS public.internal_trip_logs (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  destination    text,
  days           integer,
  budget         text,
  interests      jsonb,
  itinerary_data jsonb,
  is_public      boolean DEFAULT true,
  created_at     timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.trip_groups (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id     text,
  name        text NOT NULL,
  destination text,
  created_by  uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  invite_code text NOT NULL,
  created_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.group_members (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id     uuid REFERENCES public.trip_groups(id) ON DELETE CASCADE,
  user_id      uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  display_name text,
  role         text DEFAULT 'member' CHECK (role IN ('admin', 'member')),
  joined_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.group_messages (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id    uuid NOT NULL REFERENCES public.trip_groups(id) ON DELETE CASCADE,
  user_id     uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  sender_name text NOT NULL,
  text        text NOT NULL,
  type        text NOT NULL DEFAULT 'text',
  poll_data   jsonb,
  created_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.group_expenses (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id        uuid NOT NULL REFERENCES public.trip_groups(id) ON DELETE CASCADE,
  title           text NOT NULL,
  amount          numeric NOT NULL,
  paid_by         text NOT NULL,
  paid_by_user_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  split_between   text[] DEFAULT '{}',
  category        text DEFAULT 'Other',
  date            text,
  created_at      timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.trip_bookings (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id       text NOT NULL,
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  type          text NOT NULL,
  category      text,
  title         text NOT NULL,
  description   text,
  details       text,
  status        text DEFAULT 'Planned',
  price         text,
  price_unit    text,
  file_gcs_path text,
  file_gcs_url  text,
  has_file      boolean DEFAULT false,
  attachment_name text,
  created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.trip_documents (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id    text NOT NULL,
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name       text NOT NULL,
  category   text NOT NULL,
  file_name  text NOT NULL,
  gcs_path   text NOT NULL,
  gcs_url    text NOT NULL,
  status     text DEFAULT 'pending',
  created_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.trip_reels (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id       text NOT NULL,
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  title         text NOT NULL,
  category      text DEFAULT 'Memory',
  gcs_path      text NOT NULL,
  gcs_url       text NOT NULL,
  thumbnail_url text,
  duration      text,
  status        text DEFAULT 'pending',
  created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.reel_scans (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id       text UNIQUE NOT NULL,
  reel_url     text NOT NULL,
  caption      text,
  video_url    text,
  frame_urls   text[] DEFAULT '{}',
  result       jsonb,
  status       text NOT NULL DEFAULT 'queued',
  created_at   timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS public.reel_jobs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id        text,                          -- pipeline job id (job_<ULID>), shared across users on cache hits
  source_url    text,
  source_title  text,
  title         text,
  thumbnail_url text,
  video_url     text,
  status        text NOT NULL DEFAULT 'pending',
  progress      integer DEFAULT 0,
  error_message text,
  created_at    timestamptz DEFAULT now(),
  updated_at    timestamptz DEFAULT now(),
  UNIQUE (user_id, job_id)
);
CREATE INDEX IF NOT EXISTS reel_jobs_job_id_idx ON public.reel_jobs (job_id);

-- Migration for deployments that created reel_jobs before the pipeline wrote to it:
--   ALTER TABLE public.reel_jobs ADD COLUMN IF NOT EXISTS job_id text;
--   ALTER TABLE public.reel_jobs ADD COLUMN IF NOT EXISTS video_url text;
--   ALTER TABLE public.reel_jobs ADD COLUMN IF NOT EXISTS error_message text;
--   ALTER TABLE public.reel_jobs ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();
--   ALTER TABLE public.reel_jobs ADD CONSTRAINT reel_jobs_user_job_unique UNIQUE (user_id, job_id);
--   CREATE INDEX IF NOT EXISTS reel_jobs_job_id_idx ON public.reel_jobs (job_id);

CREATE TABLE IF NOT EXISTS public.saved_reels (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id        text,
  title         text NOT NULL,
  category      text NOT NULL,
  thumbnail_url text DEFAULT '',
  video_url     text DEFAULT '',
  description   text DEFAULT '',
  vibes         text[] DEFAULT '{}',
  is_liked      boolean DEFAULT false,
  created_at    timestamptz DEFAULT now()
);

-- Migration for existing deployments:
--   ALTER TABLE public.saved_reels ADD COLUMN IF NOT EXISTS video_url text DEFAULT '';

-- Saved-library reels attached to a trip (applied 2026-06-12 as migration
-- trip_saved_reels_attachments). trip_id is text to match trip_reels.
CREATE TABLE IF NOT EXISTS public.trip_saved_reels (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  trip_id    text NOT NULL,
  reel_id    uuid NOT NULL REFERENCES public.saved_reels(id) ON DELETE CASCADE,
  created_at timestamptz DEFAULT now(),
  UNIQUE (user_id, trip_id, reel_id)
);
CREATE INDEX IF NOT EXISTS trip_saved_reels_user_trip_idx ON public.trip_saved_reels (user_id, trip_id);

CREATE TABLE IF NOT EXISTS public.trip_journey (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id                 uuid NOT NULL REFERENCES public.trips(id) ON DELETE CASCADE,
  user_id                 uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,

  origin                  text,
  destination             text,
  departure_date          date,
  return_date             date,
  num_days                integer,
  travelers               integer,
  budget                  text,
  stay_nights             integer,

  going                   jsonb DEFAULT '{}',
  return_journey          jsonb DEFAULT '{}',

  journey_summary         text,
  total_distance_km       numeric,
  total_duration_approx   text,

  total_estimated_cost    numeric,
  cost_per_person         numeric,
  cost_for_all_travelers  numeric,
  cost_breakdown          jsonb DEFAULT '{}',
  budget_breakdown        jsonb DEFAULT '{}',

  best_time_to_depart     text,
  packing_tips            jsonb DEFAULT '[]',
  is_international        boolean DEFAULT false,
  visa_required           boolean DEFAULT false,
  visa_info               text,
  documents_needed        jsonb DEFAULT '[]',
  health_requirements     jsonb DEFAULT '[]',
  currency_info           jsonb DEFAULT '{}',

  created_at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trip_journey_trip_id_idx ON public.trip_journey(trip_id);
CREATE INDEX IF NOT EXISTS trip_journey_user_id_idx ON public.trip_journey(user_id);

CREATE TABLE IF NOT EXISTS public.saved_folders (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  name        text NOT NULL,
  emoji       text NOT NULL DEFAULT '📁',
  item_type   text NOT NULL DEFAULT 'mixed' CHECK (item_type IN ('trips','nights','reels','mixed')),
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.folder_items (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  folder_id      uuid NOT NULL REFERENCES public.saved_folders(id) ON DELETE CASCADE,
  item_id        text NOT NULL,
  item_type      text NOT NULL CHECK (item_type IN ('trip','night','reel')),
  item_snapshot  jsonb NOT NULL DEFAULT '{}',
  added_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (folder_id, item_id, item_type)
);

CREATE INDEX IF NOT EXISTS saved_folders_user_idx ON public.saved_folders(user_id);
CREATE INDEX IF NOT EXISTS folder_items_folder_idx ON public.folder_items(folder_id);

CREATE TABLE IF NOT EXISTS public.user_home_feed (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  feed_type    text NOT NULL,
  items        jsonb NOT NULL DEFAULT '[]',
  generated_at timestamptz DEFAULT now()
);

-- ============================================================
-- AUTH TRIGGERS
-- ============================================================

-- Auto-create a profiles row whenever a new user signs up via Supabase Auth.
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
  INSERT INTO public.profiles (id, email, full_name, avatar_url)
  VALUES (
    new.id,
    new.email,
    new.raw_user_meta_data->>'full_name',
    new.raw_user_meta_data->>'avatar_url'
  )
  ON CONFLICT (id) DO NOTHING;
  RETURN new;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- ============================================================
-- ROW LEVEL SECURITY
-- ============================================================

ALTER TABLE public.profiles         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trips             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.internal_trip_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_groups       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.group_members     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.group_messages    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.group_expenses    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_bookings     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_documents    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_reels        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reel_scans        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.reel_jobs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.saved_reels       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_saved_reels  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_journey       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.saved_folders      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.folder_items       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_home_feed    ENABLE ROW LEVEL SECURITY;

-- profiles
CREATE POLICY "profiles_read_all"   ON public.profiles FOR SELECT USING (true);
CREATE POLICY "profiles_insert_own" ON public.profiles FOR INSERT WITH CHECK (auth.uid() = id);
CREATE POLICY "profiles_update_own" ON public.profiles FOR UPDATE USING (auth.uid() = id);
CREATE POLICY "profiles_admin_all"  ON public.profiles FOR ALL USING (is_admin());

-- trips
CREATE POLICY "trips_crud_own"    ON public.trips FOR ALL    USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "trips_public_read" ON public.trips FOR SELECT USING (is_public = true);

-- internal_trip_logs
CREATE POLICY "logs_system_insert" ON public.internal_trip_logs FOR INSERT WITH CHECK (true);
CREATE POLICY "logs_public_read"   ON public.internal_trip_logs FOR SELECT USING (is_public = true);
CREATE POLICY "logs_admin_read"    ON public.internal_trip_logs FOR SELECT USING (is_admin());

-- trip_groups
CREATE POLICY "trip_groups_owner_all"    ON public.trip_groups FOR ALL    USING (auth.uid() = created_by) WITH CHECK (auth.uid() = created_by);
CREATE POLICY "trip_groups_member_read"  ON public.trip_groups FOR SELECT USING (is_group_member(id));

-- group_members
CREATE POLICY "group_members_read"   ON public.group_members FOR SELECT USING (user_id = auth.uid() OR is_group_member(group_id));
CREATE POLICY "group_members_insert" ON public.group_members FOR INSERT WITH CHECK (auth.uid() = user_id OR is_group_admin(group_id));
CREATE POLICY "group_members_delete" ON public.group_members FOR DELETE USING (user_id = auth.uid() OR is_group_admin(group_id));

-- group_messages
CREATE POLICY "group_messages_member_all" ON public.group_messages FOR ALL USING (is_group_member(group_id)) WITH CHECK (is_group_member(group_id));

-- group_expenses
CREATE POLICY "group_expenses_member_all" ON public.group_expenses FOR ALL USING (is_group_member(group_id)) WITH CHECK (is_group_member(group_id));

-- trip_bookings
CREATE POLICY "trip_bookings_own" ON public.trip_bookings FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- trip_documents
CREATE POLICY "trip_documents_own" ON public.trip_documents FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- trip_reels
CREATE POLICY "trip_reels_own" ON public.trip_reels FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- reel_scans
CREATE POLICY "reel_scans_own" ON public.reel_scans FOR ALL USING (auth.uid() = user_id);

-- reel_jobs
CREATE POLICY "reel_jobs_own" ON public.reel_jobs FOR ALL USING (auth.uid() = user_id);

-- saved_reels
CREATE POLICY "saved_reels_own" ON public.saved_reels FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- trip_saved_reels
CREATE POLICY "trip_saved_reels_own" ON public.trip_saved_reels FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- trip_journey
CREATE POLICY "trip_journey_crud_own" ON public.trip_journey FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- saved_folders
CREATE POLICY "saved_folders_own" ON public.saved_folders FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- folder_items
CREATE POLICY "folder_items_own" ON public.folder_items FOR ALL USING (EXISTS (SELECT 1 FROM public.saved_folders WHERE id = folder_items.folder_id AND user_id = auth.uid()));

-- user_home_feed
CREATE POLICY "feed_user_read"  ON public.user_home_feed FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "feed_service_all" ON public.user_home_feed FOR ALL USING (auth.role() = 'service_role');

-- ══════════════════════════════════════════════════════════════════════════════
-- Trip State V2 — persistent TripState object + append-only patch log
-- (Travel-OS architecture: workspace state, undo, version history, collab)
-- ══════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.trip_states (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid REFERENCES auth.users(id) ON DELETE CASCADE,
  title          text,
  origin         text,
  destination    text,
  status         text NOT NULL DEFAULT 'planning',   -- planning | ready | error
  visibility     text NOT NULL DEFAULT 'private',    -- private | link | public
  schema_version integer NOT NULL DEFAULT 1,
  state          jsonb NOT NULL DEFAULT '{}',        -- current TripState snapshot
  last_seq       integer NOT NULL DEFAULT 0,         -- seq of last applied event
  created_at     timestamptz NOT NULL DEFAULT timezone('utc', now()),
  updated_at     timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE TABLE IF NOT EXISTS public.trip_state_events (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trip_id    uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  seq        integer NOT NULL,                       -- per-trip ordering
  actor      text NOT NULL,                          -- user | agent:<name> | supervisor
  phase      text,                                   -- generation phase / edit label
  patches    jsonb NOT NULL DEFAULT '[]',            -- [PatchOp, ...]
  reasoning  text,                                   -- agent explanation (optional)
  created_at timestamptz NOT NULL DEFAULT timezone('utc', now()),
  UNIQUE (trip_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_trip_states_user     ON public.trip_states(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_trip_state_events_trip ON public.trip_state_events(trip_id, seq);

ALTER TABLE public.trip_states       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.trip_state_events ENABLE ROW LEVEL SECURITY;

-- Owner CRUD; public/link trips readable by anyone (share/clone flows)
CREATE POLICY "trip_states_own"    ON public.trip_states FOR ALL
  USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY "trip_states_shared" ON public.trip_states FOR SELECT
  USING (visibility IN ('link', 'public'));

CREATE POLICY "trip_state_events_own" ON public.trip_state_events FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = trip_state_events.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link', 'public'))));
-- Writes go through the service role only (backend orchestrator)


-- ── Copilot chat transcript ──────────────────────────────────────────────────
-- Durable, linear per-trip conversation log. Decoupled from trip_state_events
-- (which carries state PATCHES + CAS/snapshot semantics): chat is an append-only
-- transcript that must survive a page refresh and a Redis flush. Every copilot
-- turn — user message AND assistant reply, including pure Q&A that changes no
-- state — is recorded here so the thread rehydrates on load.
CREATE TABLE IF NOT EXISTS public.copilot_messages (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  trip_id    uuid NOT NULL REFERENCES public.trip_states(id) ON DELETE CASCADE,
  role       text NOT NULL,                          -- user | assistant
  content    text NOT NULL,
  intent     text,                                   -- classified intent (assistant turns)
  meta       jsonb NOT NULL DEFAULT '{}',            -- clarify options, applied patch count, …
  created_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

CREATE INDEX IF NOT EXISTS idx_copilot_messages_trip ON public.copilot_messages(trip_id, id);

ALTER TABLE public.copilot_messages ENABLE ROW LEVEL SECURITY;

-- Readable by the trip owner + anyone the trip is shared with (mirrors events)
CREATE POLICY "copilot_messages_own" ON public.copilot_messages FOR SELECT
  USING (EXISTS (SELECT 1 FROM public.trip_states t
                 WHERE t.id = copilot_messages.trip_id
                   AND (t.user_id = auth.uid() OR t.visibility IN ('link', 'public'))));
-- Writes go through the service role only (backend copilot)


-- ── Per-user travel preference memory ────────────────────────────────────────
-- Lightweight, learned profile the agents read to warm-start planning +
-- recommendations + copilot. Distilled from a user's completed trips and their
-- copilot edits (budget sensitivity, preferred stay types, pace, transport,
-- dietary, interests). One row per user; the backend merges into `memory`.
CREATE TABLE IF NOT EXISTS public.user_travel_memory (
  user_id    uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  memory     jsonb NOT NULL DEFAULT '{}',
  updated_at timestamptz NOT NULL DEFAULT timezone('utc', now())
);

ALTER TABLE public.user_travel_memory ENABLE ROW LEVEL SECURITY;

CREATE POLICY "user_travel_memory_own" ON public.user_travel_memory FOR SELECT
  USING (auth.uid() = user_id);
-- Writes go through the service role only (backend distiller)
