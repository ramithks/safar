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

    # ── Hotel OS ─────────────────────────────────────────────────────────────
    #
    # Whether this process serves the hotel OS at all — `/api/v1/pms/*` and the
    # messaging webhooks. **Off by default, and the default is the whole point.**
    #
    # The hotel OS is a different Supabase project to the marketplace
    # (`rdlozqvuywtgsodunait` against `uabxtdhxbamuhthxnhid`) and `get_db()`
    # builds one client from one `SUPABASE_URL`. A marketplace deployment with
    # these routes mounted answers 500 to every one of them, because the tables
    # are not there — so StayOS is its own deployment of this codebase with
    # `.env.hotelos`, and the marketplace stays correct without anybody having
    # to remember to switch something off.
    enable_hotel_os: bool = False

    # ── Hotel OS agent runtime (services/ai) ────────────────────────────────
    # Gemini on Vertex, through the same `services/vertex.py` the traveller-side
    # pipeline uses — same project, same credentials, same region rotation, same
    # circuit breaker. One AI provider architecture, not two.
    #
    # Model ids live here and nowhere else, matching the rule above, and the
    # runtime asks `model_router` for a *capability* rather than naming any of
    # these. Three tiers because guest traffic is not one workload:
    #
    #   conversation  the workhorse. A guest turn is a short reasoning step over
    #                 twenty tool schemas, run thousands of times a month.
    #   reasoning     the exception. A rate negotiation, a cancellation dispute,
    #                 an owner asking why revenue moved — where a wrong answer
    #                 costs more than the tokens.
    #   classify      routing, intent, language, spam. Never worth the others.
    #
    # **The 3.x family is the intended target and is not yet reachable.**
    # gemini-3-flash, gemini-3.1-pro and gemini-3.1-flash-lite all return 404
    # for project travbuddy-499007 — in us-central1, us-east5, europe-west4 and
    # global, on both v1 and v1beta1. Credentials are fine; the models are not
    # enabled for this project. So the tiers run on 2.5, which is verified
    # working with function calling, and moving is three lines here once access
    # lands: the router, the provider and the tool conversion name no model.
    hotel_model_conversation: str = "gemini-2.5-flash"       # target: gemini-3-flash
    hotel_model_reasoning: str = "gemini-2.5-pro"            # target: gemini-3.1-pro
    hotel_model_classify: str = "gemini-2.5-flash-lite"      # target: gemini-3.1-flash-lite

    # Per request, not per turn. A guest waiting on WhatsApp will forgive thirty
    # seconds; a hung socket holding a worker is a different problem.
    hotel_model_timeout_seconds: float = 45.0
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

    # ── Instagram ────────────────────────────────────────────────────────────
    #
    # Two windows, because Meta gives two. An automated reply gets 24 hours from
    # the guest's last message, the same as WhatsApp. A conversation a *person*
    # has taken over gets seven days under Meta's human-agent allowance — which
    # is why `services/messaging/policy.py` reads the conversation's `handler`
    # rather than treating this as a constant. Claiming the longer window while
    # the agent is still driving would be a policy violation wearing a longer
    # window's clothes.
    instagram_service_window_hours: int = 24
    instagram_human_agent_window_hours: int = 168

    instagram_sandbox_ref: str = "sandbox"
    instagram_verify_token: Optional[str] = None
    instagram_access_token: Optional[str] = None
    instagram_app_secret: Optional[str] = None
    instagram_api_version: str = "v21.0"

    # The controlled smoke test, and only the controlled smoke test.
    #
    # Deliberately singular. `scripts/meta_smoke.py` messages exactly this
    # number and has no way to be handed a list — a smoke test that could
    # quietly widen into production traffic would be the worst possible way to
    # discover a mistake. Read through settings like every other credential, so
    # `.env` is genuinely the one place any of this lives.
    whatsapp_test_recipient: Optional[str] = None
    whatsapp_test_phone_number_id: Optional[str] = None

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

    # Job queue — and the Celery broker, and the rate limiter's storage, and
    # the messaging pipeline's lock. One URL behind all of them.
    #
    # **The localhost default is a development convenience and is always wrong
    # in a container.** Nothing listens on localhost inside one, so it produces
    # `Error 111 connecting to localhost:6379` — which reads like Redis is down
    # rather than like the variable was never set. Worse than the noise is the
    # silence: this is also `broker=` in `workers/celery_app.py`, so a worker
    # that starts on the default connects to nothing and every messaging and
    # marketing task simply never runs, with no error anywhere.
    #
    # `app/main.py` therefore logs an error at boot if production is still
    # holding this default. See `redis_target()` for the redacted form used in
    # logs and health output.
    # The two deployments share a Redis server and must not share a namespace:
    # the marketplace is on database 0, StayOS on 1. That is belt to the queue
    # routing's braces (`workers/celery_app.py`) — routing is what stops a task
    # crossing over; separate databases keep result keys and locks apart too.
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


def redis_target(url: str | None = None) -> str:
    """
    The Redis this process is pointed at, safe to log.

    Scheme, host, port and database number only — a Redis URL carries its
    password inline (`redis://:secret@host:6379/0`) and this string goes into
    logs and into the health endpoint's error text, both of which get pasted
    into chats and issue trackers.

    Exists because the failure it describes is otherwise unreadable. An unset
    `REDIS_URL` leaves the localhost default in place, and the resulting
    `Error 111 connecting to localhost:6379` reads like Redis is down rather
    than like a variable was never set — which is what it actually means in a
    container, where nothing listens on localhost.
    """
    from urllib.parse import urlsplit

    raw = url if url is not None else settings.redis_url
    try:
        parts = urlsplit(raw)
        host = parts.hostname or "?"
        port = f":{parts.port}" if parts.port else ""
        db = parts.path or ""
        return f"{parts.scheme}://{host}{port}{db}"
    except Exception:  # noqa: BLE001 — a log line must never raise
        return "<unparseable REDIS_URL>"


# Whether anybody actually supplied `REDIS_URL`, recorded once from the settings
# object the app is really using.
#
# Asked of pydantic rather than answered by comparing the value to the default,
# because those differ: `redis://localhost:6379/0` is a legitimate thing to set
# — a compose stack whose Redis really is on localhost — and a value check calls
# that "unset" and prints a boot error about a variable somebody just set. Which
# it did, while verifying the compose stack. A warning that cries wolf once is a
# warning nobody reads twice.
#
# `model_fields_set` covers every source pydantic-settings reads: the
# environment, a `.env` file, an explicit argument. Only the field default is
# absent from it. Captured at import rather than recomputed per call, so the
# health endpoint does not re-read `.env` from disk on every hit.
_REDIS_URL_WAS_PROVIDED = "redis_url" in settings.model_fields_set


def redis_is_unconfigured() -> bool:
    """True when `REDIS_URL` was never supplied and the default survived."""
    return not _REDIS_URL_WAS_PROVIDED
