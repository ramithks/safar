"""Tests for the downloader fallback chain."""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.downloader import (
    DownloadError,
    DownloadResult,
    _extract_shortcode,
    download_video,
)


class TestExtractShortcode:
    def test_reel_url(self):
        url = "https://www.instagram.com/reels/C8xK2mNpQrT/"
        assert _extract_shortcode(url) == "C8xK2mNpQrT"

    def test_post_url(self):
        url = "https://www.instagram.com/p/C8xK2mNpQrT/"
        assert _extract_shortcode(url) == "C8xK2mNpQrT"

    def test_invalid_url(self):
        assert _extract_shortcode("https://youtube.com/watch?v=abc") is None


class TestDownloadFallbackChain:
    def test_returns_first_success(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        open(fake_file, "w").write("fake")  # create non-empty file

        mock_result = DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._ytdlp_download", return_value=mock_result):
            with patch("app.pipeline.downloader._instaloader_download") as mock_insta:
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                assert result.file_path == fake_file
                mock_insta.assert_not_called()

    def test_falls_back_to_instaloader_on_ytdlp_failure(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        mock_result = DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._instaloader_download", return_value=mock_result):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                assert result.file_path == fake_file

    def test_raises_when_all_fail(self, tmp_path):
        with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._instaloader_download", side_effect=DownloadError("blocked")):
                with patch("app.pipeline.downloader._gallerydl_download", side_effect=DownloadError("blocked")):
                    with pytest.raises(DownloadError):
                        download_video("https://www.instagram.com/reels/test/", str(tmp_path))

    def test_non_retryable_error_skips_retries(self, tmp_path):
        """A non-retryable error should skip remaining retries for that downloader."""
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)
        mock_result = DownloadResult(file_path=fake_file)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise DownloadError("private account", retryable=False)

        with patch("app.pipeline.downloader._ytdlp_download", side_effect=side_effect):
            with patch("app.pipeline.downloader._instaloader_download", return_value=mock_result):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                # yt-dlp should only be called once (non-retryable skips retries)
                assert call_count == 1
                assert result.file_path == fake_file
