"""Tests for the downloader fallback chain."""
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app.pipeline.downloader import (
    DownloadError,
    DownloadResult,
    _extract_shortcode,
    _videodropper_encrypt_url,
    download_video,
)


@pytest.fixture(autouse=True)
def _fast_and_deterministic():
    """No proxy (single direct attempt per downloader) and no real sleeps."""
    with patch("app.pipeline.downloader._get_proxy_url", return_value=None):
        with patch("app.pipeline.downloader.time.sleep"):
            yield


class TestExtractShortcode:
    def test_reel_url(self):
        url = "https://www.instagram.com/reels/C8xK2mNpQrT/"
        assert _extract_shortcode(url) == "C8xK2mNpQrT"

    def test_post_url(self):
        url = "https://www.instagram.com/p/C8xK2mNpQrT/"
        assert _extract_shortcode(url) == "C8xK2mNpQrT"

    def test_invalid_url(self):
        assert _extract_shortcode("https://youtube.com/watch?v=abc") is None


class TestVideodropperEncrypt:
    def test_matches_known_header(self):
        """AES-128-ECB header must match fastvideosave.net's captured sample."""
        header = _videodropper_encrypt_url("https://www.instagram.com/reel/DZ7uO2ksnG2/")
        assert header == (
            "35f91d0ece1ec1b60aea4eb1c27cf17b033245f4b8ed003e4a7fb20f127a4974"
            "7754a119e8eac2fa0d25a0bab642c132"
        )


class TestDownloadFallbackChain:
    def test_returns_first_success(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        mock_result = DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._downreels_download", return_value=mock_result):
            with patch("app.pipeline.downloader._videodropper_download") as mock_next:
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                assert result.file_path == fake_file
                mock_next.assert_not_called()

    def test_falls_back_to_next_on_downreels_failure(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        mock_result = DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._downreels_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._videodropper_download", return_value=mock_result):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                assert result.file_path == fake_file

    def test_raises_when_all_fail(self, tmp_path):
        with patch("app.pipeline.downloader._downreels_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._videodropper_download", side_effect=DownloadError("blocked")):
                with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("blocked")):
                    with patch("app.pipeline.downloader._embed_scrape_download", side_effect=DownloadError("blocked")):
                        with pytest.raises(DownloadError):
                            download_video("https://www.instagram.com/reels/test/", str(tmp_path))

    def test_all_fail_error_reports_every_downloader(self, tmp_path):
        """The final error should carry each downloader's failure, not just the last."""
        with patch("app.pipeline.downloader._downreels_download", side_effect=DownloadError("api down")):
            with patch("app.pipeline.downloader._videodropper_download", side_effect=DownloadError("vd down")):
                with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("yt blocked")):
                    with patch("app.pipeline.downloader._embed_scrape_download", side_effect=DownloadError("no video url")):
                        with pytest.raises(DownloadError) as exc_info:
                            download_video("https://www.instagram.com/reels/test/", str(tmp_path))
        msg = str(exc_info.value)
        for part in ("downreels", "videodropper", "yt-dlp", "embed-scraper"):
            assert part in msg

    def test_fatal_error_stops_the_chain(self, tmp_path):
        """A fatal error (post has no video) must stop immediately — later
        downloaders can't succeed either."""
        fatal = DownloadError("This Instagram post is not a video (image post)", fatal=True)
        with patch("app.pipeline.downloader._downreels_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._videodropper_download", side_effect=fatal):
                with patch("app.pipeline.downloader._ytdlp_download") as mock_ytdlp:
                    with pytest.raises(DownloadError) as exc_info:
                        download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                    mock_ytdlp.assert_not_called()
        assert "not a video" in str(exc_info.value)

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

        with patch("app.pipeline.downloader._downreels_download", side_effect=side_effect):
            with patch("app.pipeline.downloader._ytdlp_download", return_value=mock_result):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                # downreels should only be called once (non-retryable skips retries)
                assert call_count == 1
                assert result.file_path == fake_file


class TestProxyOnly:
    def test_all_attempts_use_proxy_when_configured(self, tmp_path):
        """With a proxy configured, every attempt must go through it — no direct fallback."""
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        seen_proxies = []

        def fake_dl(url, output_dir, proxy=None):
            seen_proxies.append(proxy)
            return DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._get_proxy_url", return_value="http://proxy:8080"):
            with patch("app.pipeline.downloader._downreels_download", side_effect=fake_dl):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))

        assert result.file_path == fake_file
        assert seen_proxies == ["http://proxy:8080"]

    def test_never_falls_back_to_direct(self, tmp_path):
        """Even when every downloader fails, no attempt may run without the proxy."""
        seen_proxies = []

        def fake_dl(url, output_dir, proxy=None):
            seen_proxies.append(proxy)
            raise DownloadError("blocked", retryable=False)

        with patch("app.pipeline.downloader._get_proxy_url", return_value="http://proxy:8080"):
            with patch("app.pipeline.downloader._downreels_download", side_effect=fake_dl):
                with patch("app.pipeline.downloader._ytdlp_download", side_effect=fake_dl):
                    with patch("app.pipeline.downloader._embed_scrape_download", side_effect=fake_dl):
                        with pytest.raises(DownloadError):
                            download_video("https://www.instagram.com/reels/test/", str(tmp_path))

        assert None not in seen_proxies

    def test_retryable_errors_retry_through_proxy(self, tmp_path):
        """Retryable failures get extra attempts through the rotating proxy (fresh exit IPs)."""
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        calls = []

        def fake_dl(url, output_dir, proxy=None):
            calls.append(proxy)
            if len(calls) < 3:
                raise DownloadError("429 rate limit", retryable=True)
            return DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._get_proxy_url", return_value="http://proxy:8080"):
            with patch("app.pipeline.downloader._downreels_download", side_effect=fake_dl):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))

        assert result.file_path == fake_file
        assert calls == ["http://proxy:8080"] * 3

    def test_no_proxy_goes_direct_only(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        seen_proxies = []

        def fake_dl(url, output_dir, proxy=None):
            seen_proxies.append(proxy)
            return DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._downreels_download", side_effect=fake_dl):
            download_video("https://www.instagram.com/reels/test/", str(tmp_path))

        assert seen_proxies == [None]
