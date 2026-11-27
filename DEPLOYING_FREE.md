# Deploying Safar free (Rs 0, no card)

One Docker web service + free Supabase + free Redis + free LLM key.
Everything below is a free tier with no card required.

## 1. OpenRouter key (free LLM access)

1. Create a key at https://openrouter.ai/keys (free account).
2. The app defaults to `:free` models — hard-zero spend. Do not add credits;
   paid models are never configured, so nothing can bill.

## 2. Supabase (database + auth, free tier)

1. Create a project at https://supabase.com (GitHub sign-in works).
2. In the SQL editor, run `schema.sql`, then the files in `migrations/` in
   order, then (optionally) the seed data in `.seed/`.
3. From Settings → API, copy: project URL, anon key, service_role key, and
   JWT secret.

## 3. Redis (optional, for the reel pipeline + background jobs)

1. Create a free database at https://upstash.com (GitHub sign-in works).
2. Copy the `rediss://...` TLS URL. Without it the web tier still serves the
   trip planner; reels and scheduled recommendation jobs queue nowhere.

## 4. Web service (Render free, Docker)

1. New → Web Service → the GitHub repo → Docker runtime.
2. Free instance type. No disk needed.
3. Environment:

| Key | Value |
| --- | --- |
| `LLM_BACKEND` | `openrouter` |
| `OPENROUTER_API_KEY` | from step 1 |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` / `SUPABASE_SERVICE_KEY` / `SUPABASE_JWT_SECRET` | from step 2 |
| `REDIS_URL` | from step 3 (optional) |
| `RUN_WORKER` | `true` only if `REDIS_URL` is set |
| `APP_ENV` | `production` |
| `APP_BRAND_NAME` | `Safar` |

4. Deploy. `https://<service>.onrender.com/` is the UI,
   `/api/v1/health` the health check.

Free instances sleep when idle; the first request after a nap takes ~30 s.

## What is intentionally off

- Vertex AI / Gemini (`LLM_BACKEND=vertex` brings it back, with GCP costs)
- Vertex embeddings → semantic re-ranking silently degrades to deterministic
  scoring (the recommendation engine's documented best-effort path)
- Sentry, Google Places, GCS, Razorpay, WhatsApp/Instagram messaging,
  StaySuite hotel OS (`ENABLE_HOTEL_OS`) — all optional upstream and all left
  unconfigured here
