-- ============================================================================
-- Safar Growth OS — group_trips: DB-driven group-trip pages
-- Idempotent: safe to re-run.
--
-- One row = one sellable group trip (the /trips/{slug} template page).
-- Add or edit trips directly in the Supabase dashboard — no code deploys.
-- `content` carries the page sections (images, moments, route, days, pricing,
-- hotel_photos, reviews, people, inclusions, fine_print, group_types) in the
-- exact shapes the template component renders.
-- Payments (Razorpay) come later — `price_from`/pricing are display-only now.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.group_trips (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug            text NOT NULL UNIQUE,          -- /trips/{slug}
  title           text NOT NULL,
  seo_title       text,
  description     text,                          -- meta description
  destination     text,
  origin          text,
  duration_label  text,                          -- "5 Days 4 Nights"
  duration_days   int,
  departure_date  date,
  departure_label text,                          -- "7 August 2026"
  seats_total     int,
  seats_left      int,
  price_from      int,                           -- ₹, cheapest tier (rails + cards)
  whatsapp_number text,
  hero_image      text,
  content         jsonb NOT NULL DEFAULT '{}'::jsonb,
  is_active       boolean NOT NULL DEFAULT true,
  sort            int NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT timezone('utc', now()),
  updated_at      timestamptz NOT NULL DEFAULT timezone('utc', now())
);
CREATE INDEX IF NOT EXISTS idx_group_trips_active
  ON public.group_trips (is_active, sort, departure_date);

ALTER TABLE public.group_trips ENABLE ROW LEVEL SECURITY;

-- Public marketing content: anyone can read ACTIVE trips (the website reads
-- with the anon key); writes are dashboard/service-role only.
DROP POLICY IF EXISTS group_trips_public_read ON public.group_trips;
drop policy if exists group_trips_public_read on public.group_trips;
create policy group_trips_public_read on public.group_trips
  FOR SELECT USING (is_active = true);

DROP POLICY IF EXISTS group_trips_service ON public.group_trips;
drop policy if exists group_trips_service on public.group_trips;
create policy group_trips_service on public.group_trips
  FOR ALL USING (auth.role() = 'service_role') WITH CHECK (auth.role() = 'service_role');
