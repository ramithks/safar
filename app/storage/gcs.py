"""
GCP Cloud Storage — upload files, generate signed PUT URLs, manage blobs.
Skipped gracefully if GCS_BUCKET_NAME is not configured.
"""
import json
import logging
import os
from datetime import timedelta
from functools import lru_cache

import sentry_sdk

from app.config import settings

logger = logging.getLogger(__name__)

_client = None


# ── Client factory (uses service-account JSON if available, else ADC) ─────────

@lru_cache(maxsize=1)
def _get_sa_info() -> dict | None:
    raw = (settings.google_service_account_json or "").strip()
    if not raw:
        return None
    sa = json.loads(raw)
    if "private_key" in sa and isinstance(sa["private_key"], str):
        sa["private_key"] = sa["private_key"].replace("\\n", "\n")
    return sa


def _get_client():
    global _client
    if _client is None:
        from google.cloud import storage
        sa_info = _get_sa_info()
        if sa_info:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(sa_info)
            _client = storage.Client(credentials=creds, project=sa_info["project_id"])
        else:
            _client = storage.Client()
    return _client


def _bucket_name() -> str:
    name = settings.gcs_bucket_name
    if not name:
        raise RuntimeError("GCS_BUCKET_NAME is not configured")
    return name


def public_url(blob_path: str) -> str:
    """Canonical public HTTPS URL for a blob (bucket must allow public reads)."""
    return f"https://storage.googleapis.com/{_bucket_name()}/{blob_path}"


# ── Signed upload URL (PUT) ───────────────────────────────────────────────────

def generate_signed_upload_url(
    blob_path: str,
    content_type: str,
    expiration_seconds: int = 900,
) -> tuple[str, str]:
    """
    Generate a v4 signed PUT URL for a direct client → GCS upload.
    Returns (signed_url, public_url).
    Public URL becomes valid once the client confirms the upload.
    """
    from google.cloud import storage
    client = _get_client()
    bucket = client.bucket(_bucket_name())
    blob = bucket.blob(blob_path)

    signed = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=expiration_seconds),
        method="PUT",
        content_type=content_type,
    )
    return signed, public_url(blob_path)


# ── Server-side bytes upload ──────────────────────────────────────────────────

def upload_bytes(blob_path: str, data: bytes, content_type: str, make_public: bool = True) -> str:
    """
    Upload raw bytes directly to GCS from the server.
    Returns the public URL of the uploaded blob.
    The bucket must allow public object reads (allUsers → Storage Object Viewer),
    or set make_public=False and use signed URLs instead.
    """
    client = _get_client()
    bucket = client.bucket(_bucket_name())
    blob = bucket.blob(blob_path)
    blob.upload_from_string(data, content_type=content_type)
    if make_public:
        blob.make_public()
    return public_url(blob_path)


# ── Post-upload helpers ───────────────────────────────────────────────────────

def make_blob_public(blob_path: str) -> str:
    """
    Make an already-uploaded blob publicly readable.
    Returns the public URL.
    """
    client = _get_client()
    bucket = client.bucket(_bucket_name())
    blob = bucket.blob(blob_path)
    blob.make_public()
    return public_url(blob_path)


def delete_blob(blob_path: str) -> None:
    """Delete a blob from GCS. Silently ignores NotFound."""
    try:
        client = _get_client()
        bucket = client.bucket(_bucket_name())
        blob = bucket.blob(blob_path)
        blob.delete()
        logger.info(f"Deleted GCS blob: {blob_path}")
    except Exception as e:
        logger.warning(f"GCS delete failed for {blob_path}: {e}")


# ── Legacy helpers (pipeline) ─────────────────────────────────────────────────

def upload_video(job_id: str, video_path: str, bucket_name: str) -> str:
    """Upload video file to GCS. Returns public URL."""
    ext = os.path.splitext(video_path)[1] or ".mp4"
    blob_path = f"videos/{job_id}/video{ext}"
    return _upload_file(video_path, bucket_name, blob_path)


def upload_frame(job_id: str, frame_path: str, frame_index: int, bucket_name: str) -> str:
    """Upload a single frame JPEG to GCS. Returns public URL."""
    blob_path = f"frames/{job_id}/frame_{frame_index:04d}.jpg"
    return _upload_file(frame_path, bucket_name, blob_path)


def upload_frames(job_id: str, frame_paths: list[str], bucket_name: str) -> dict[str, str]:
    url_map: dict[str, str] = {}
    for idx, path in enumerate(frame_paths):
        try:
            url = upload_frame(job_id, path, idx, bucket_name)
            url_map[path] = url
        except Exception as e:
            logger.warning(f"Failed to upload frame {path}: {e}")
            # Surface to Sentry — these are silently swallowed otherwise, so a
            # systemic failure (e.g. missing bucket IAM → 403) went unnoticed.
            sentry_sdk.set_context("frame_upload", {"job_id": job_id, "path": path})
            sentry_sdk.capture_exception(e)
    return url_map


def _upload_file(local_path: str, bucket_name: str, blob_path: str) -> str:
    client = _get_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    blob.make_public()
    url = blob.public_url
    logger.info(f"Uploaded {local_path} → {url}")
    return url
