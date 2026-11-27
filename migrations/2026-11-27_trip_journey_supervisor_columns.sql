-- trip_journey: columns the supervisor's save path writes that the reconstructed
-- schema lacked, so every trip_journey upsert was failing (trips saved, journey lost).
alter table public.trip_journey add column if not exists budget_status jsonb;
alter table public.trip_journey add column if not exists trip_validation jsonb;
alter table public.trip_journey add column if not exists supervisor_action text;
