-- TripState teardown — DEFERRED (docs/tripstate_removal_plan.md §5).
--
-- ⚠️  APPLY ONLY AFTER the TripDoc backend + website are deployed and soaked:
-- the old deployed code writes trip_state_events until then, and dropping
-- these tables under it would 500 every live generation.
--
-- What stays: trip_states (now the TripDoc home: id/user_id/title/status/
-- visibility/progress/doc), trip_snapshots (pre-regeneration archive — the
-- one piece of the integrity layer kept).

-- The event log + copilot transcript (no readers since the TripDoc cutover)
DROP TABLE IF EXISTS public.trip_state_events;
DROP TABLE IF EXISTS public.copilot_messages;

-- The search store + personalization layer (docs are self-contained; reco
-- training reads transport_options, fed directly by services/transport.py)
DROP TABLE IF EXISTS public.trip_result_views;
DROP TABLE IF EXISTS public.trip_recommendations;
DROP TABLE IF EXISTS public.trip_memory;
DROP TABLE IF EXISTS public.search_results;
DROP TABLE IF EXISTS public.search_batches;

-- Legacy columns on the surviving row (state → doc happened via backfill)
ALTER TABLE public.trip_states
  DROP COLUMN IF EXISTS state,
  DROP COLUMN IF EXISTS last_seq,
  DROP COLUMN IF EXISTS schema_version;
