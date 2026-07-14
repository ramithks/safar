# TravBuddy Backend — Run Commands

Every command you'll actually run, what it does, and when to use it. Run all of
these from the repo root with the virtualenv active unless noted.

```bash
source venv/bin/activate     # activate the venv (first thing, every terminal)
```

---

## 1. First-time setup

| Command | What it does |
|---|---|
| `python3 -m venv venv` | Create the virtualenv (once). |
| `source venv/bin/activate` | Activate it. Needed in **every** new terminal. |
| `pip install -r requirements.txt` | Install all dependencies. |
| `cp .env.example .env` | Create your env file, then fill in the secrets (see [Environment](#environment)). |

---

## 2. Run the app (local dev)

You need **three processes** for the full system: the API, a Redis, and a Celery
worker. The Celery **beat** scheduler is a fourth, only needed for the nightly
recommendation jobs.

| Command | What it does | When |
|---|---|---|
| `uvicorn app.main:app --reload` | Start the FastAPI server on `http://localhost:8000`. `--reload` restarts on code changes. | Always (main dev process). |
| `redis-server` | Local Redis — the Celery broker + the recommendation cache / session-bias store. (Skip if you already run Redis via Docker.) | Always (worker + reco cache need it). |
| `celery -A app.workers.celery_app.celery_app worker --loglevel=info` | Start the background worker: reel pipeline jobs **and** the reco embed/profile tasks. | When you need reels or reco background work. |
| `celery -A app.workers.celery_app.celery_app beat --loglevel=info` | Start the scheduler that fires the **nightly/weekly reco jobs** (expected-price, destination-priors, fit-centroids). Run **exactly one** beat instance. | To keep reco aggregates fresh. |

- **API docs (Swagger):** `http://localhost:8000/docs`
- If you need to run an interactive login (e.g. `gcloud auth login`) inside a
  Claude Code session, prefix it with `!` in the prompt.

---

## 3. Recommendation engine — bootstrap & backfill

One script drives the whole recommendation data layer:
`scripts/reco_bootstrap.py`. It is **safe by default** (no flags = check only) and
**resumable** (a re-run skips hotels whose data hasn't changed via the doc-hash
gate). See `docs/recommendation_engine.md` for the design.

> Prerequisite: apply `migrations/2026-07-13_reco_engine.sql` once in the Supabase
> SQL editor (creates the vector tables + enables pgvector). `--check` verifies it.

| Command | What it does |
|---|---|
| `python scripts/reco_bootstrap.py --check` | Verify the migration is applied (all reco tables + `hotels.slug` + the `match_hotels` pgvector RPC). Read-only. **Run this first.** |
| `python scripts/reco_bootstrap.py --seed-centroids` | Seed the 24 personality "centroids" from text anchors so hotel clustering works before any hotel is hand-labelled. One-time (re-run only if you change the anchors). |
| `python scripts/reco_bootstrap.py --backfill --seo` | The big one: for **every** hotel, generate its 384-dim embedding + personality tags + LLM `one_liner`/`seo_summary` (the SEO pages). ~8s/hotel, resumable. Drop `--seo` for tags+embeddings only (cheaper, no LLM prose). |
| `... --backfill --seo --limit 30 --destination Udaipur` | Same, but scoped — great for a quick test run before the full backfill. |
| `... --backfill --dry-run` | Show what would be processed, write nothing. |

Typical first run, in order:
```bash
python scripts/reco_bootstrap.py --check
python scripts/reco_bootstrap.py --seed-centroids
python scripts/reco_bootstrap.py --backfill --seo --limit 30 --destination Udaipur   # sanity check
python scripts/reco_bootstrap.py --backfill --seo                                     # full run (hours)
```

Run the full backfill detached so it survives the terminal closing:
```bash
nohup python -u scripts/reco_bootstrap.py --backfill --seo > reco_backfill.log 2>&1 &
tail -f reco_backfill.log      # watch progress
```

---

## 4. Tests

| Command | What it does |
|---|---|
| `pytest -q` | Run the whole suite (quiet). |
| `pytest tests/test_reco_engine.py -v` | Just the recommendation-engine unit tests. |
| `pytest tests/test_scoring.py tests/test_hotel_dedup.py -v` | Ranking + cross-provider dedup guards. |
| `python -m pyflakes app/reco/ app/api/v1/recommend/` | Quick dead-import / unused-name lint on the reco code. |

> Note: `tests/test_save_trip.py` + `tests/test_pipeline.py` have pre-existing
> failures (a stale `app.api.v1.save_trip` patch path) unrelated to the reco work.

---

## 5. Diagnostic / data scripts

| Command | What it does |
|---|---|
| `python scripts/smoke_test.py` | Quick end-to-end health check of the core endpoints. |
| `python scripts/dump_consolidated.py` | Call every hotel + transport scraper for a sample city/route, write `scraper_dumps/hotels_result.json` + `transport_result.json` for eyeballing data quality. Env-configurable: `HOTEL_CITY=Jaipur ORIGIN=Mumbai DEST=Goa python scripts/dump_consolidated.py`. |
| `python scripts/dump_scraper_responses.py` | Same idea, but one raw file per provider (`scraper_dumps/{provider}_{mode}.json`). |
| `python scripts/scrape_gostops.py` | Refresh the goSTOPS hostels master table. |

---

## 6. Docker / production (Coolify)

`docker-compose.yml` defines four services: `redis`, `api`, `worker`, `beat`.

| Command | What it does |
|---|---|
| `docker compose up -d` | Start everything (Redis + API + worker + beat). |
| `docker compose up -d --build` | Rebuild images then start (after code changes). |
| `docker compose logs -f api` | Tail the API logs (swap `api`→`worker`/`beat`). |
| `docker compose down` | Stop everything. |

In production this is deployed via the **Coolify UI** (port 8000). The `beat`
service must run as a **single instance** — never scale it above 1, or the nightly
jobs double-fire.

---

## Environment

Set these in `.env` (local) or the Coolify UI (prod). Full list in
`.env.example`; the ones you can't run without:

| Variable | Purpose |
|---|---|
| `SUPABASE_URL` | Supabase project URL. |
| `SUPABASE_SERVICE_KEY` | Service-role key — server-side DB writes, bypasses RLS. |
| `SUPABASE_ANON_KEY` | Anon key — verifies user JWTs. |
| `GCP_PROJECT` | Vertex AI project ID (Gemini + embeddings). |
| `GCP_LOCATION` | Vertex region (default `us-central1`). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service-account JSON string for Vertex auth. |
| `REDIS_URL` | Redis for Celery + reco cache (default `redis://localhost:6379/0`). |
| `GCS_BUCKET_NAME` | GCS bucket for reel video/frame storage. |
| `CELERY_WORKER_CONCURRENCY` | Worker parallelism (default 8). |

Recommendation-engine knobs (sane defaults, override only if needed):
`VERTEX_MODEL_EMBEDDING` (`text-embedding-005`), `EMBEDDING_DIM` (`384`),
`VERTEX_MODEL_BRAIN` / `VERTEX_MODEL_MUSCLE`.
