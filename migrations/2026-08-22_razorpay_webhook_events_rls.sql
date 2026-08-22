-- The payment webhook log is readable with the publishable anon key.
--
-- APPLIED 2026-08-22 as migration `the_payment_webhook_log_stops_being_public`,
-- after the exposure below was confirmed live and with the user's go-ahead.
-- Re-verified afterwards: anon SELECT and DELETE both 401, and the service-role
-- writer still claims, deduplicates a replay, marks and reads back.
--
-- VERIFIED LIVE, 2026-08-22, against rdlozqvuywtgsodunait:
--
--     curl "$SUPABASE_URL/rest/v1/razorpay_webhook_events?select=*" -H "apikey: $ANON"
--     → 200, 15 rows
--
-- `razorpay_webhook_events` is the one table in this schema with
-- `relrowsecurity = false`, and `anon` and `authenticated` hold
-- SELECT/INSERT/UPDATE/DELETE/TRUNCATE on it. The anon key is in every browser
-- that loads the partner app, so every Razorpay event this system has ever
-- received — the whole `payload` jsonb, subscription ids, payment ids, order
-- ids — is readable by anybody who has opened the app, and deletable by them
-- too. That log is the only record of what Razorpay actually sent, and by the
-- time anybody needs it the event is gone from Razorpay's dashboard.
--
-- WHY THIS IS SAFE TO APPLY:
--
--   * The only writer is `app/services/pms/webhook_log.py`, through `get_db()`,
--     which is the service-role client. Service role bypasses both RLS and
--     these grants, so the webhook path is untouched.
--   * Nothing reads this table from a browser. There is no screen for it.
--   * The e2e suites clean up through the management API (`sql(...)`), not
--     through PostgREST, so they are unaffected.
--
-- Revocation is the whole fix; the RLS enable is belt-and-braces for any future
-- grant somebody adds by habit. There is deliberately no policy: nobody who
-- reaches this table through PostgREST should see a row.

revoke all on public.razorpay_webhook_events from anon, authenticated;

alter table public.razorpay_webhook_events enable row level security;
alter table public.razorpay_webhook_events force row level security;

-- To undo:
--   grant select, insert, update, delete on public.razorpay_webhook_events
--     to anon, authenticated;
--   alter table public.razorpay_webhook_events no force row level security;
--   alter table public.razorpay_webhook_events disable row level security;
