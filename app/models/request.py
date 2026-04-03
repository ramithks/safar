from pydantic import BaseModel, HttpUrl, field_validator
from typing import Optional, Any


class AnalyzeOptions(BaseModel):
    min_confidence: float = 0.60
    max_locations: int = 20
    enrich_places: bool = False
    include_frames: bool = True
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None


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
