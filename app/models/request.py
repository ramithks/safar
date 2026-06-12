from pydantic import BaseModel, field_validator
from typing import Optional, Any


class AnalyzeOptions(BaseModel):
    min_confidence: float = 0.60
    max_locations: int = 20
    enrich_places: bool = True   # verify Gemini results against Google Places
    include_frames: bool = True
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None

    @field_validator("webhook_url")
    @classmethod
    def validate_webhook_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if not v.startswith(("https://", "http://")):
            raise ValueError("webhook_url must be an http/https URL")
        # Block internal/private network addresses to prevent SSRF
        from urllib.parse import urlparse
        host = urlparse(v).hostname or ""
        blocked = ("localhost", "127.", "0.0.0.0", "::1", "169.254.", "10.", "192.168.", "172.")
        if any(host == b or host.startswith(b) for b in blocked):
            raise ValueError("webhook_url must not point to a private or local address")
        return v


class AnalyzeReelRequest(BaseModel):
    reel_url: str
    options: AnalyzeOptions = AnalyzeOptions()
    metadata: Optional[dict[str, Any]] = None

    @field_validator("reel_url")
    @classmethod
    def validate_instagram_url(cls, v: str) -> str:
        v = v.strip()
        if "instagram.com" not in v:
            raise ValueError("URL must be an Instagram URL")
        return v
