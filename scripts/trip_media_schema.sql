-- ─── TravBuddy: Trip Media Schema ────────────────────────────────────────────
-- Run in Supabase SQL Editor after groups_schema.sql

-- ── Trip Reels (user-uploaded video memories) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS trip_reels (
    id            uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    trip_id       text NOT NULL,
    user_id       uuid NOT NULL,
    title         text NOT NULL,
    category      text DEFAULT 'Memory',
    gcs_path      text NOT NULL,        -- e.g. trips/{trip_id}/reels/{id}/clip.mp4
    gcs_url       text NOT NULL,        -- https://storage.googleapis.com/...
    thumbnail_url text,                 -- optional poster image URL
    duration      text,                 -- e.g. "0:45"
    status        text DEFAULT 'pending',  -- 'pending' | 'uploaded'
    created_at    timestamptz DEFAULT now()
);

-- ── Trip Bookings (flight / hotel / cab / activity confirmations) ─────────────
CREATE TABLE IF NOT EXISTS trip_bookings (
    id              uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    trip_id         text NOT NULL,
    user_id         uuid NOT NULL,
    type            text NOT NULL,      -- 'flight' | 'train' | 'bus' | 'hotel' | 'cab' | 'activity'
    category        text,               -- 'Flight' | 'Transport' | 'Stay' | 'Activity'
    title           text NOT NULL,
    description     text,
    details         text,               -- "15 Jun • 17:00 - 19:30 • Direct"
    status          text DEFAULT 'Planned',  -- 'Confirmed' | 'Pending' | 'Planned'
    price           text,
    price_unit      text,
    file_gcs_path   text,               -- optional confirmation file in GCS
    file_gcs_url    text,
    has_file        boolean DEFAULT false,
    created_at      timestamptz DEFAULT now()
);

-- ── Trip Documents (passport, visa, aadhar, etc.) ─────────────────────────────
CREATE TABLE IF NOT EXISTS trip_documents (
    id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    trip_id     text NOT NULL,
    user_id     uuid NOT NULL,
    name        text NOT NULL,          -- e.g. "Passport Front & Back"
    category    text NOT NULL,          -- 'Passport' | 'Aadhar Card' | 'Visa' | 'Driving License' | 'Other'
    file_name   text NOT NULL,          -- original filename
    gcs_path    text NOT NULL,          -- GCS blob path
    gcs_url     text NOT NULL,          -- public HTTPS URL
    status      text DEFAULT 'pending', -- 'pending' | 'uploaded'
    created_at  timestamptz DEFAULT now()
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS trip_reels_trip_user_idx    ON trip_reels(trip_id, user_id);
CREATE INDEX IF NOT EXISTS trip_bookings_trip_user_idx ON trip_bookings(trip_id, user_id);
CREATE INDEX IF NOT EXISTS trip_documents_trip_user_idx ON trip_documents(trip_id, user_id);

-- ── RLS ───────────────────────────────────────────────────────────────────────
ALTER TABLE trip_reels     ENABLE ROW LEVEL SECURITY;
ALTER TABLE trip_bookings  ENABLE ROW LEVEL SECURITY;
ALTER TABLE trip_documents ENABLE ROW LEVEL SECURITY;

-- ── GCS folder structure (reference only, not SQL) ────────────────────────────
-- trips/{trip_id}/reels/{reel_id}/{filename}
-- trips/{trip_id}/bookings/{booking_id}/{filename}
-- trips/{trip_id}/documents/{doc_id}/{filename}
