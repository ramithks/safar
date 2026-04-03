"""
Frame extraction from video using OpenCV.
Extracts mid-frame of each scene, resizes, and saves as JPEG.
"""
import os
import logging
from dataclasses import dataclass
from typing import Optional

import cv2
from PIL import Image

from app.config import settings
from app.pipeline.scene_detector import Scene

logger = logging.getLogger(__name__)


@dataclass
class ExtractedFrame:
    scene_index: int
    frame_path: str
    timestamp_seconds: float
    scene_start_seconds: float
    scene_end_seconds: float
    width: int
    height: int


def extract_frames(video_path: str, scenes: list[Scene], output_dir: str) -> list[ExtractedFrame]:
    """
    Extract one representative frame per scene.
    Frame is taken from the middle of each scene to avoid transition artifacts.
    Saves as JPEG at configured quality and max dimension.
    """
    os.makedirs(output_dir, exist_ok=True)
    frames = []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

        for idx, scene in enumerate(scenes):
            frame_path = os.path.join(output_dir, f"frame_{idx:04d}.jpg")

            extracted = _extract_single_frame(
                cap=cap,
                frame_number=scene.mid_frame,
                output_path=frame_path,
                fps=fps,
            )

            if extracted:
                frames.append(
                    ExtractedFrame(
                        scene_index=idx,
                        frame_path=frame_path,
                        timestamp_seconds=scene.mid_seconds,
                        scene_start_seconds=scene.start_seconds,
                        scene_end_seconds=scene.end_seconds,
                        width=extracted[0],
                        height=extracted[1],
                    )
                )
            else:
                logger.warning(f"Failed to extract frame at scene {idx} (frame {scene.mid_frame})")

    finally:
        cap.release()

    logger.info(f"Extracted {len(frames)}/{len(scenes)} frames")
    return frames


def _extract_single_frame(
    cap: cv2.VideoCapture,
    frame_number: int,
    output_path: str,
    fps: float,
) -> Optional[tuple[int, int]]:
    """
    Seek to frame_number, read it, resize if needed, save as JPEG.
    Returns (width, height) on success, None on failure.
    """
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
    ret, frame = cap.read()

    if not ret or frame is None:
        return None

    # Convert BGR (OpenCV) → RGB (PIL)
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame_rgb)

    # Resize: maintain aspect ratio, max dimension = FRAME_MAX_DIMENSION
    max_dim = settings.frame_max_dimension
    w, h = img.size
    if max(w, h) > max_dim:
        ratio = max_dim / max(w, h)
        new_w = int(w * ratio)
        new_h = int(h * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        w, h = new_w, new_h

    img.save(output_path, "JPEG", quality=settings.frame_jpeg_quality, optimize=True)
    return w, h


def get_video_metadata(video_path: str) -> dict:
    """Extract basic video metadata without full processing."""
    cap = cv2.VideoCapture(video_path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = total_frames / fps if fps > 0 else 0
        return {
            "fps": fps,
            "total_frames": total_frames,
            "width": width,
            "height": height,
            "duration_seconds": duration,
        }
    finally:
        cap.release()
