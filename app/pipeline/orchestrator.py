"""
Pipeline orchestrator — runs all steps in sequence, handles cleanup,
tracks progress, and emits SSE events.
"""
import asyncio
import logging
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Callable, Optional

from app.config import settings
from app.models.response import (
    ErrorDetail, GeoBBox, LocationSegment, PlaceDetail,
    ProcessingStats, ReelInfo, ReelResult, ReelSummary, VisionContext
)
from app.pipeline.deduplicator import deduplicate_frames
from app.pipeline.downloader import DownloadError, download_video_async
from app.pipeline.frame_extractor import extract_frames, get_video_metadata
from app.pipeline.places_client import EnrichmentResult
from app.pipeline.scene_detector import detect_scenes
from app.pipeline.vision_client import VisionResult, analyze_frames, parse_caption

logger = logging.getLogger(__name__)

PIPELINE_STAGES = [
    "download",
    "frame_extraction",
    "scene_detection",
    "vision_analysis",
    "places_enrichment",
]


@dataclass
class PipelineEvent:
    event_type: str  # stage_start | stage_complete | location_found | progress | completed | error
    data: dict


@dataclass
class PipelineContext:
    job_id: str
    reel_url: str
    work_dir: str
    min_confidence: float
    max_locations: int
    enrich_places: bool
    include_frames: bool
    events: list[PipelineEvent] = field(default_factory=list)
    api_calls: dict[str, int] = field(default_factory=dict)

    def emit(self, event_type: str, data: dict):
        self.events.append(PipelineEvent(event_type=event_type, data=data))

    def inc_api(self, api: str, count: int = 1):
        self.api_calls[api] = self.api_calls.get(api, 0) + count


