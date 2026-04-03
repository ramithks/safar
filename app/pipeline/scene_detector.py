"""
Scene change detection using PySceneDetect.

Strategy (in order of preference):
  1. ContentDetector  — fast cuts, hard transitions (most travel reels)
  2. AdaptiveDetector — crossfades, slow zooms, gradual transitions
  3. Time-based       — fallback when no cuts detected (static/slideshow reels)
"""
import logging
from dataclasses import dataclass
from typing import Optional

from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector, AdaptiveDetector

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class Scene:
    start_frame: int
    end_frame: int
    start_seconds: float
    end_seconds: float
    mid_frame: int
    mid_seconds: float


def detect_scenes(video_path: str, fps: Optional[float] = None) -> list[Scene]:
    """
    Detect scene changes in a video file.
    Returns a list of Scene objects ordered by time.
    """
    scenes = _run_content_detector(video_path)

    if len(scenes) < 2:
        logger.info("ContentDetector found < 2 scenes, trying AdaptiveDetector")
        scenes = _run_adaptive_detector(video_path)

    if len(scenes) < 2:
        logger.info("Both detectors found < 2 scenes, falling back to time-based extraction")
        scenes = _time_based_fallback(video_path, interval_seconds=10.0)

    # Cap at max_unique_frames (before dedup — dedup will reduce further)
    if len(scenes) > settings.max_unique_frames * 2:
        logger.info(f"Detected {len(scenes)} scenes, capping at {settings.max_unique_frames * 2}")
        scenes = _subsample_scenes(scenes, settings.max_unique_frames * 2)

    logger.info(f"Scene detection complete: {len(scenes)} scenes")
    return scenes


def _run_content_detector(video_path: str) -> list[Scene]:
    """Primary detector: optimized for hard cuts (most common in travel reels)."""
    try:
        video = open_video(video_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(
            ContentDetector(
                threshold=settings.scene_detection_threshold,
                min_scene_len=settings.scene_min_length_frames,
            )
        )
        scene_manager.detect_scenes(video)
        raw = scene_manager.get_scene_list()
        return _convert_scenes(raw)
    except Exception as e:
        logger.warning(f"ContentDetector failed: {e}")
        return []


def _run_adaptive_detector(video_path: str) -> list[Scene]:
    """Fallback: handles crossfades, zoom transitions, gradual changes."""
    try:
        video = open_video(video_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(
            AdaptiveDetector(
                adaptive_threshold=3.0,
                min_scene_len=settings.scene_min_length_frames,
            )
        )
        scene_manager.detect_scenes(video)
        raw = scene_manager.get_scene_list()
        return _convert_scenes(raw)
    except Exception as e:
        logger.warning(f"AdaptiveDetector failed: {e}")
        return []


def _time_based_fallback(video_path: str, interval_seconds: float = 10.0) -> list[Scene]:
    """
    Last resort: extract frames at fixed intervals.
    Used for slideshows or continuous single-shot reels.
    """
    import cv2

    scenes = []
    cap = cv2.VideoCapture(video_path)

    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = total_frames / fps
        interval_frames = int(fps * interval_seconds)

        if total_frames <= 0 or fps <= 0:
            return []

        t = 0.0
        while t < duration:
            start_frame = int(t * fps)
            end_frame = min(int((t + interval_seconds) * fps), total_frames - 1)
            mid_frame = (start_frame + end_frame) // 2

            scenes.append(
                Scene(
                    start_frame=start_frame,
                    end_frame=end_frame,
                    start_seconds=t,
                    end_seconds=min(t + interval_seconds, duration),
                    mid_frame=mid_frame,
                    mid_seconds=mid_frame / fps,
                )
            )
            t += interval_seconds

    finally:
        cap.release()

    return scenes


def _convert_scenes(raw_scenes: list) -> list[Scene]:
    """Convert PySceneDetect FrameTimecode tuples to Scene objects."""
    result = []
    for start_tc, end_tc in raw_scenes:
        start_frame = start_tc.get_frames()
        end_frame = end_tc.get_frames()
        mid_frame = (start_frame + end_frame) // 2

        result.append(
            Scene(
                start_frame=start_frame,
                end_frame=end_frame,
                start_seconds=start_tc.get_seconds(),
                end_seconds=end_tc.get_seconds(),
                mid_frame=mid_frame,
                mid_seconds=mid_frame / max(start_tc.get_framerate(), 1),
            )
        )
    return result


def _subsample_scenes(scenes: list[Scene], max_count: int) -> list[Scene]:
    """Evenly subsample scenes to cap at max_count."""
    if len(scenes) <= max_count:
        return scenes
    step = len(scenes) / max_count
    return [scenes[int(i * step)] for i in range(max_count)]
