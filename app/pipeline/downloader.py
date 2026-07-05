"""
Video downloader with a fallback chain (strictly no auth/cookies/tokens):
  1. downreels       — third-party fetch API (downreels.com); does the Instagram
                       access on ITS servers, so IG blocks don't apply to us
  2. videodropper    — third-party fetch API behind fastvideosave.net; same
                       server-side-access advantage as downreels
  3. yt-dlp          — direct extraction; fails fast on login-required errors
  4. embed-scraper   — pure httpx, scrapes Instagram's embed page for the CDN URL

PROXY-ONLY: when INSTAGRAM_PROXY_URL is configured, every attempt goes through
the rotating proxy — Instagram has the server's own IP blocked, so a direct
attempt can never succeed; it only leaks our IP and wastes time. The rotating
proxy hands out a fresh exit IP per connection, so reliability comes from more
retries through the proxy, not from a direct fallback.
"""
import os
import re
import time
import random
import asyncio
import logging
from typing import Optional

import yt_dlp
import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES_DIRECT = 1     # retries per downloader without a proxy
MAX_RETRIES_PROXY = 3      # retries per downloader through the rotating proxy —
                           # each retry opens a new connection = fresh exit IP,
                           # so every extra retry is a fresh shot past an IP block
RETRY_DELAYS = [3, 5, 8]   # seconds between retries (fresh IP needs no long cooldown)