async def run_pipeline(
    job_id: str,
    reel_url: str,
    options: dict,
    metadata: Optional[dict] = None,
    on_event: Optional[Callable[[PipelineEvent], None]] = None,
) -> ReelResult:
    """
    Main pipeline entry point.
    Runs all stages, cleans up temp files, and returns a ReelResult.
    """
    work_dir = os.path.join(settings.temp_dir, job_id)
    os.makedirs(work_dir, exist_ok=True)

    ctx = PipelineContext(
        job_id=job_id,
        reel_url=reel_url,
        work_dir=work_dir,
        min_confidence=options.get("min_confidence", settings.min_vision_confidence),
        max_locations=options.get("max_locations", settings.max_locations),
        enrich_places=options.get("enrich_places", True),
        include_frames=options.get("include_frames", False),
    )

    def emit(event_type: str, data: dict):
        event = PipelineEvent(event_type=event_type, data=data)
        ctx.events.append(event)
        if on_event:
            on_event(event)

    pipeline_start = time.time()

    try:
        # ── Stage 1: Download ────────────────────────────────────────────────────
        emit("stage_start", {"stage": "download", "message": "Downloading Reel from Instagram..."})
        t = time.time()

        try:
            download_result = await download_video_async(
                reel_url,
                os.path.join(work_dir, "video"),
            )
        except DownloadError as e:
            code = _classify_download_error(str(e))
            return _error_result(job_id, reel_url, code, str(e), e.retryable, metadata)

        video_path = download_result.file_path
        caption_ctx = parse_caption(download_result.caption or "")
        emit("stage_complete", {
            "stage": "download",
            "duration_ms": int((time.time() - t) * 1000),
            "caption_found": bool(download_result.caption),
            "place_hints": len(caption_ctx.place_hints) if caption_ctx else 0,
        })

        # ── Video metadata ───────────────────────────────────────────────────────
        video_meta = get_video_metadata(video_path)
        duration_seconds = download_result.duration_seconds or video_meta["duration_seconds"]

        if duration_seconds > settings.max_video_duration_seconds:
            return _error_result(
                job_id, reel_url,
                "VIDEO_TOO_LONG",
                f"Video is {duration_seconds:.0f}s. Max allowed: {settings.max_video_duration_seconds}s",
                retryable=False,
                metadata=metadata,
            )

        # ── Stage 2: Scene Detection ─────────────────────────────────────────────
        emit("stage_start", {"stage": "scene_detection", "message": "Detecting scene changes..."})
        t = time.time()

        scenes = await asyncio.get_event_loop().run_in_executor(
            None, detect_scenes, video_path
        )
        emit("stage_complete", {
            "stage": "scene_detection",
            "duration_ms": int((time.time() - t) * 1000),
            "scenes_found": len(scenes),
        })

        # ── Stage 3: Frame Extraction ────────────────────────────────────────────
        emit("stage_start", {"stage": "frame_extraction", "message": "Extracting frames..."})
        t = time.time()

        frames_dir = os.path.join(work_dir, "frames")
        raw_frames = await asyncio.get_event_loop().run_in_executor(
            None, extract_frames, video_path, scenes, frames_dir
        )

        unique_frames = await asyncio.get_event_loop().run_in_executor(
            None, deduplicate_frames, raw_frames
        )

        # Cap to max_unique_frames
        if len(unique_frames) > settings.max_unique_frames:
            unique_frames = unique_frames[:settings.max_unique_frames]

        emit("stage_complete", {
            "stage": "frame_extraction",
            "duration_ms": int((time.time() - t) * 1000),
            "frames_extracted": len(unique_frames),
        })

        if not unique_frames:
            return _no_results(
                job_id, reel_url, duration_seconds, 0,
                "NO_SCENE_CHANGES",
                "No distinct scenes were detected in this video.",
                metadata,
            )

        # ── Stage 4: Vision Analysis ─────────────────────────────────────────────
        emit("stage_start", {
            "stage": "vision_analysis",
            "message": f"Identifying locations from {len(unique_frames)} frames...",
        })
        t = time.time()

        vision_results = await asyncio.get_event_loop().run_in_executor(
            None, analyze_frames, unique_frames, caption_ctx
        )
        ctx.inc_api("vision_ai", len(unique_frames))

        # Emit location_found events as we parse results
        for vr in vision_results:
            if vr.confidence >= ctx.min_confidence and vr.landmark_name:
                emit("location_found", {
                    "place_name": vr.landmark_name,
                    "confidence": vr.confidence,
                    "timestamp_seconds": vr.frame.timestamp_seconds,
                    "detection_source": vr.detection_source,
                })

        emit("stage_complete", {
            "stage": "vision_analysis",
            "duration_ms": int((time.time() - t) * 1000),
            "locations_found": sum(
                1 for vr in vision_results if vr.confidence >= ctx.min_confidence
            ),
        })

        # Filter by confidence
        confident_results = [
            vr for vr in vision_results if vr.confidence >= ctx.min_confidence
        ]

        if not confident_results:
            return _no_results(
                job_id, reel_url, duration_seconds, len(unique_frames),
                "INDOOR_UNIDENTIFIABLE",
                "No recognizable locations were found. Try a Reel with outdoor landmarks or identifiable venues.",
                metadata,
            )

        # ── Stage 5: Places Enrichment (skipped — Gemini data is sufficient) ────
        enriched_pairs: list[tuple[VisionResult, EnrichmentResult]] = [
            (vr, EnrichmentResult(primary=None)) for vr in confident_results
        ]

        # ── Assemble timeline ────────────────────────────────────────────────────
        timeline = _build_timeline(enriched_pairs, ctx)

        # Cap to max_locations
        timeline = timeline[:ctx.max_locations]

        if not timeline:
            return _no_results(
                job_id, reel_url, duration_seconds, len(unique_frames),
                "LOW_VISUAL_QUALITY",
                "Locations were detected but none could be matched to a known place.",
                metadata,
            )

        # ── Summary ──────────────────────────────────────────────────────────────
        summary = _build_summary(timeline)

        total_ms = int((time.time() - pipeline_start) * 1000)
        emit("completed", {
            "job_id": job_id,
            "total_locations": len(timeline),
            "dominant_city": summary.dominant_city,
            "total_duration_ms": total_ms,
        })

        return ReelResult(
            job_id=job_id,
            status="completed",
            reel=ReelInfo(
                url=reel_url,
                duration_seconds=duration_seconds,
                frame_count=len(unique_frames),
                processed_at=datetime.now(timezone.utc),
                caption=download_result.caption,
            ),
            summary=summary,
            timeline=timeline,
            processing_stats=ProcessingStats(
                total_duration_ms=total_ms,
                frames_analyzed=len(unique_frames),
                locations_detected_raw=len(vision_results),
                locations_after_dedup=len(confident_results),
                api_calls=ctx.api_calls,
            ),
            metadata=metadata,
        )

    finally:
        # Always clean up temp files
        _cleanup(work_dir)


def _normalize_name(name: str) -> str:
    """Lowercase + strip punctuation for place name dedup comparison."""
    import re
    return re.sub(r"[^\w\s]", "", name.lower()).strip()


