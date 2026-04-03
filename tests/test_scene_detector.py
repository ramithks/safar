"""Tests for scene detection."""
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.scene_detector import Scene, _subsample_scenes, _time_based_fallback


class TestSubsampleScenes:
    def test_no_change_when_under_limit(self):
        scenes = [
            Scene(i, i + 30, i / 30, (i + 30) / 30, i + 15, (i + 15) / 30)
            for i in range(0, 300, 30)
        ]
        result = _subsample_scenes(scenes, max_count=10)
        assert len(result) == 10

    def test_passthrough_when_at_limit(self):
        scenes = [
            Scene(i, i + 30, i / 30, (i + 30) / 30, i + 15, (i + 15) / 30)
            for i in range(5)
        ]
        result = _subsample_scenes(scenes, max_count=5)
        assert len(result) == 5

    def test_subsamples_evenly(self):
        scenes = [
            Scene(i, i + 1, float(i), float(i + 1), i, float(i))
            for i in range(20)
        ]
        result = _subsample_scenes(scenes, max_count=4)
        assert len(result) == 4


class TestTimeBasedFallback:
    def test_returns_scenes_for_valid_video(self, tmp_path):
        """Uses a real OpenCV VideoCapture mock."""
        import cv2

        mock_cap = MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.side_effect = lambda prop: {
            cv2.CAP_PROP_FRAME_COUNT: 900,  # 30s at 30fps
            cv2.CAP_PROP_FPS: 30.0,
        }.get(prop, 0)

        with patch("cv2.VideoCapture", return_value=mock_cap):
            scenes = _time_based_fallback("fake.mp4", interval_seconds=10.0)

        # 30 seconds / 10 second intervals = 3 scenes
        assert len(scenes) == 3
        assert all(isinstance(s, Scene) for s in scenes)
