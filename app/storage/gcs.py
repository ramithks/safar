"""
GCP Cloud Storage — upload videos and frames, return public URLs.
Uploads are skipped gracefully if GCS_BUCKET_NAME is not configured.
"""
import logging
import os

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from google.cloud import storage
        _client = storage.Client()
    return _client


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
    """
    Upload all frames to GCS.
    Returns dict mapping local frame_path → GCS public URL.
    """
    url_map: dict[str, str] = {}
    for idx, path in enumerate(frame_paths):
        try:
            url = upload_frame(job_id, path, idx, bucket_name)
            url_map[path] = url
        except Exception as e:
            logger.warning(f"Failed to upload frame {path}: {e}")
    return url_map


def _upload_file(local_path: str, bucket_name: str, blob_path: str) -> str:
    client = _get_client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(local_path)
    # Make publicly readable
    blob.make_public()
    url = blob.public_url
    logger.info(f"Uploaded {local_path} → {url}")
    return url
