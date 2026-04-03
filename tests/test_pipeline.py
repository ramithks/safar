"""
Integration-style test for the full pipeline using mocked external services.
Run with: pytest tests/test_pipeline.py -v
"""
import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.downloader import DownloadResult
from app.pipeline.frame_extractor import ExtractedFrame
from app.pipeline.scene_detector import Scene
from app.pipeline.vision_client import VisionResult


@pytest.fixture
def mock_frames(tmp_path):
    """Create real JPEG files that the pipeline can process."""
    from PIL import Image
    frames = []
    for i in range(3):
        path = str(tmp_path / f"frame_{i:04d}.jpg")
        img = Image.new("RGB", (640, 480), color=(i * 80, 100, 150))
        img.save(path, "JPEG")
        frames.append(
            ExtractedFrame(
                scene_index=i,
                frame_path=path,
                timestamp_seconds=float(i * 10),
                scene_start_seconds=float(i * 10),
                scene_end_seconds=float((i + 1) * 10),
                width=640,
                height=480,
            )
        )
    return frames


@pytest.fixture
def mock_vision_results(mock_frames):
    results = []
    locations = [
        ("Eiffel Tower", 48.8584, 2.2945, "landmark_recognition", 0.95),
        ("Louvre Museum", 48.8606, 2.3376, "landmark_recognition", 0.88),
        ("Arc de Triomphe", 48.8738, 2.2950, "web_detection", 0.72),
    ]
    for frame, (name, lat, lng, source, conf) in zip(mock_frames, locations):
        vr = VisionResult(frame=frame)
        vr.landmark_name = name
        vr.lat = lat
        vr.lng = lng
        vr.detection_source = source
        vr.confidence = conf
        vr.scene_description = f"View of {name}"
        results.append(vr)
    return results


class TestFullPipeline:
    def test_pipeline_happy_path(self, tmp_path, mock_frames, mock_vision_results):
        from app.pipeline.orchestrator import run_pipeline
        from app.models.response import PlaceDetail, Coordinates

        fake_video = str(tmp_path / "video.mp4")
        with open(fake_video, "wb") as f:
            f.write(b"fake_video_data" * 100)

        mock_download = DownloadResult(file_path=fake_video, duration_seconds=30.0)
        mock_scenes = [
            Scene(i * 300, (i + 1) * 300, float(i * 10), float((i + 1) * 10), i * 300 + 150, float(i * 10 + 5))
            for i in range(3)
        ]

        mock_place = PlaceDetail(
            place_id="ChIJtest",
            name="Eiffel Tower",
            coordinates=Coordinates(lat=48.8584, lng=2.2945),
            types=["tourist_attraction"],
        )

        with (
            patch("app.pipeline.orchestrator.download_video_async", return_value=mock_download),
            patch("app.pipeline.orchestrator.get_video_metadata", return_value={
                "fps": 30.0, "total_frames": 900, "width": 1280,
                "height": 720, "duration_seconds": 30.0,
            }),
            patch("app.pipeline.orchestrator.detect_scenes", return_value=mock_scenes),
            patch("app.pipeline.orchestrator.extract_frames", return_value=mock_frames),
            patch("app.pipeline.orchestrator.deduplicate_frames", return_value=mock_frames),
            patch("app.pipeline.orchestrator.analyze_frames", return_value=mock_vision_results),
            patch("app.pipeline.orchestrator.enrich_with_places", return_value=[
                (vr, mock_place) for vr in mock_vision_results
            ]),
            patch("app.pipeline.orchestrator._cleanup"),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                run_pipeline(
                    job_id="test_job_001",
                    reel_url="https://www.instagram.com/reels/test/",
                    options={"min_confidence": 0.60, "enrich_places": True},
                )
            )

        assert result.status == "completed"
        assert len(result.timeline) > 0
        assert result.summary.total_locations > 0

    def test_pipeline_returns_error_on_download_failure(self, tmp_path):
        from app.pipeline.orchestrator import run_pipeline
        from app.pipeline.downloader import DownloadError

        with patch(
            "app.pipeline.orchestrator.download_video_async",
            side_effect=DownloadError("private account", retryable=False),
        ):
            with patch("app.pipeline.orchestrator._cleanup"):
                result = asyncio.get_event_loop().run_until_complete(
                    run_pipeline(
                        job_id="test_job_002",
                        reel_url="https://www.instagram.com/reels/private/",
                        options={},
                    )
                )

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "REEL_ACCESS_DENIED"

    def test_pipeline_returns_no_results_when_vision_finds_nothing(
        self, tmp_path, mock_frames
    ):
        from app.pipeline.orchestrator import run_pipeline
        from app.pipeline.downloader import DownloadResult
        from app.pipeline.vision_client import VisionResult

        fake_video = str(tmp_path / "video.mp4")
        with open(fake_video, "wb") as f:
            f.write(b"x" * 1000)

        low_conf_results = [VisionResult(frame=f) for f in mock_frames]  # confidence=0.0

        mock_scenes = [
            Scene(i * 300, (i + 1) * 300, float(i * 10), float((i + 1) * 10), i * 300 + 150, float(i * 10 + 5))
            for i in range(3)
        ]

        with (
            patch("app.pipeline.orchestrator.download_video_async",
                  return_value=DownloadResult(file_path=fake_video, duration_seconds=30.0)),
            patch("app.pipeline.orchestrator.get_video_metadata", return_value={
                "fps": 30.0, "total_frames": 900, "width": 640,
                "height": 480, "duration_seconds": 30.0,
            }),
            patch("app.pipeline.orchestrator.detect_scenes", return_value=mock_scenes),
            patch("app.pipeline.orchestrator.extract_frames", return_value=mock_frames),
            patch("app.pipeline.orchestrator.deduplicate_frames", return_value=mock_frames),
            patch("app.pipeline.orchestrator.analyze_frames", return_value=low_conf_results),
            patch("app.pipeline.orchestrator._cleanup"),
        ):
            result = asyncio.get_event_loop().run_until_complete(
                run_pipeline(
                    job_id="test_job_003",
                    reel_url="https://www.instagram.com/reels/indoor/",
                    options={"min_confidence": 0.60},
                )
            )

        assert result.status == "completed"
        assert len(result.timeline) == 0
        assert result.no_results_reason == "INDOOR_UNIDENTIFIABLE"
