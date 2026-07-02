"""
Video downloader with a fallback chain (no auth/cookies required):
  1. yt-dlp          — most reliable; fast-fails on explicit login-required errors
  2. embed-scraper   — pure httpx, scrapes Instagram's embed page for the CDN video URL
  3. graphql-scraper — Instagram's public GraphQL endpoint (PolarisPostActionLoadPostQuery)
  4. gallery-dl      — different HTTP extraction path; runs with a hard timeout

PROXY-FIRST: when INSTAGRAM_PROXY_URL is configured, every downloader goes
through the rotating proxy first so Instagram never sees (and rate-limits/blocks)
the server's own IP. A direct attempt is only the fallback for when the proxy
itself is down or exhausted.
"""
import os
import re
import json
import time
import random
import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional

import yt_dlp
import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 1            # retries per (downloader, proxy-mode) pair
RETRY_DELAYS = [5, 15]    # seconds between retries

# ─── User-Agent pool ─────────────────────────────────────────────────────────────
# Rotate across realistic mobile + desktop UAs to reduce fingerprinting.

_USER_AGENTS = [
    # Android Chrome
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; OnePlus 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    # iOS Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    # Desktop Chrome
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Desktop Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) Gecko/20100101 Firefox/124.0",
]


def _random_ua() -> str:
    return random.choice(_USER_AGENTS)


# ─── Error classification ────────────────────────────────────────────────────────

def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in ("login required", "login page", "use --cookies", "netrc"))


