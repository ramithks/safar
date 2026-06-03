"""
Supabase Storage — upload files to the `trip-files` bucket.
The service-role client is used so server-side uploads bypass RLS.
"""
import logging
import uuid

logger = logging.getLogger(__name__)

BUCKET = "trip-files"


def upload_bytes(db_client, path: str, file_bytes: bytes, content_type: str) -> str:
    """
    Upload raw bytes to the trip-files bucket at `path`.
    Returns the public URL of the uploaded object.

    The bucket must be set to PUBLIC in the Supabase dashboard
    (Storage → trip-files → Make public), or you can use signed URLs instead.
    """
    db_client.storage.from_(BUCKET).upload(
        path=path,
        file=file_bytes,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    url: str = db_client.storage.from_(BUCKET).get_public_url(path)
    return url


def delete_file(db_client, path: str) -> None:
    """Delete a file from the bucket. Silently ignores missing files."""
    try:
        db_client.storage.from_(BUCKET).remove([path])
    except Exception as e:
        logger.warning(f"supabase_storage.delete_file({path}): {e}")
