# Safar

An AI trip planner and travel backend that runs **completely free**: free open
LLMs instead of paid Gemini, free hosting, no credit card, no GCP project.

Safar is a full rebrand and free-stack rebuild of the TravBuddy Backend, which
Sahil Verma open-sourced after shutting the product down (see
[NOTICE.md](./NOTICE.md) for attribution and the exact reuse basis).

## What it does

- **AI Trip Planner & TripDoc (`/api/v1/plan`, `/api/v3/trips`)** — multi-city
  trip planning with transport, hotels and day-wise itineraries, budget-aware
  supervisor interventions and confidence scoring. The LLM calls run on free
  open models (default: OpenRouter's free tier) instead of Vertex AI Gemini.
- **Live Travel Search & Recommendations (`/api/v1/search`, `/api/v1/recommend`)** —
  live inventory from Booking.com, Agoda, EaseMyTrip, Ixigo, goSTOPS, RedBus,
  FlixBus and Indian Railways, with deterministic scoring and optional
  semantic re-ranking.
- **Instagram Reel → Location Timeline (`/api/v1/reels`)** — video-to-location
  pipeline (yt-dlp → scene detection → keyframes → vision model → timeline),
  with the vision step on a free vision-capable model.
- **Marketplace, Trips & Community (`/api/v1/marketplace`, `/api/v1/trips`,
  `/api/v1/groups`)** — partner listings, availability matching, saved trips
  and travel groups. Razorpay checkout stays optional (unconfigured by default).
- **StaySuite — Hotel PMS & AI Guest Messaging (`ENABLE_HOTEL_OS=true`)** —
  optional second deployment mode for property management and automated guest
  messaging. Off by default.
- **Web UI (`/`)** — a branded single-page trip planner served by the API.

## The free stack

| Piece | Upstream (paid/keyed) | Safar (free) |
| --- | --- | --- |
| LLM (planning, extraction, agents) | Vertex AI Gemini | Free OpenAI-compatible models via `LLM_BACKEND=openrouter` |
| Vision (reel pipeline) | Gemini Vision on Vertex | Free vision-capable model, same batch contract |
| Embeddings (reco re-rank) | Vertex `text-embedding-005` | Optional — degrades gracefully to deterministic scoring |
| Database + auth | Supabase | Supabase free tier (unchanged) |
| Queue/cache | Redis + Celery | Any free Redis (e.g. Upstash); optional for the web tier |
| Hosting | GCP VM | One free Docker web service (Render / Hugging Face Spaces) |
| Error tracking | Sentry | Disabled by default |

Everything else — Google Places, GCS, Razorpay, WhatsApp/Instagram messaging —
was optional in the upstream codebase and stays optional env-driven
configuration here.

## Quick start (local)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set LLM_BACKEND=openrouter, OPENROUTER_API_KEY, Supabase
uvicorn app.main:app --reload
```

Open http://localhost:8000 for the UI, http://localhost:8000/api/v1/health for
the health check.

## Deploy (free, no card)

See [DEPLOYING_FREE.md](./DEPLOYING_FREE.md).

## Configuration

All settings live in `app/config.py` with env overrides; see `.env.example`.
The free-LLM block:

```env
LLM_BACKEND=openrouter
OPENROUTER_API_KEY=sk-or-...          # https://openrouter.ai/keys (free)
OPENROUTER_MODEL_BRAIN=google/gemma-4-31b-it:free
OPENROUTER_MODEL_MUSCLE=google/gemma-4-26b-a4b-it:free
OPENROUTER_MODEL_VISION=google/gemma-4-31b-it:free
OPENROUTER_FALLBACK_MODELS=nvidia/nemotron-3.5-super-vl:free,openrouter/free
APP_BRAND_NAME=Safar
```

Free models are shared, rate-limited pools: the transport rotates through the
fallback list on 429s (`app/services/freellm.py`). Set `LLM_BACKEND=vertex`
with the GCP variables from `.env.example` to use the original paid Gemini
path instead — it is untouched.

## Repo layout

- `app/` — FastAPI application (API v1 + v3, pipeline, services, workers)
- `static/` — the single-page UI served at `/`
- `schema.sql`, `migrations/`, `.seed/` — Supabase schema and seed data
- `docs/` — upstream architecture docs (original TravBuddy writing, kept for reference)
- `tests/` — the upstream test suite
