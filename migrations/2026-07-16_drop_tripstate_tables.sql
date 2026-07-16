-- TripState teardown (docs/tripstate_removal_plan.md §5).
-- APPLIED 2026-07-16 (no live users; all 46 trips converted to TripDocs —
-- incl. one straggler re-converted with hydration from the pre-deletion
-- commit right before the drop).
--
-- NOTE: the old deployed backend 500s on generation until the TripDoc deploy
-- lands — accepted (zero users), deploy follows immediately.
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
