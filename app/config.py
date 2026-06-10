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
    # Path to a service account JSON key; leave unset to use ADC
    google_application_credentials: Optional[str] = None

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

    # Proxy — used as fallback when Instagram rate-limits or blocks direct requests
    # Format: http://user:pass@host:port/
    instagram_proxy_url: Optional[str] = None

    # Celery concurrency — keep low to avoid Instagram rate limits per IP
    celery_worker_concurrency: int = 8

    # App
    app_env: str = "development"
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


settings = Settings()