class DownloadError(Exception):
    """Raised when all downloaders have failed.

    fatal=True means the failure is definitive for the post itself (e.g. it has
    no video at all) — the fallback chain stops immediately instead of burning
    time on downloaders that cannot succeed either.
    """
    def __init__(self, message: str, retryable: bool = False, fatal: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.fatal = fatal


class DownloadResult:
    def __init__(
        self,
        file_path: str,
        duration_seconds: Optional[float] = None,
        caption: Optional[str] = None,
    ):
        self.file_path = file_path
        self.duration_seconds = duration_seconds
        self.caption = caption


# ─── helpers ────────────────────────────────────────────────────────────────────

def _extract_shortcode(url: str) -> Optional[str]:
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


def extract_shortcode(url: str) -> Optional[str]:
    """Public alias: pull the post shortcode out of any Instagram URL variant."""
    return _extract_shortcode(url)


def canonical_reel_url(url: str) -> Optional[str]:
    """
    Normalize any Instagram reel/post URL variant (/reel/ vs /reels/, share
    query params, trailing slashes) to one canonical form so caching and
    dedup keys match across submissions of the same content.
    """
    shortcode = _extract_shortcode(url)
    if not shortcode:
        return None
    return f"https://www.instagram.com/reel/{shortcode}/"


def _jitter_sleep():
    """Short random delay to reduce rate-limit risk."""
    delay = random.uniform(1.0, 3.0)
    logger.debug(f"[jitter] sleeping {delay:.1f}s")
    time.sleep(delay)


def _get_proxy_url() -> Optional[str]:
    from app.config import settings
    return settings.instagram_proxy_url


# ─── yt-dlp ─────────────────────────────────────────────────────────────────────

def _ytdlp_download(url: str, output_dir: str, proxy: Optional[str] = None) -> DownloadResult:
    from app.config import settings

    output_path = os.path.join(output_dir, "video.%(ext)s")

    ydl_opts = {
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
        "retries": 0,
        "http_chunk_size": 10485760,
        "max_filesize": settings.max_video_size_mb * 1024 * 1024,
        "http_headers": {"User-Agent": _random_ua()},
    }

    if proxy:
        ydl_opts["proxy"] = proxy

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            duration = info.get("duration")
            caption = info.get("description") or info.get("title") or None
    except Exception as exc:
        if _is_auth_error(exc):
            raise DownloadError(
                f"yt-dlp: Instagram login required: {exc}",
                retryable=False,
            )
        raise

    for ext in ["mp4", "webm", "mkv", "mov"]:
        candidate = os.path.join(output_dir, f"video.{ext}")
        if os.path.exists(candidate):
            return DownloadResult(file_path=candidate, duration_seconds=duration, caption=caption)

    for f in os.listdir(output_dir):
        if f.startswith("video.") and not f.endswith(".part"):
            return DownloadResult(
                file_path=os.path.join(output_dir, f),
                duration_seconds=duration,
                caption=caption,
            )

    raise DownloadError("yt-dlp completed but output file not found")


# ─── embed-page scraper (no cookies, pure httpx) ────────────────────────────────

_VIDEO_URL_PATTERNS = [
    r'"video_url"\s*:\s*"(https://[^"]+\.mp4[^"]*)"',
    r'"contentUrl"\s*:\s*"(https://[^"]+)"',
    r'<video[^>]+src="(https://[^"]+\.mp4[^"]*)"',
    r'"playable_url"\s*:\s*"(https://[^"]+)"',
    r'"playable_url_quality_hd"\s*:\s*"(https://[^"]+)"',
]


def _unescape_url(raw: str) -> str:
    return raw.replace("\\u0026", "&").replace("\\/", "/").replace("\\u003C", "<")


def _embed_scrape_download(url: str, output_dir: str, proxy: Optional[str] = None) -> DownloadResult:
    """
    Scrape Instagram's public embed page to find the CDN video URL.
    No authentication or cookies required — only works for public posts.
    """
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise DownloadError("embed-scraper: could not extract shortcode", retryable=False)

    embed_url = f"https://www.instagram.com/reel/{shortcode}/embed/captioned/"
    headers = {
        "User-Agent": _random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

    client_kwargs: dict = {"timeout": 30, "follow_redirects": True, "headers": headers}
    if proxy:
        client_kwargs["proxy"] = proxy

    with httpx.Client(**client_kwargs) as client:
        resp = client.get(embed_url)

    if resp.status_code == 429:
        raise DownloadError("embed-scraper: 429 rate limit", retryable=True)
    if resp.status_code != 200:
        raise DownloadError(
            f"embed-scraper: HTTP {resp.status_code}", retryable=True
        )

    html = resp.text

    caption: Optional[str] = None
    m = re.search(r'"caption"\s*:\s*"([^"]*)"', html)
    if m:
        caption = m.group(1)

    video_url: Optional[str] = None
    for pattern in _VIDEO_URL_PATTERNS:
        m = re.search(pattern, html)
        if m:
            video_url = _unescape_url(m.group(1))
            break

    if not video_url:
        for script_content in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
            for pattern in _VIDEO_URL_PATTERNS:
                m = re.search(pattern, script_content)
                if m:
                    video_url = _unescape_url(m.group(1))
                    break
            if video_url:
                break

    if not video_url:
        raise DownloadError("embed-scraper: could not find video URL in embed page", retryable=True)

    output_path = os.path.join(output_dir, "video.mp4")
    logger.info(f"embed-scraper: downloading CDN video for {shortcode}")

    dl_kwargs: dict = {
        "timeout": 120,
        "follow_redirects": True,
        "headers": {"User-Agent": headers["User-Agent"]},
    }
    if proxy:
        dl_kwargs["proxy"] = proxy

    with httpx.Client(**dl_kwargs) as client:
        with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with open(output_path, "wb") as fh:
                for chunk in stream.iter_bytes(chunk_size=65536):
                    fh.write(chunk)

    return DownloadResult(file_path=output_path, caption=caption)


# ─── GraphQL scraper (Instagram's public post query, no cookies) ────────────────

_IG_APP_ID = "936619743392459"                       # instagram.com web app id
_IG_GRAPHQL_DOC_ID = "8845758582119845"              # PolarisPostActionLoadPostQueryQuery


def _graphql_download(url: str, output_dir: str, proxy: Optional[str] = None) -> DownloadResult:
    """
    Fetch the post via Instagram's public GraphQL endpoint — the same query the
    web client issues for logged-out users. Works for public posts; also tells
    us definitively when a post has no video (image/carousel post).
    """
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise DownloadError("graphql: could not extract shortcode", retryable=False)

    headers = {
        "User-Agent": _random_ua(),
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-IG-App-ID": _IG_APP_ID,
        "X-FB-Friendly-Name": "PolarisPostActionLoadPostQueryQuery",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.instagram.com",
        "Referer": f"https://www.instagram.com/reel/{shortcode}/",
    }
    payload = {
        "variables": json.dumps({
            "shortcode": shortcode,
            "fetch_tagged_user_count": None,
            "hoisted_comment_id": None,
            "hoisted_reply_id": None,
        }),
        "doc_id": _IG_GRAPHQL_DOC_ID,
    }

    client_kwargs: dict = {"timeout": 30, "follow_redirects": True}
    if proxy:
        client_kwargs["proxy"] = proxy

    with httpx.Client(**client_kwargs) as client:
        resp = client.post("https://www.instagram.com/graphql/query", data=payload, headers=headers)

    if resp.status_code in (401, 403, 429):
        raise DownloadError(f"graphql: HTTP {resp.status_code} (blocked)", retryable=True)
    if resp.status_code != 200:
        raise DownloadError(f"graphql: HTTP {resp.status_code}", retryable=True)

    try:
        media = (resp.json().get("data") or {}).get("xdt_shortcode_media") or {}
    except Exception:
        raise DownloadError("graphql: non-JSON response (likely blocked)", retryable=True)

    if not media:
        raise DownloadError("graphql: no media in response (private or deleted post?)", retryable=True)
    if not media.get("is_video"):
        raise DownloadError("This Instagram post is not a video (image post)", retryable=False, fatal=True)

    video_url = media.get("video_url")
    if not video_url:
        raise DownloadError("graphql: media has no video_url", retryable=True)

    caption: Optional[str] = None
    try:
        edges = (media.get("edge_media_to_caption") or {}).get("edges") or []
        if edges:
            caption = edges[0]["node"]["text"]
    except Exception:
        pass
    duration = media.get("video_duration")

    output_path = os.path.join(output_dir, "video.mp4")
    logger.info(f"graphql: downloading CDN video for {shortcode}")

    dl_kwargs: dict = {"timeout": 120, "follow_redirects": True,
                       "headers": {"User-Agent": headers["User-Agent"]}}
    if proxy:
        dl_kwargs["proxy"] = proxy

    with httpx.Client(**dl_kwargs) as client:
        with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with open(output_path, "wb") as fh:
                for chunk in stream.iter_bytes(chunk_size=65536):
                    fh.write(chunk)

    return DownloadResult(file_path=output_path, duration_seconds=duration, caption=caption)


# ─── gallery-dl (with hard timeout) ─────────────────────────────────────────────

_GALLERY_DL_TIMEOUT = 60  # seconds; abort if stalled on 429 waits


def _gallerydl_download(url: str, output_dir: str, proxy: Optional[str] = None) -> DownloadResult:
    """
    Uses gallery-dl with a hard wall-clock timeout so 429-induced sleeps
    cannot block the worker indefinitely.
    """
    import gallery_dl

    result_holder: dict = {}
    exc_holder: dict = {}

    def _run():
        try:
            gallery_dl.config.clear()
            gallery_dl.config.set((), "base-directory", output_dir)
            gallery_dl.config.set((), "filename", "gdl_video")
            gallery_dl.config.set(("extractor", "instagram"), "videos", True)
            gallery_dl.config.set(("extractor", "instagram"), "reels", True)
            gallery_dl.config.set(("downloader",), "part", False)
            gallery_dl.config.set(("extractor",), "sleep-request", 0)
            gallery_dl.config.set(("extractor",), "retries", 0)
            gallery_dl.config.set(
                ("extractor", "instagram"),
                "user-agent",
                _random_ua(),
            )
            if proxy:
                gallery_dl.config.set((), "proxy", proxy)

            before = set(os.listdir(output_dir))
            job = gallery_dl.job.DownloadJob(url)
            job.run()
            after = set(os.listdir(output_dir))

            video_extensions = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
            for fname in after - before:
                if Path(fname).suffix.lower() in video_extensions:
                    result_holder["path"] = os.path.join(output_dir, fname)
                    return

            exc_holder["error"] = DownloadError("gallery-dl: no video file found after download")
        except Exception as exc:
            exc_holder["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=_GALLERY_DL_TIMEOUT)

    if thread.is_alive():
        raise DownloadError(
            f"gallery-dl: timed out after {_GALLERY_DL_TIMEOUT}s (likely stalled on 429)",
            retryable=True,
        )

    if "error" in exc_holder:
        raise exc_holder["error"]

    if "path" not in result_holder:
        raise DownloadError("gallery-dl: completed but no video file found")

    return DownloadResult(file_path=result_holder["path"])


# ─── Fallback chain ─────────────────────────────────────────────────────────────

# (label, module attribute name) — resolved via globals() at call time so
# unittest.mock.patch on the module attribute affects the chain.
_DOWNLOADERS = [
    ("yt-dlp", "_ytdlp_download"),
    ("embed-scraper", "_embed_scrape_download"),
    ("graphql", "_graphql_download"),
    ("gallery-dl", "_gallerydl_download"),
]


def _attempt(name: str, fn, url: str, output_dir: str, proxy: Optional[str]) -> DownloadResult:
    """Single attempt: jitter → call → validate."""
    _jitter_sleep()
    result = fn(url, output_dir, proxy=proxy)
    if not os.path.exists(result.file_path):
        raise DownloadError(f"{name}: file path does not exist after download")
    if os.path.getsize(result.file_path) < 1024:
        raise DownloadError(f"{name}: downloaded file too small (likely corrupt)")
    return result


def _try_downloader(name: str, fn, url: str, output_dir: str,
                    proxy: Optional[str], label: str) -> DownloadResult:
    """
    Run one downloader under a single proxy mode, with retries for retryable
    errors. Returns a DownloadResult or raises the last error.
    Fatal errors (definitive verdicts about the post itself) propagate as-is.
    """
    last: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            logger.info(f"[{name}] {label} attempt {attempt + 1} for {url}")
            result = _attempt(name, fn, url, output_dir, proxy=proxy)
            logger.info(f"[{name}] success ({label}): {result.file_path}")
            return result
        except DownloadError as exc:
            if exc.fatal:
                logger.warning(f"[{name}] fatal ({label}): {exc}")
                raise
            last = exc
            if not exc.retryable:
                logger.warning(f"[{name}] non-retryable ({label}): {exc}")
                break
        except Exception as exc:
            last = exc
            if _is_auth_error(exc):
                logger.warning(f"[{name}] auth error ({label}): {exc}")
                break
            logger.warning(f"[{name}] {label} attempt {attempt + 1} unexpected error: {exc}")

        if attempt < MAX_RETRIES:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            logger.warning(f"[{name}] {label} attempt {attempt + 1} failed, retry in {delay}s: {last}")
            time.sleep(delay)

    assert last is not None
    raise last


def download_video(url: str, output_dir: str) -> DownloadResult:
    """
    For each downloader:
      1. Try WITH the rotating proxy first (if configured) — Instagram
         rate-limits/blocks server IPs fast; the proxy exists precisely so
         Instagram never sees our own IP.
      2. Fall back to a direct attempt (proxy down/exhausted).
      3. Move to the next downloader if both modes fail.

    Raises DownloadError if all downloaders × both modes fail; the message
    carries every downloader's last error for debugging.
    """
    os.makedirs(output_dir, exist_ok=True)
    proxy = _get_proxy_url()
    modes = [("proxy", proxy), ("direct", None)] if proxy else [("direct", None)]
    failures: dict[str, str] = {}   # per-downloader last error, for the final report

    for name, fn_attr in _DOWNLOADERS:
        fn = globals()[fn_attr]
        for label, mode_proxy in modes:
            try:
                return _try_downloader(name, fn, url, output_dir, mode_proxy, label)
            except DownloadError as exc:
                if exc.fatal:
                    # Definitive verdict about the post itself (e.g. no video) —
                    # no other downloader or mode can succeed, stop the chain.
                    raise
                failures[name] = str(exc)
            except Exception as exc:
                failures[name] = str(exc)

    detail = " | ".join(f"{n}: {e}" for n, e in failures.items()) or "no downloaders ran"
    raise DownloadError(
        f"All downloaders failed. {detail}",
        retryable=True,
    )


async def download_video_async(url: str, output_dir: str) -> DownloadResult:
    """Async wrapper — runs the blocking download in a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, download_video, url, output_dir)
