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


@pytest.fixture(autouse=True)
def _fast_and_deterministic():
    """No proxy (single mode per downloader) and no real sleeps."""
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


class TestDownloadFallbackChain:
    def test_returns_first_success(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        mock_result = DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._ytdlp_download", return_value=mock_result):
            with patch("app.pipeline.downloader._embed_scrape_download") as mock_embed:
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                assert result.file_path == fake_file
                mock_embed.assert_not_called()

    def test_falls_back_to_embed_scraper_on_ytdlp_failure(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        mock_result = DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._embed_scrape_download", return_value=mock_result):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                assert result.file_path == fake_file

    def test_raises_when_all_fail(self, tmp_path):
        with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._embed_scrape_download", side_effect=DownloadError("blocked")):
                with patch("app.pipeline.downloader._graphql_download", side_effect=DownloadError("blocked")):
                    with patch("app.pipeline.downloader._gallerydl_download", side_effect=DownloadError("blocked")):
                        with pytest.raises(DownloadError):
                            download_video("https://www.instagram.com/reels/test/", str(tmp_path))

    def test_all_fail_error_reports_every_downloader(self, tmp_path):
        """The final error should carry each downloader's failure, not just the last."""
        with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("yt blocked")):
            with patch("app.pipeline.downloader._embed_scrape_download", side_effect=DownloadError("no video url")):
                with patch("app.pipeline.downloader._graphql_download", side_effect=DownloadError("http blocked")):
                    with patch("app.pipeline.downloader._gallerydl_download", side_effect=DownloadError("login page")):
                        with pytest.raises(DownloadError) as exc_info:
                            download_video("https://www.instagram.com/reels/test/", str(tmp_path))
        msg = str(exc_info.value)
        for part in ("yt-dlp", "embed-scraper", "graphql", "gallery-dl"):
            assert part in msg

    def test_fatal_error_stops_the_chain(self, tmp_path):
        """A fatal error (post has no video) must stop immediately — later
        downloaders can't succeed either."""
        fatal = DownloadError("This Instagram post is not a video (image post)", fatal=True)
        with patch("app.pipeline.downloader._ytdlp_download", side_effect=DownloadError("blocked")):
            with patch("app.pipeline.downloader._embed_scrape_download", side_effect=DownloadError("blocked")):
                with patch("app.pipeline.downloader._graphql_download", side_effect=fatal):
                    with patch("app.pipeline.downloader._gallerydl_download") as mock_gdl:
                        with pytest.raises(DownloadError) as exc_info:
                            download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                        mock_gdl.assert_not_called()
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

        with patch("app.pipeline.downloader._ytdlp_download", side_effect=side_effect):
            with patch("app.pipeline.downloader._embed_scrape_download", return_value=mock_result):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))
                # yt-dlp should only be called once (non-retryable skips retries)
                assert call_count == 1
                assert result.file_path == fake_file


class TestProxyFirstOrdering:
    def test_proxy_tried_before_direct(self, tmp_path):
        """With a proxy configured, the first attempt must go through it."""
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        seen_proxies = []

        def fake_dl(url, output_dir, proxy=None):
            seen_proxies.append(proxy)
            if proxy is None:
                return DownloadResult(file_path=fake_file)
            raise DownloadError("proxy unreachable", retryable=False)

        with patch("app.pipeline.downloader._get_proxy_url", return_value="http://proxy:8080"):
            with patch("app.pipeline.downloader._ytdlp_download", side_effect=fake_dl):
                result = download_video("https://www.instagram.com/reels/test/", str(tmp_path))

        assert result.file_path == fake_file
        assert seen_proxies[0] == "http://proxy:8080"   # proxy first
        assert seen_proxies[-1] is None                  # direct fallback second

    def test_no_proxy_goes_direct_only(self, tmp_path):
        fake_file = str(tmp_path / "video.mp4")
        with open(fake_file, "wb") as f:
            f.write(b"x" * 2048)

        seen_proxies = []

        def fake_dl(url, output_dir, proxy=None):
            seen_proxies.append(proxy)
            return DownloadResult(file_path=fake_file)

        with patch("app.pipeline.downloader._ytdlp_download", side_effect=fake_dl):
            download_video("https://www.instagram.com/reels/test/", str(tmp_path))

        assert seen_proxies == [None]
