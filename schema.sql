-- TravBuddy — single source of truth
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
  source_url    text,
  source_title  text,
  title         text,
  thumbnail_url text,
  status        text NOT NULL DEFAULT 'pending',
  progress      integer DEFAULT 0,
  created_at    timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.saved_reels (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_id        text,
  title         text NOT NULL,
  category      text NOT NULL,
  thumbnail_url text DEFAULT '',
  description   text DEFAULT '',
  vibes         text[] DEFAULT '{}',
  is_liked      boolean DEFAULT false,
  created_at    timestamptz DEFAULT now()
);

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

-- trip_journey
CREATE POLICY "trip_journey_crud_own" ON public.trip_journey FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- saved_folders
CREATE POLICY "saved_folders_own" ON public.saved_folders FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- folder_items
CREATE POLICY "folder_items_own" ON public.folder_items FOR ALL USING (EXISTS (SELECT 1 FROM public.saved_folders WHERE id = folder_items.folder_id AND user_id = auth.uid()));

-- user_home_feed
CREATE POLICY "feed_user_read"  ON public.user_home_feed FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "feed_service_all" ON public.user_home_feed FOR ALL USING (auth.role() = 'service_role');