def _build_timeline(
    enriched_pairs: list[tuple[VisionResult, EnrichmentResult]],
    ctx: PipelineContext,
) -> list[LocationSegment]:
    """Convert enriched vision results into a deduplicated, ordered timeline.

    Dedup strategy: normalize place name (lowercase, no punctuation).
    If the same place appears in multiple frames, merge into one segment
    spanning the full time range and keep the highest-confidence thumbnail.
    """
    import base64
    import re
    from app.models.response import Coordinates, PlaceAddress

    # name_key → index in timeline (for merging duplicates)
    seen_names: dict[str, int] = {}
    timeline: list[LocationSegment] = []

    for vr, enrichment in enriched_pairs:
        place = enrichment.primary

        # Build PlaceDetail from Gemini fields when Places API is not used
        if not place and vr.landmark_name:
            place = PlaceDetail(
                place_id=f"gemini_{vr.frame.scene_index}",
                name=vr.landmark_name,
                address=PlaceAddress(
                    formatted=vr.address or ", ".join(
                        p for p in [vr.city, vr.country] if p
                    ),
                    city=vr.city,
                    country=vr.country,
                ) if (vr.address or vr.city or vr.country) else None,
                coordinates=(
                    Coordinates(lat=vr.lat, lng=vr.lng)
                    if vr.lat and vr.lng else None
                ),
                google_maps_url=vr.maps_search_url,
                types=["point_of_interest"],
            )

        # Deduplicate by normalized place name
        name_key = _normalize_name(place.name) if place and place.name else None
        if name_key and name_key in seen_names:
            # Merge: extend the existing segment's time range
            existing = timeline[seen_names[name_key]]
            existing.timestamp_end_seconds = max(
                existing.timestamp_end_seconds, vr.frame.scene_end_seconds
            )
            existing.duration_seconds = (
                existing.timestamp_end_seconds - existing.timestamp_start_seconds
            )
            # Upgrade thumbnail if this frame has higher confidence
            if vr.confidence > existing.confidence:
                existing.confidence = vr.confidence
                try:
                    with open(vr.frame.frame_path, "rb") as f:
                        existing.frame_thumbnail_b64 = base64.b64encode(f.read()).decode("utf-8")
                except Exception:
                    pass
            continue

        frame_b64 = None
        try:
            with open(vr.frame.frame_path, "rb") as f:
                frame_b64 = base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            pass

        segment = LocationSegment(
            segment_id=f"seg_{len(timeline) + 1:03d}",
            timestamp_start_seconds=vr.frame.scene_start_seconds,
            timestamp_end_seconds=vr.frame.scene_end_seconds,
            duration_seconds=vr.frame.scene_end_seconds - vr.frame.scene_start_seconds,
            confidence=vr.confidence,
            detection_source=vr.detection_source,
            frame_timestamp_seconds=vr.frame.timestamp_seconds,
            frame_thumbnail_b64=frame_b64,
            place=place,
            maps_search_url=vr.maps_search_url,
            nearby_suggestions=enrichment.suggestions,
            vision_context=VisionContext(
                scene_description=vr.scene_description,
                visual_cues=vr.visual_cues,
                text_detected=vr.text_detected,
            ),
        )
        if name_key:
            seen_names[name_key] = len(timeline)
        timeline.append(segment)

    timeline.sort(key=lambda s: s.timestamp_start_seconds)
    return timeline


def _build_summary(timeline: list[LocationSegment]) -> ReelSummary:
    unique_places = len({
        s.place.place_id for s in timeline if s.place
    })

    # Dominant country/city from places
    countries: dict[str, int] = {}
    cities: dict[str, int] = {}
    lats, lngs = [], []

    for seg in timeline:
        if seg.place:
            if seg.place.address:
                if seg.place.address.country:
                    countries[seg.place.address.country] = countries.get(seg.place.address.country, 0) + 1
                if seg.place.address.city:
                    cities[seg.place.address.city] = cities.get(seg.place.address.city, 0) + 1
            if seg.place.coordinates:
                lats.append(seg.place.coordinates.lat)
                lngs.append(seg.place.coordinates.lng)

    dominant_country = max(countries, key=countries.get) if countries else None
    dominant_city = max(cities, key=cities.get) if cities else None

    bbox = None
    if lats and lngs:
        bbox = GeoBBox(
            north=max(lats), south=min(lats),
            east=max(lngs), west=min(lngs),
        )

    return ReelSummary(
        total_locations=len(timeline),
        unique_places=unique_places,
        dominant_city=dominant_city,
        dominant_country=dominant_country,
        geographic_bbox=bbox,
    )


def _classify_download_error(message: str) -> str:
    msg = message.lower()
    if "private" in msg or "login" in msg or "authentication" in msg:
        return "REEL_ACCESS_DENIED"
    if "not found" in msg or "404" in msg or "does not exist" in msg:
        return "REEL_NOT_FOUND"
    if "timeout" in msg:
        return "DOWNLOAD_TIMEOUT"
    if "too large" in msg or "filesize" in msg:
        return "VIDEO_TOO_LONG"
    return "DOWNLOAD_FAILED"


def _error_result(
    job_id: str,
    reel_url: str,
    code: str,
    message: str,
    retryable: bool,
    metadata: Optional[dict],
) -> ReelResult:
    return ReelResult(
        job_id=job_id,
        status="failed",
        reel=ReelInfo(url=reel_url),
        summary=ReelSummary(total_locations=0, unique_places=0),
        timeline=[],
        error=ErrorDetail(code=code, message=message, retryable=retryable),
        metadata=metadata,
    )


def _no_results(
    job_id: str,
    reel_url: str,
    duration: float,
    frame_count: int,
    reason: str,
    hint: str,
    metadata: Optional[dict],
) -> ReelResult:
    return ReelResult(
        job_id=job_id,
        status="completed",
        reel=ReelInfo(url=reel_url, duration_seconds=duration, frame_count=frame_count),
        summary=ReelSummary(total_locations=0, unique_places=0),
        timeline=[],
        no_results_reason=reason,
        no_results_hint=hint,
        metadata=metadata,
    )


def _cleanup(work_dir: str):
    try:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
    except Exception as e:
        logger.warning(f"Failed to clean up {work_dir}: {e}")
