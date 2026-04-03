-- Run this in Supabase SQL Editor (supabase.com → project → SQL Editor)

CREATE TABLE IF NOT EXISTS jobs (
    job_id          text PRIMARY KEY,
    reel_url        text NOT NULL,
    status          text NOT NULL DEFAULT 'queued',
    video_gcs_url   text,
    result          jsonb,
    created_at      timestamptz DEFAULT now(),
    completed_at    timestamptz DEFAULT now()
);

-- Index for fast lookups by status
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);

-- Row Level Security (optional but recommended)
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
