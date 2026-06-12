from pydantic import BaseModel
from typing import Optional, Any, Literal
from datetime import datetime


class Coordinates(BaseModel):
    lat: float
    lng: float
    accuracy_meters: Optional[int] = None


class PlaceAddress(BaseModel):
    formatted: str
    street: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    postal_code: Optional[str] = None


class PlacePhoto(BaseModel):
    url: str
    width: Optional[int] = None
    height: Optional[int] = None


class PlaceHours(BaseModel):
    open_now: Optional[bool] = None
    weekday_text: Optional[list[str]] = None


class PlaceDetail(BaseModel):
    place_id: str
    name: str
    name_local: Optional[str] = None
    category: Optional[str] = None
    address: Optional[PlaceAddress] = None
    coordinates: Optional[Coordinates] = None
    rating: Optional[float] = None
    rating_count: Optional[int] = None
    price_level: Optional[int] = None
    hours: Optional[PlaceHours] = None
    photos: list[PlacePhoto] = []
    website: Optional[str] = None
    phone: Optional[str] = None
    google_maps_url: Optional[str] = None
    types: list[str] = []


class VisionContext(BaseModel):
    scene_description: Optional[str] = None
    visual_cues: list[str] = []
    text_detected: list[str] = []
    estimated_time_of_day: Optional[str] = None


class LocationSegment(BaseModel):
    segment_id: str
    timestamp_start_seconds: float
    timestamp_end_seconds: float
    duration_seconds: float
    confidence: float
    detection_source: Literal["gemini", "unknown"]
    frame_timestamp_seconds: float
    frame_thumbnail_b64: Optional[str] = None
    frame_thumbnail_url: Optional[str] = None  # GCS public URL (preferred over b64)
    # Primary matched place (full details from Places API, or Gemini data if Places disabled)
    place: Optional[PlaceDetail] = None
    # Direct Google Maps search link — reliable even when coords are approximate
    maps_search_url: Optional[str] = None
    # Other places near the identified location — shown as suggestions to the user
    nearby_suggestions: list[PlaceDetail] = []
    vision_context: Optional[VisionContext] = None


class GeoBBox(BaseModel):
    north: float
    south: float
    east: float
    west: float


class ReelSummary(BaseModel):
    total_locations: int
    unique_places: int
    dominant_city: Optional[str] = None
    dominant_country: Optional[str] = None
    geographic_bbox: Optional[GeoBBox] = None


class ReelInfo(BaseModel):
    url: str
    duration_seconds: Optional[float] = None
    frame_count: Optional[int] = None
    processed_at: Optional[datetime] = None
    caption: Optional[str] = None
    video_url: Optional[str] = None  # GCS copy of the downloaded video


class ProcessingStats(BaseModel):
    total_duration_ms: int
    frames_analyzed: int
    locations_detected_raw: int
    locations_after_dedup: int
    api_calls: dict[str, int] = {}


class StageProgress(BaseModel):
    name: str
    status: Literal["pending", "processing", "done", "failed"]
    duration_ms: Optional[int] = None


class JobProgress(BaseModel):
    stage: str
    stage_index: int
    total_stages: int
    percent_complete: int
    stages: list[StageProgress]


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Optional[str] = None
    retryable: bool = False
    retry_after_seconds: Optional[int] = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: Literal["queued", "processing", "completed", "failed"]
    progress: Optional[JobProgress] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    estimated_completion_at: Optional[datetime] = None
    error: Optional[ErrorDetail] = None
    metadata: Optional[dict[str, Any]] = None
    # Full ReelResult dict, included once status == completed so the client
    # can skip the extra /result round-trip.
    result: Optional[dict[str, Any]] = None


class AnalyzeReelResponse(BaseModel):
    job_id: str
    status: str = "queued"
    estimated_duration_seconds: int = 30
    poll_url: str
    stream_url: str
    created_at: datetime
    metadata: Optional[dict[str, Any]] = None


class ReelResult(BaseModel):
    job_id: str
    status: str
    reel: ReelInfo
    summary: ReelSummary
    timeline: list[LocationSegment]
    no_results_reason: Optional[str] = None
    no_results_hint: Optional[str] = None
    processing_stats: Optional[ProcessingStats] = None
    metadata: Optional[dict[str, Any]] = None
    error: Optional[ErrorDetail] = None
