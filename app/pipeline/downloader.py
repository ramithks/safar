"""
Video downloader with a 3-library fallback chain (no auth/cookies required):
  1. yt-dlp       — most reliable, actively maintained, public reels work without auth
  2. gallery-dl   — different HTTP client + extraction path, no auth needed
  3. instaloader  — public-only mode (no session), different rate limit pool
"""
import os
import re
import time
import asyncio
import logging
from pathlib import Path
from typing import Optional

import yt_dlp
import instaloader
import gallery_dl

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_DELAYS = [5, 15]  # seconds between retries


class DownloadError(Exception):
    """Raised when all downloaders have failed."""
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class DownloadResult:
    def __init__(
        self,
        file_path: str,
        duration_seconds: Optional[float] = None,
        caption: Optional[str] = None,
    ):
        self.file_path = file_path
        self.duration_seconds = duration_seconds
        self.caption = caption  # raw Instagram post caption/description


# ─── yt-dlp ─────────────────────────────────────────────────────────────────────

def _ytdlp_download(url: str, output_dir: str) -> DownloadResult:
    from app.config import settings

    output_path = os.path.join(output_dir, "video.%(ext)s")

    ydl_opts = {
        # Instagram Reels are single-stream mp4 — no separate audio track.
        # Use a wide fallback chain so yt-dlp never hits "format not available".
        "format": (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[height<=720]+bestaudio"
            "/best[height<=720][ext=mp4]"
            "/best[height<=720]"
            "/best"
        ),
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "http_chunk_size": 10485760,  # 10MB chunks
        "max_filesize": settings.max_video_size_mb * 1024 * 1024,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        duration = info.get("duration")
        caption = info.get("description") or info.get("title") or None

    # Find the downloaded file (extension may vary)
    for ext in ["mp4", "webm", "mkv", "mov"]:
        candidate = os.path.join(output_dir, f"video.{ext}")
        if os.path.exists(candidate):
            return DownloadResult(file_path=candidate, duration_seconds=duration, caption=caption)

    # yt-dlp sometimes merges into a different name — scan directory
    for f in os.listdir(output_dir):
        if f.startswith("video.") and not f.endswith(".part"):
            return DownloadResult(
                file_path=os.path.join(output_dir, f),
                duration_seconds=duration,
                caption=caption,
            )

    raise DownloadError("yt-dlp completed but output file not found")


# ─── gallery-dl ─────────────────────────────────────────────────────────────────

def _gallerydl_download(url: str, output_dir: str) -> DownloadResult:
    """
    No auth required. Uses a completely different HTTP extraction path from yt-dlp,
    making it a meaningful fallback when yt-dlp's extractor breaks.
    """
    gallery_dl.config.set((), "base-directory", output_dir)
    gallery_dl.config.set((), "filename", "gdl_video")
    gallery_dl.config.set(("extractor", "instagram"), "videos", True)
    gallery_dl.config.set(("extractor", "instagram"), "reels", True)
    gallery_dl.config.set(("downloader",), "part", False)

    before = set(os.listdir(output_dir))

    job = gallery_dl.job.DownloadJob(url)
    job.run()

    after = set(os.listdir(output_dir))
    new_files = after - before

    video_extensions = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
    for fname in new_files:
        if Path(fname).suffix.lower() in video_extensions:
            return DownloadResult(file_path=os.path.join(output_dir, fname))

    raise DownloadError("gallery-dl completed but no video file found")


# ─── instaloader (no auth) ───────────────────────────────────────────────────────

def _extract_shortcode(url: str) -> Optional[str]:
    """Extract Instagram shortcode from reel/post URL."""
    patterns = [
        r"instagram\.com/reels?/([A-Za-z0-9_-]+)",
        r"instagram\.com/p/([A-Za-z0-9_-]+)",
        r"instagram\.com/tv/([A-Za-z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _instaloader_download(url: str, output_dir: str) -> DownloadResult:
    """
    Public-only, no session or cookies. Hits different Instagram endpoints
    than yt-dlp and gallery-dl, giving it independent failure characteristics.
    """
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise DownloadError("Could not extract shortcode from URL", retryable=False)

    loader = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        dirname_pattern=output_dir,
        filename_pattern="{shortcode}",
        quiet=True,
    )

    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    loader.download_post(post, target=Path(output_dir))

    candidate = os.path.join(output_dir, f"{shortcode}.mp4")
    if os.path.exists(candidate):
        return DownloadResult(
            file_path=candidate,
            duration_seconds=float(post.video_duration) if post.video_duration else None,
        )

    for f in os.listdir(output_dir):
        if f.endswith(".mp4"):
            return DownloadResult(file_path=os.path.join(output_dir, f))

    raise DownloadError("instaloader completed but output file not found")


# ─── Fallback chain ─────────────────────────────────────────────────────────────

_DOWNLOADERS = [
    ("yt-dlp", _ytdlp_download),
    ("gallery-dl", _gallerydl_download),
    ("instaloader", _instaloader_download),
]


def download_video(url: str, output_dir: str) -> DownloadResult:
    """
    Try each downloader in sequence. Returns on first success.
    Raises DownloadError if all three fail.
    """
    os.makedirs(output_dir, exist_ok=True)
    last_error: Optional[Exception] = None

    for name, downloader_fn in _DOWNLOADERS:
        for attempt in range(MAX_RETRIES + 1):
            try:
                logger.info(f"[{name}] attempt {attempt + 1} for {url}")
                result = downloader_fn(url, output_dir)

                if not os.path.exists(result.file_path):
                    raise DownloadError(f"{name}: file path does not exist after download")
                if os.path.getsize(result.file_path) < 1024:
                    raise DownloadError(f"{name}: downloaded file is too small (likely corrupt)")

                logger.info(f"[{name}] success: {result.file_path}")
                return result

            except DownloadError as e:
                if not e.retryable:
                    logger.warning(f"[{name}] non-retryable error: {e}")
                    last_error = e
                    break
                last_error = e
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    logger.warning(f"[{name}] attempt {attempt + 1} failed, retrying in {delay}s: {e}")
                    time.sleep(delay)

            except Exception as e:
                last_error = e
                logger.warning(f"[{name}] attempt {attempt + 1} unexpected error: {e}")
                if attempt < MAX_RETRIES:
                    delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                    time.sleep(delay)

    raise DownloadError(
        f"All downloaders failed. Last error: {last_error}",
        retryable=True,
    )


async def download_video_async(url: str, output_dir: str) -> DownloadResult:
    """Async wrapper — runs the blocking download in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, download_video, url, output_dir)
