-- ─── TravBuddy: Trip Groups Schema ──────────────────────────────────────────
-- Run in Supabase SQL Editor to create the group tables.

CREATE TABLE IF NOT EXISTS trip_groups (
    id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    trip_id     text,                                 -- optional plan reference
    name        text NOT NULL,
    destination text,
    created_by  uuid NOT NULL,
    invite_code text UNIQUE NOT NULL,
    created_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS group_members (
    id           uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id     uuid NOT NULL REFERENCES trip_groups(id) ON DELETE CASCADE,
    user_id      uuid NOT NULL,
    display_name text NOT NULL,
    role         text NOT NULL DEFAULT 'member',      -- 'leader' | 'member'
    joined_at    timestamptz DEFAULT now(),
    UNIQUE(group_id, user_id)
);

CREATE TABLE IF NOT EXISTS group_messages (
    id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id    uuid NOT NULL REFERENCES trip_groups(id) ON DELETE CASCADE,
    user_id     uuid,
    sender_name text NOT NULL,
    text        text NOT NULL,
    type        text NOT NULL DEFAULT 'text',         -- 'text' | 'poll' | 'ai'
    poll_data   jsonb,                                -- { question, options: [{id, label, votes[]}] }
    created_at  timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS group_expenses (
    id              uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    group_id        uuid NOT NULL REFERENCES trip_groups(id) ON DELETE CASCADE,
    title           text NOT NULL,
    amount          numeric(12, 2) NOT NULL,
    paid_by         text NOT NULL,                    -- display name of payer
    paid_by_user_id uuid,
    split_between   text[] NOT NULL DEFAULT '{}',
    category        text DEFAULT 'Other',
    date            text,                             -- human-readable e.g. "Jun 15"
    created_at      timestamptz DEFAULT now()
);

-- ─── Indexes ──────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS trip_groups_invite_code_idx  ON trip_groups(invite_code);
CREATE INDEX IF NOT EXISTS trip_groups_created_by_idx   ON trip_groups(created_by);
CREATE INDEX IF NOT EXISTS group_members_group_id_idx   ON group_members(group_id);
CREATE INDEX IF NOT EXISTS group_members_user_id_idx    ON group_members(user_id);
CREATE INDEX IF NOT EXISTS group_messages_group_id_idx  ON group_messages(group_id, created_at);
CREATE INDEX IF NOT EXISTS group_expenses_group_id_idx  ON group_expenses(group_id);

-- ─── RLS (optional — backend uses service_role so these are informational) ───
ALTER TABLE trip_groups    ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_members  ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_expenses ENABLE ROW LEVEL SECURITY;
