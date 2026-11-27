"""
Pipeline orchestrator — runs all steps in sequence, handles cleanup,
tracks progress, and emits SSE events.
"""
import asyncio
import logging
import os
import re
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
from app.pipeline.scene_detector import detect_scenes
from app.pipeline.vision_client import VisionResult, analyze_frames, parse_caption

logger = logging.getLogger(__name__)

PIPELINE_STAGES = [
    "download",
    "frame_extraction",
    "scene_detection",
    "vision_analysis",
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
    user_id: Optional[str] = None,
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
    video_gcs_url: Optional[str] = None
    frame_gcs_urls: dict[str, str] = {}
    video_upload_future: Optional[asyncio.Future] = None

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

        # ── Upload video to GCS (in background — URL not needed until result assembly) ──
        if settings.gcs_bucket_name:
            from app.storage.gcs import upload_video as gcs_upload_video
            video_upload_future = asyncio.get_event_loop().run_in_executor(
                None, gcs_upload_video, job_id, video_path, settings.gcs_bucket_name
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

        # Cap candidates before dedup so we don't dedup the entire video worth of frames
        candidates = raw_frames[: settings.max_unique_frames * 3]
        unique_frames = await asyncio.get_event_loop().run_in_executor(
            None, deduplicate_frames, candidates
        )
        unique_frames = unique_frames[: settings.max_unique_frames]

        # ── Upload frames to GCS ─────────────────────────────────────────────────
        if settings.gcs_bucket_name:
            try:
                from app.storage.gcs import upload_frames as gcs_upload_frames
                frame_paths = [f.frame_path for f in unique_frames]
                frame_gcs_urls = await asyncio.get_event_loop().run_in_executor(
                    None, gcs_upload_frames, job_id, frame_paths, settings.gcs_bucket_name
                )
            except Exception as e:
                logger.warning(f"GCS frame upload failed: {e}")

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
        ctx.inc_api("vision_ai", 1)

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
            if vision_results and all(vr.detection_source == "freellm_failed" for vr in vision_results):
                return _no_results(
                    job_id, reel_url, duration_seconds, len(unique_frames),
                    "ANALYSIS_UNAVAILABLE",
                    "The free analysis models are rate-limited right now, so this Reel could not be analysed. Try again in a few minutes.",
                    metadata,
                )
            return _no_results(
                job_id, reel_url, duration_seconds, len(unique_frames),
                "INDOOR_UNIDENTIFIABLE",
                "No recognizable locations were found. Try a Reel with outdoor landmarks or identifiable venues.",
                metadata,
            )

        # ── Stage 5: Places verification ─────────────────────────────────────────
        # Gemini names places; Google Places confirms they exist and supplies
        # canonical coordinates/addresses. Unverifiable places get downgraded
        # (and drop out if they fall below the confidence floor).
        enrichment_map: dict[int, object] = {}
        if ctx.enrich_places:
            from app.pipeline.places_client import enrich_with_places, _get_client as _places_available

            if _places_available() is not None:
                emit("stage_start", {
                    "stage": "places_enrichment",
                    "message": f"Verifying {len(confident_results)} places with Google Places...",
                })
                t = time.time()

                pairs = await asyncio.get_event_loop().run_in_executor(
                    None, enrich_with_places, confident_results
                )
                ctx.inc_api("places", len(confident_results))

                verified: list = []
                for vr, er in pairs:
                    if er.primary:
                        enrichment_map[id(vr)] = er
                        verified.append(vr)
                    else:
                        # Couldn't corroborate — downgrade rather than trust blindly
                        vr.confidence *= 0.7
                        if vr.confidence >= ctx.min_confidence:
                            verified.append(vr)
                        else:
                            logger.info(
                                f"Dropping unverifiable place '{vr.landmark_name}' "
                                f"(downgraded to {vr.confidence:.2f})"
                            )

                emit("stage_complete", {
                    "stage": "places_enrichment",
                    "duration_ms": int((time.time() - t) * 1000),
                    "verified": len(enrichment_map),
                    "dropped": len(confident_results) - len(verified),
                })
                confident_results = verified

                if not confident_results:
                    return _no_results(
                        job_id, reel_url, duration_seconds, len(unique_frames),
                        "LOW_VISUAL_QUALITY",
                        "Locations were detected but none could be matched to a known place.",
                        metadata,
                    )

        # ── Assemble timeline ────────────────────────────────────────────────────
        timeline = _build_timeline(confident_results, ctx, frame_gcs_urls, enrichment_map)

        # Cap to max_locations
        timeline = timeline[:ctx.max_locations]

        if not timeline:
            return _no_results(
                job_id, reel_url, duration_seconds, len(unique_frames),
                "LOW_VISUAL_QUALITY",
                "Locations were detected but none could be matched to a known place.",
                metadata,
            )

        # ── Collect background video upload ─────────────────────────────────────
        if video_upload_future is not None:
            try:
                video_gcs_url = await video_upload_future
                emit("stage_complete", {"stage": "upload_video", "gcs_url": video_gcs_url})
            except Exception as e:
                logger.warning(f"GCS video upload failed: {e}")
            video_upload_future = None

        # ── Summary ──────────────────────────────────────────────────────────────
        summary = _build_summary(timeline)

        total_ms = int((time.time() - pipeline_start) * 1000)
        emit("completed", {
            "job_id": job_id,
            "total_locations": len(timeline),
            "dominant_city": summary.dominant_city,
            "total_duration_ms": total_ms,
        })

        result = ReelResult(
            job_id=job_id,
            status="completed",
            reel=ReelInfo(
                url=reel_url,
                duration_seconds=duration_seconds,
                frame_count=len(unique_frames),
                processed_at=datetime.now(timezone.utc),
                caption=download_result.caption,
                video_url=video_gcs_url,
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

        # ── Persist to Supabase ──────────────────────────────────────────────────
        if settings.supabase_url and settings.supabase_service_key:
            try:
                from app.storage.supabase_client import save_reel_scan
                save_reel_scan(
                    job_id=job_id,
                    reel_url=reel_url,
                    user_id=user_id,
                    caption=download_result.caption,
                    video_url=video_gcs_url,
                    frame_urls=list(frame_gcs_urls.values()),
                    result_dict=result.model_dump(mode="json"),
                    status="completed",
                )
            except Exception as e:
                logger.warning(f"Supabase save failed: {e}")

        return result

    finally:
        # Don't delete the work dir while the background upload may still be
        # reading the video file (early-return paths skip the await above)
        if video_upload_future is not None:
            try:
                await video_upload_future
            except Exception as e:
                logger.warning(f"GCS video upload failed: {e}")
        # Always clean up temp files
        _cleanup(work_dir)


_NORMALIZE_RE = re.compile(r"[^\w\s]")


def _normalize_name(name: str) -> str:
    """Lowercase + strip punctuation for place name dedup comparison."""
    return _NORMALIZE_RE.sub("", name.lower()).strip()


def _thumb_b64(frame_path: str, max_px: int = 480, quality: int = 70) -> Optional[str]:
    """
    Downscaled base64 JPEG fallback for when GCS isn't configured.
    Bounds payload size so result rows don't balloon into multi-MB jsonb.
    """
    import base64
    import io
    try:
        from PIL import Image
        img = Image.open(frame_path)
        img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None


def _build_timeline(
    vision_results: list[VisionResult],
    ctx: PipelineContext,
    frame_gcs_urls: Optional[dict[str, str]] = None,
    enrichment_map: Optional[dict[int, object]] = None,
) -> list[LocationSegment]:
    """Convert vision results into a deduplicated, ordered timeline.

    Places-verified results use the canonical PlaceDetail from Google Places
    (real place_id, coordinates, address, rating, photos). Unverified ones
    fall back to Gemini's data.

    Dedup strategy: normalize place name (lowercase, no punctuation).
    If the same place appears in multiple frames, merge into one segment
    spanning the full time range and keep the highest-confidence thumbnail.
    """
    from app.models.response import Coordinates, PlaceAddress

    # name_key → index in timeline (for merging duplicates)
    seen_names: dict[str, int] = {}
    timeline: list[LocationSegment] = []

    for vr in vision_results:
        place = None
        suggestions: list = []
        maps_url = vr.maps_search_url
        enrichment = (enrichment_map or {}).get(id(vr))

        if enrichment is not None and getattr(enrichment, "primary", None):
            # Verified: canonical Places data wins over Gemini's guess
            place = enrichment.primary
            suggestions = list(getattr(enrichment, "suggestions", []) or [])
            maps_url = place.google_maps_url or vr.maps_search_url
        elif vr.landmark_name:
            place = PlaceDetail(
                place_id=f"gemini_{vr.frame.scene_index}",
                name=vr.landmark_name,
                category=vr.place_type,
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

        # Deduplicate by normalized place name (canonical name when verified,
        # so "Café Torte" and "Torte by Kusha Kapila" merge once Places agrees)
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
                existing.frame_thumbnail_url = (frame_gcs_urls or {}).get(vr.frame.frame_path)
                if not existing.frame_thumbnail_url:
                    existing.frame_thumbnail_b64 = _thumb_b64(vr.frame.frame_path)
            continue

        gcs_url = (frame_gcs_urls or {}).get(vr.frame.frame_path)
        frame_b64 = None if gcs_url else _thumb_b64(vr.frame.frame_path)

        segment = LocationSegment(
            segment_id=f"seg_{len(timeline) + 1:03d}",
            timestamp_start_seconds=vr.frame.scene_start_seconds,
            timestamp_end_seconds=vr.frame.scene_end_seconds,
            duration_seconds=vr.frame.scene_end_seconds - vr.frame.scene_start_seconds,
            confidence=vr.confidence,
            detection_source=vr.detection_source,
            frame_timestamp_seconds=vr.frame.timestamp_seconds,
            frame_thumbnail_b64=frame_b64,
            frame_thumbnail_url=gcs_url,
            place=place,
            maps_search_url=maps_url,
            nearby_suggestions=suggestions,
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
    if "not a video" in msg:
        return "REEL_NO_VIDEO"
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