# CDN streaming: connect (incl. TLS handshake) must fail fast — a stalled
# handshake through the rotating proxy otherwise eats the whole timeout budget
# before the retry (= fresh exit IP) gets a chance. read is per-chunk, so 60s
# still tolerates a slow stream.
_CDN_TIMEOUT = httpx.Timeout(connect=15, read=60, write=60, pool=15)

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
            # "login required" / "empty media response" is IP-reputation-based
            # for public posts: on a rotating proxy a retry gets a fresh exit
            # IP and often succeeds, so only give up when there is no proxy.
            raise DownloadError(
                f"yt-dlp: Instagram login required: {exc}",
                retryable=proxy is not None,
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

    embed_urls = [
        f"https://www.instagram.com/reel/{shortcode}/embed/captioned/",
        f"https://www.instagram.com/reel/{shortcode}/embed/",
    ]
    headers = {
        "User-Agent": _random_ua(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

    client_kwargs: dict = {
        "timeout": httpx.Timeout(connect=15, read=30, write=30, pool=15),
        "follow_redirects": True,
        "headers": headers,
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    caption: Optional[str] = None
    video_url: Optional[str] = None
    last_status: Optional[int] = None

    with httpx.Client(**client_kwargs) as client:
        for embed_url in embed_urls:
            resp = client.get(embed_url)
            last_status = resp.status_code
            if resp.status_code != 200:
                continue

            html = resp.text

            if caption is None:
                m = re.search(r'"caption"\s*:\s*"([^"]*)"', html)
                if m:
                    caption = m.group(1)

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

            if video_url:
                break

    if not video_url:
        if last_status == 429:
            raise DownloadError("embed-scraper: 429 rate limit", retryable=True)
        if last_status != 200:
            raise DownloadError(f"embed-scraper: HTTP {last_status}", retryable=True)
        raise DownloadError("embed-scraper: could not find video URL in embed page", retryable=True)

    output_path = os.path.join(output_dir, "video.mp4")
    logger.info(f"embed-scraper: downloading CDN video for {shortcode}")

    dl_kwargs: dict = {
        "timeout": _CDN_TIMEOUT,
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


# ─── third-party fetch APIs (no cookies/tokens; IG access happens on THEIR side) ─
#
# These resolvers make the Instagram request from their OWN infrastructure and
# hand us back a direct CDN URL, so our IP reputation is irrelevant. Only the
# final CDN download runs through us (and the proxy, when configured).

_DOWNREELS_API = "https://downreels.com/api/fetch.php"
_VIDEODROPPER_API = "https://api.videodropper.app/allinone"
# AES-128-ECB key + PKCS7, hex output — fastvideosave.net encrypts the reel URL
# into the `url` request header this way (no token/cookie; key is baked into
# their public JS bundle). Verified reproducing their sample header 2026-07-05.
_VIDEODROPPER_KEY = b"qwertyuioplkjhgf"


def _stream_cdn_to_file(video_url: str, output_path: str, user_agent: str,
                        proxy: Optional[str]) -> None:
    """Stream a resolved CDN video URL to disk (through the proxy if given)."""
    dl_kwargs: dict = {
        "timeout": _CDN_TIMEOUT,
        "follow_redirects": True,
        "headers": {"User-Agent": user_agent},
    }
    if proxy:
        dl_kwargs["proxy"] = proxy

    with httpx.Client(**dl_kwargs) as client:
        with client.stream("GET", video_url) as stream:
            stream.raise_for_status()
            with open(output_path, "wb") as fh:
                for chunk in stream.iter_bytes(chunk_size=65536):
                    fh.write(chunk)


def _downreels_download(url: str, output_dir: str, proxy: Optional[str] = None) -> DownloadResult:
    """Resolve the CDN video URL via downreels.com's public fetch API."""
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise DownloadError("downreels: could not extract shortcode", retryable=False)

    canonical = f"https://www.instagram.com/reel/{shortcode}/"
    user_agent = _random_ua()
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": "https://downreels.com",
        "Referer": "https://downreels.com/en/download-video-instagram/",
    }

    client_kwargs: dict = {
        "timeout": httpx.Timeout(connect=15, read=45, write=45, pool=15),
        "follow_redirects": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    with httpx.Client(**client_kwargs) as client:
        resp = client.post(_DOWNREELS_API, json={"url": canonical}, headers=headers)

    if resp.status_code == 429:
        raise DownloadError("downreels: 429 rate limit", retryable=True)
    if resp.status_code != 200:
        raise DownloadError(f"downreels: HTTP {resp.status_code}", retryable=True)

    try:
        data = resp.json()
    except Exception:
        raise DownloadError("downreels: non-JSON response", retryable=True)

    if data.get("status") != "ok":
        raise DownloadError(f"downreels: status={data.get('status')!r}", retryable=True)

    videos = data.get("videos") or []
    video_url: Optional[str] = None
    for item in videos:
        if item.get("url"):
            video_url = item["url"]
            if item.get("quality") == "HD":
                break
    if not video_url:
        raise DownloadError("downreels: no video URL in response", retryable=True)

    output_path = os.path.join(output_dir, "video.mp4")
    logger.info(f"downreels: downloading CDN video for {shortcode}")
    _stream_cdn_to_file(video_url, output_path, user_agent, proxy)
    return DownloadResult(file_path=output_path)


def _videodropper_encrypt_url(reel_url: str) -> str:
    """Reproduce fastvideosave.net's `url` header: AES-128-ECB + PKCS7, hex."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(128).padder()
    padded = padder.update(reel_url.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_VIDEODROPPER_KEY), modes.ECB()).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()


def _videodropper_download(url: str, output_dir: str, proxy: Optional[str] = None) -> DownloadResult:
    """Resolve the CDN video URL via videodropper.app (fastvideosave.net's API)."""
    shortcode = _extract_shortcode(url)
    if not shortcode:
        raise DownloadError("videodropper: could not extract shortcode", retryable=False)

    canonical = f"https://www.instagram.com/reel/{shortcode}/"
    user_agent = _random_ua()
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Origin": "https://fastvideosave.net",
        "Referer": "https://fastvideosave.net/",
        "url": _videodropper_encrypt_url(canonical),
    }

    client_kwargs: dict = {
        "timeout": httpx.Timeout(connect=15, read=45, write=45, pool=15),
        "follow_redirects": True,
    }
    if proxy:
        client_kwargs["proxy"] = proxy

    with httpx.Client(**client_kwargs) as client:
        resp = client.get(_VIDEODROPPER_API, headers=headers)

    if resp.status_code == 429:
        raise DownloadError("videodropper: 429 rate limit", retryable=True)
    if resp.status_code != 200:
        raise DownloadError(f"videodropper: HTTP {resp.status_code}", retryable=True)

    try:
        data = resp.json()
    except Exception:
        raise DownloadError("videodropper: non-JSON response", retryable=True)

    videos = data.get("video") or []
    video_url: Optional[str] = None
    for item in videos:
        if item.get("video"):
            video_url = item["video"]
            break
    if not video_url:
        raise DownloadError("videodropper: no video URL in response", retryable=True)

    output_path = os.path.join(output_dir, "video.mp4")
    logger.info(f"videodropper: downloading CDN video for {shortcode}")
    _stream_cdn_to_file(video_url, output_path, user_agent, proxy)
    return DownloadResult(file_path=output_path)


# ─── Fallback chain ─────────────────────────────────────────────────────────────

# (label, module attribute name) — resolved via globals() at call time so
# unittest.mock.patch on the module attribute affects the chain.
_DOWNLOADERS = [
    ("downreels", "_downreels_download"),
    ("videodropper", "_videodropper_download"),
    ("yt-dlp", "_ytdlp_download"),
    ("embed-scraper", "_embed_scrape_download"),
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
    max_retries = MAX_RETRIES_PROXY if proxy else MAX_RETRIES_DIRECT
    last: Optional[Exception] = None
    for attempt in range(max_retries + 1):
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
            # Auth walls are IP-reputation-based; retrying through the rotating
            # proxy gets a fresh exit IP, so only give up when running direct.
            if _is_auth_error(exc) and not proxy:
                logger.warning(f"[{name}] auth error ({label}): {exc}")
                break
            logger.warning(f"[{name}] {label} attempt {attempt + 1} unexpected error: {exc}")

        if attempt < max_retries:
            delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
            logger.warning(f"[{name}] {label} attempt {attempt + 1} failed, retry in {delay}s: {last}")
            time.sleep(delay)

    assert last is not None
    raise last


def download_video(url: str, output_dir: str) -> DownloadResult:
    """
    Runs the fallback chain. When a proxy is configured, EVERY attempt goes
    through it — the server's own IP is blocked by Instagram, so a direct
    fallback can never succeed; it only leaks our IP and wastes time. Each
    proxy retry opens a fresh connection (= fresh exit IP), so reliability
    comes from retries through the proxy, not from a direct mode.

    Raises DownloadError if all downloaders fail; the message carries every
    downloader's last error for debugging.
    """
    os.makedirs(output_dir, exist_ok=True)
    proxy = _get_proxy_url()
    label = "proxy" if proxy else "direct"
    failures: dict[str, str] = {}   # per-downloader last error, for the final report

    for name, fn_attr in _DOWNLOADERS:
        fn = globals()[fn_attr]
        try:
            return _try_downloader(name, fn, url, output_dir, proxy, label)
        except DownloadError as exc:
            if exc.fatal:
                # Definitive verdict about the post itself (e.g. no video) —
                # no other downloader can succeed, stop the chain.
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
