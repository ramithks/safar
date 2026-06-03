"""
Trip media service — reels, bookings, documents stored in Supabase,
files hosted on GCS via signed PUT URLs.
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta

from app.db.client import get_db
from app.config import settings
import app.storage.gcs as gcs

logger = logging.getLogger(__name__)

_SIGNED_URL_TTL = 900  # 15 minutes


def _db():
    return get_db()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gcs_ready() -> bool:
    return bool(settings.gcs_bucket_name)


# ── Reels ─────────────────────────────────────────────────────────────────────

def init_reel_upload(trip_id: str, user_id: str, title: str, category: str, filename: str, content_type: str) -> dict:
    """
    Create a pending reel record and return a signed GCS PUT URL.
    The client PUTs the video to the signed URL, then calls confirm_reel_upload.
    """
    reel_id = str(uuid.uuid4())
    safe_name = filename.replace(" ", "_")
    blob_path = f"trips/{trip_id}/reels/{reel_id}/{safe_name}"

    signed_url = None
    public = gcs.public_url(blob_path) if _gcs_ready() else None

    if _gcs_ready():
        try:
            signed_url, public = gcs.generate_signed_upload_url(blob_path, content_type, _SIGNED_URL_TTL)
        except Exception as e:
            logger.error(f"GCS signed URL failed: {e}")

    db = _db()
    db.table("trip_reels").insert({
        "id": reel_id,
        "trip_id": trip_id,
        "user_id": user_id,
        "title": title,
        "category": category,
        "gcs_path": blob_path,
        "gcs_url": public or "",
        "status": "pending",
    }).execute()

    return {
        "reel_id": reel_id,
        "upload_url": signed_url,
        "gcs_path": blob_path,
        "public_url": public,
        "expires_in": _SIGNED_URL_TTL,
        "content_type": content_type,
        "note": "PUT the file to upload_url with the matching Content-Type header, then call /confirm.",
    }


def confirm_reel_upload(trip_id: str, reel_id: str, user_id: str, duration: str | None) -> dict:
    db = _db()
    res = (
        db.table("trip_reels")
        .select("*")
        .eq("id", reel_id)
        .eq("trip_id", trip_id)
        .maybe_single()
        .execute()
    )
    if not res.data:
        raise ValueError("Reel not found")

    blob_path = res.data["gcs_path"]
    public = res.data["gcs_url"]

    if _gcs_ready() and blob_path:
        try:
            public = gcs.make_blob_public(blob_path)
        except Exception as e:
            logger.warning(f"make_blob_public failed for {blob_path}: {e}")

    updates: dict = {"status": "uploaded", "gcs_url": public}
    if duration:
        updates["duration"] = duration

    updated = (
        db.table("trip_reels")
        .update(updates)
        .eq("id", reel_id)
        .execute()
    )
    return updated.data[0]


def list_reels(trip_id: str, user_id: str) -> list[dict]:
    db = _db()
    res = (
        db.table("trip_reels")
        .select("*")
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def delete_reel(trip_id: str, reel_id: str, user_id: str) -> None:
    db = _db()
    res = (
        db.table("trip_reels")
        .select("gcs_path")
        .eq("id", reel_id)
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if res.data and res.data.get("gcs_path") and _gcs_ready():
        gcs.delete_blob(res.data["gcs_path"])

    db.table("trip_reels").delete().eq("id", reel_id).eq("trip_id", trip_id).eq("user_id", user_id).execute()


# ── Bookings ──────────────────────────────────────────────────────────────────

def create_booking(trip_id: str, user_id: str, **fields) -> dict:
    db = _db()
    # Map frontend attachment fields → DB columns
    attachment_url = fields.pop("attachment_url", None)
    attachment_name = fields.pop("attachment_name", None)
    row: dict = {"trip_id": trip_id, "user_id": user_id, **fields}
    if attachment_url:
        row["file_gcs_url"] = attachment_url
        row["has_file"] = True
    if attachment_name:
        row["attachment_name"] = attachment_name
    res = db.table("trip_bookings").insert(row).execute()
    return res.data[0]


def list_bookings(trip_id: str, user_id: str) -> list[dict]:
    db = _db()
    res = (
        db.table("trip_bookings")
        .select("*")
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def update_booking(trip_id: str, booking_id: str, user_id: str, updates: dict) -> dict:
    db = _db()
    res = (
        db.table("trip_bookings")
        .update({k: v for k, v in updates.items() if v is not None})
        .eq("id", booking_id)
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not res.data:
        raise ValueError("Booking not found")
    return res.data[0]


def delete_booking(trip_id: str, booking_id: str, user_id: str) -> None:
    db = _db()
    res = (
        db.table("trip_bookings")
        .select("file_gcs_path")
        .eq("id", booking_id)
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if res.data and res.data.get("file_gcs_path") and _gcs_ready():
        gcs.delete_blob(res.data["file_gcs_path"])

    db.table("trip_bookings").delete().eq("id", booking_id).eq("trip_id", trip_id).eq("user_id", user_id).execute()


def init_booking_file_upload(trip_id: str, booking_id: str, user_id: str, filename: str, content_type: str) -> dict:
    safe_name = filename.replace(" ", "_")
    blob_path = f"trips/{trip_id}/bookings/{booking_id}/{safe_name}"

    signed_url = None
    public = gcs.public_url(blob_path) if _gcs_ready() else None

    if _gcs_ready():
        try:
            signed_url, public = gcs.generate_signed_upload_url(blob_path, content_type, _SIGNED_URL_TTL)
        except Exception as e:
            logger.error(f"GCS booking file URL failed: {e}")

    db = _db()
    db.table("trip_bookings").update({
        "file_gcs_path": blob_path,
        "file_gcs_url": public or "",
    }).eq("id", booking_id).eq("trip_id", trip_id).eq("user_id", user_id).execute()

    return {
        "booking_id": booking_id,
        "upload_url": signed_url,
        "gcs_path": blob_path,
        "public_url": public,
        "expires_in": _SIGNED_URL_TTL,
        "content_type": content_type,
        "note": "PUT the file to upload_url, then call /confirm-file.",
    }


def confirm_booking_file(trip_id: str, booking_id: str, user_id: str) -> dict:
    db = _db()
    res = (
        db.table("trip_bookings")
        .select("file_gcs_path, file_gcs_url")
        .eq("id", booking_id)
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not res.data:
        raise ValueError("Booking not found")

    blob_path = res.data.get("file_gcs_path")
    public = res.data.get("file_gcs_url", "")

    if _gcs_ready() and blob_path:
        try:
            public = gcs.make_blob_public(blob_path)
        except Exception as e:
            logger.warning(f"make_blob_public failed: {e}")

    updated = (
        db.table("trip_bookings")
        .update({"file_gcs_url": public, "has_file": True})
        .eq("id", booking_id)
        .execute()
    )
    return updated.data[0]


# ── Documents ─────────────────────────────────────────────────────────────────

def init_document_upload(trip_id: str, user_id: str, name: str, category: str, filename: str, content_type: str) -> dict:
    doc_id = str(uuid.uuid4())
    safe_name = filename.replace(" ", "_")
    blob_path = f"trips/{trip_id}/documents/{doc_id}/{safe_name}"

    signed_url = None
    public = gcs.public_url(blob_path) if _gcs_ready() else None

    if _gcs_ready():
        try:
            signed_url, public = gcs.generate_signed_upload_url(blob_path, content_type, _SIGNED_URL_TTL)
        except Exception as e:
            logger.error(f"GCS document URL failed: {e}")

    db = _db()
    db.table("trip_documents").insert({
        "id": doc_id,
        "trip_id": trip_id,
        "user_id": user_id,
        "name": name,
        "category": category,
        "file_name": filename,
        "gcs_path": blob_path,
        "gcs_url": public or "",
        "status": "pending",
    }).execute()

    return {
        "doc_id": doc_id,
        "upload_url": signed_url,
        "gcs_path": blob_path,
        "public_url": public,
        "expires_in": _SIGNED_URL_TTL,
        "content_type": content_type,
        "note": "PUT the file to upload_url with the matching Content-Type header, then call /confirm.",
    }


def confirm_document_upload(trip_id: str, doc_id: str, user_id: str) -> dict:
    db = _db()
    res = (
        db.table("trip_documents")
        .select("*")
        .eq("id", doc_id)
        .eq("trip_id", trip_id)
        .maybe_single()
        .execute()
    )
    if not res.data:
        raise ValueError("Document not found")

    blob_path = res.data["gcs_path"]
    public = res.data["gcs_url"]

    if _gcs_ready() and blob_path:
        try:
            public = gcs.make_blob_public(blob_path)
        except Exception as e:
            logger.warning(f"make_blob_public failed: {e}")

    updated = (
        db.table("trip_documents")
        .update({"status": "uploaded", "gcs_url": public})
        .eq("id", doc_id)
        .execute()
    )
    return updated.data[0]


def create_document_direct(trip_id: str, user_id: str, name: str, category: str, filename: str, file_url: str) -> dict:
    """Create a document record with a Supabase Storage URL (no GCS init/confirm needed)."""
    doc_id = str(uuid.uuid4())
    db = _db()
    res = db.table("trip_documents").insert({
        "id": doc_id,
        "trip_id": trip_id,
        "user_id": user_id,
        "name": name,
        "category": category,
        "file_name": filename,
        "gcs_path": "",
        "gcs_url": file_url,
        "status": "uploaded",
    }).execute()
    return res.data[0]


def list_documents(trip_id: str, user_id: str) -> list[dict]:
    db = _db()
    res = (
        db.table("trip_documents")
        .select("*")
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def delete_document(trip_id: str, doc_id: str, user_id: str) -> None:
    db = _db()
    res = (
        db.table("trip_documents")
        .select("gcs_path")
        .eq("id", doc_id)
        .eq("trip_id", trip_id)
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if res.data and res.data.get("gcs_path") and _gcs_ready():
        gcs.delete_blob(res.data["gcs_path"])

    db.table("trip_documents").delete().eq("id", doc_id).eq("trip_id", trip_id).eq("user_id", user_id).execute()
