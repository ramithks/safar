-- TripDoc migration (docs/tripstate_removal_plan.md §5, step 1)
--
-- Additive only: the new plain-JSON trip document lives in `doc` beside the
-- legacy `state` snapshot while both code paths coexist. After the frontend
-- flip + soak, a follow-up migration drops `state`, `last_seq`,
-- `schema_version` and the event/search-store tables.
--
-- `doc` carries a `last_seq_converted` marker: legacy writes bump `last_seq`,
-- so `last_seq > doc->>'last_seq_converted'` means the doc is stale and the
-- read path lazily re-converts from `state`.

ALTER TABLE public.trip_states
  ADD COLUMN IF NOT EXISTS doc jsonb,
  ADD COLUMN IF NOT EXISTS progress integer NOT NULL DEFAULT 0;

-- RLS: unchanged — `doc` rides the existing trip_states row policies
-- (owner CRUD + link/public SELECT).
