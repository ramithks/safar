from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Vertex AI — Gemini 2.5 Flash via Vertex (uses ADC / service account)
    gcp_project: Optional[str] = None
    gcp_location: str = "us-central1"
    # Fallback regions tried (in order) when the primary region returns 429/503.
    # Each Vertex region has its own quota, so rotating spreads load and lets a
    # rate-limited request retry immediately against a fresh region instead of
    # sleeping through a long backoff. Comma-separated; primary is always first.
    gcp_locations: str = (
        "us-central1,us-east1,us-east4,us-east5,us-west1,us-west4,"
        "us-south1,northamerica-northeast1,europe-west1,europe-west4,asia-southeast1"
    )
    # Path to a service account JSON key; leave unset to use ADC
    google_application_credentials: Optional[str] = None

    # LLM gateway tiers — model IDs live in config ONLY (never hardcode at call
    # sites). "brain" handles reasoning-heavy calls (itinerary, journey tradeoffs,
    # re-ranking); "muscle" handles high-volume schema-bound transforms
    # (extraction, tagging, onboarding). Swap to gemini-3.5-flash /
    # gemini-3.1-flash-lite via env once regional availability is confirmed.
    vertex_model_brain: str = "gemini-2.5-flash"
    vertex_model_muscle: str = "gemini-2.5-flash-lite"

    # Embeddings for the recommendation engine (docs/recommendation_engine.md).
    # 384-dim is deliberate: ~4k hotels, not 100M docs — half the RAM/index, no
    # measurable recall loss at this scale.
    vertex_model_embedding: str = "text-embedding-005"
    embedding_dim: int = 384

    # Vertex fallback account — tried only when the primary project returns 429
    # (RESOURCE_EXHAUSTED). Lets a rate-limited Gemini call retry instantly against
    # a second project's independent quota. Both optional; omit to disable fallback.
    vertex_fallback_project: Optional[str] = None
    vertex_fallback_service_account_json: Optional[str] = None  # full SA JSON string

    # ── Hotel OS agent runtime (services/ai) ────────────────────────────────
    # OpenAI, not Vertex. The runtime is provider-agnostic — `providers.py`
    # dispatches on `agent_configs.provider` — but the hotel agents run on
    # OpenAI, and the Gemini tiers above stay where they are, serving the
    # traveller-side pipeline.
    #
    # Model ids live here and nowhere else, matching the rule above. `agent` is
    # the tool-calling loop: a guest turn is a short reasoning step over a
    # dozen tool schemas, run thousands of times a month, so the cheap tier is
    # the right default and the expensive one is the exception. `reason` is for
    # the escalations — a complaint being triaged, an owner asking why revenue
    # moved — where a wrong answer costs more than the tokens.
    openai_api_key: Optional[str] = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model_agent: str = "gpt-5-mini"
    openai_model_reason: str = "gpt-5.1"
    # Per request, not per turn. A guest waiting on WhatsApp will forgive thirty
    # seconds; a hung socket holding a worker is a different problem.
    openai_timeout_seconds: float = 30.0
    # How many times the model may call tools before the runtime stops it. Six
    # is generous for "look up the booking, raise a ticket, assign it"; a loop
    # that needs more is a loop that has stopped making progress.
    agent_max_tool_turns: int = 6

    # ── WhatsApp Cloud API ──────────────────────────────────────────────────
    # Credentials live here and nowhere else. `whatsapp_connections` holds a
    # `credentials_ref` naming which entry to use, because a token in a database
    # row is a token in every backup, replica and accidental `select *`.
    #
    # `app_secret` verifies Meta's X-Hub-Signature-256 on every inbound webhook;
    # `access_token` sends. They are separate secrets doing opposite jobs and
    # are never interchangeable.
    whatsapp_app_secret: Optional[str] = None
    whatsapp_access_token: Optional[str] = None
    # The string Meta echoes during the GET handshake. Ours to choose, and
    # compared in constant time — see services/messaging/whatsapp/webhook.py.
    whatsapp_verify_token: Optional[str] = None
    whatsapp_api_base: str = "https://graph.facebook.com/v21.0"
    whatsapp_timeout_seconds: float = 15.0

    # Connections whose `credentials_ref` is this one are wired to the
    # deterministic fake rather than to Meta. Named rather than inferred from a
    # hostname, so nothing can ship believing it is talking to Meta when it is
    # not.
    whatsapp_sandbox_ref: str = "sandbox"

    # Meta permits free-form replies only inside a service window that opens on
    # each inbound message. 24 hours is Meta's number, not ours; it is here so a
    # test can shorten it rather than sleep through it.
    whatsapp_service_window_hours: int = 24

    # Run the agent inline in the webhook request instead of handing it to
    # Celery. Development and the E2E suite only: a real deployment must
    # acknowledge Meta quickly and do the model call elsewhere.
    messaging_inline_processing: bool = False

    # Google Maps — Places API for place enrichment (optional but recommended)
    google_maps_api_key: Optional[str] = None

    # Pipeline tuning
    scene_detection_threshold: float = 27.0
    scene_min_length_frames: int = 15
    max_unique_frames: int = 10
    min_vision_confidence: float = 0.60
    frame_max_dimension: int = 1024
    frame_jpeg_quality: int = 85

    # Limits
    max_video_duration_seconds: int = 300
    max_video_size_mb: int = 200
    max_locations: int = 20

    # Cache
    places_cache_dir: str = "/tmp/travbuddy/places_cache"
    places_cache_ttl_seconds: int = 604800  # 7 days

    # Job queue
    redis_url: str = "redis://localhost:6379/0"

    # GCP Cloud Storage — videos and frames
    gcs_bucket_name: Optional[str] = None

    # Supabase
    supabase_url: Optional[str] = None
    supabase_service_key: Optional[str] = None   # service_role key — DB writes, bypasses RLS
    supabase_anon_key: Optional[str] = None       # anon/public key — JWT verification
    # JWT secret (Dashboard → Settings → API → JWT Secret) — enables local HS256
    # token verification instead of a network round-trip to Supabase per request
    supabase_jwt_secret: Optional[str] = None

    # Vertex AI — service account JSON string (used by plan_trip, plan_day_out)
    google_service_account_json: Optional[str] = None  # full JSON as a single env var string

    # Bulk Explore Seeder — shared secret for POST /api/v1/bulk-generate-explore
    bulk_explore_api_key: Optional[str] = None

    # Internal admin API (travbuddy_dashboard) — shared passcode sent as
    # X-Admin-Passcode. Unset means every /api/v1/admin/* route 503s, so a
    # missing env var fails closed instead of opening the admin surface.
    admin_api_passcode: Optional[str] = None

    # Transactional email (Resend). Admin-triggered mail (approve/reject) is
    # sent from here; traveller/partner mail from the website. No key = no-op.
    resend_api_key: Optional[str] = None
    resend_from: str = "TravBuddy <hello@travbuddy.in>"
    site_url: str = "https://travbuddy.in"

    # Rotating proxy — Instagram requests go through this FIRST so Instagram
    # never sees (and blocks) the server's own IP. Direct is the fallback.
    # Format: http://user:pass@host:port/
    instagram_proxy_url: Optional[str] = None

    # Celery concurrency — keep low to avoid Instagram rate limits per IP
    celery_worker_concurrency: int = 8

    # App
    app_env: str = "development"
    # Serve /docs, /redoc and /openapi.json. Off unless explicitly switched on:
    # the schema is a complete map of every route, parameter and model we have,
    # and it was being served to the public internet. Deliberately its own flag
    # rather than `app_env != "production"` — prod is currently deployed with
    # APP_ENV unset, so an env-name check would have left it exposed.
    expose_api_docs: bool = False
    log_level: str = "INFO"
    temp_dir: str = "/tmp/travbuddy"

    # Sentry — error tracking (project: travbuddy_backend, id: 4511325237346304)
    sentry_dsn: Optional[str] = None
    sentry_traces_sample_rate: float = 0.1
    sentry_profiles_sample_rate: float = 0.1

    # Razorpay — paid community plans (INR only for now)
    razorpay_key_id: Optional[str] = None
    razorpay_key_secret: Optional[str] = None
    razorpay_webhook_secret: Optional[str] = None

    # Booking.com affiliate ID (used in flight search headers + booking URLs)
    booking_affiliate_id: str = "304142"

    # ConfirmTKT — Indian Railway API credentials
    confirmtkt_api_key: str = "ct-web!2$"
    confirmtkt_device_id: str = "067d2368-0a5c-4372-abf7-fd33546f1dea"

    # CORS — comma-separated list of allowed origins, or "*" for all
    allowed_origins: str = "*"

    # Threads (and thus thread-local Supabase clients) for the dedicated DB
    # executor. Caps concurrent DB ops per process; raise to relieve /plan
    # contention, but each thread holds its own connection pool.
    db_executor_workers: int = 4

    # Hard ceiling (seconds) on any single request before the server returns 504.
    # Backstops per-endpoint timeouts so a wedged scraper/upstream can't pin a
    # worker connection open forever. 0 disables the middleware.
    request_timeout_seconds: float = 210.0

    # ── Resilience / load-shedding ───────────────────────────────────────────
    # Max trip generations running at once PER WORKER PROCESS. Beyond this, new
    # generations queue (they don't fail) — the trip row exists and the frontend
    # sees a "queued" event until a slot frees. Stops a burst of users from
    # saturating Vertex quota + the proxy + the TLS stack all at once. With
    # uvicorn --workers 4 the box-wide ceiling is 4× this.
    max_concurrent_generations: int = 6

    # Default hard timeout (seconds) for a single provider operation wrapped by a
    # circuit breaker — nothing waits forever. Per-provider overrides live on the
    # breaker singletons (circuit_breaker.py).
    provider_timeout_seconds: float = 25.0

    # Default max concurrent in-flight calls to any one provider, across all
    # trips. Per-provider overrides live on the breaker singletons.
    provider_max_concurrency: int = 8

    # Max concurrent Vertex (Gemini) generateContent calls per process — the
    # tightest external quota, so it gets its own gate.
    vertex_max_concurrency: int = 6


settings = Settings()
