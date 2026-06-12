import uuid
import urllib.parse
import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.deps.auth import require_user
import app.storage.gcs as gcs
from .models import (
    InitReelUploadRequest,
    ConfirmReelRequest,
    AttachSavedReelRequest,
    CreateBookingRequest,
    UpdateBookingRequest,
    InitBookingFileRequest,
    InitDocumentUploadRequest,
    ConfirmDocumentRequest,
    CreateDocumentDirectRequest,
)
from . import service

router = APIRouter(prefix="/trips/{trip_id}", tags=["trips"])


# ── Reels ─────────────────────────────────────────────────────────────────────

@router.post("/reels/init", status_code=201)
def init_reel_upload(trip_id: str, body: InitReelUploadRequest, user=Depends(require_user)):
    """Step 1: Create record + get signed GCS PUT URL for a trip video reel."""
    return service.init_reel_upload(
        trip_id=trip_id,
        user_id=user.id,
        title=body.title,
        category=body.category,
        filename=body.filename,
        content_type=body.content_type,
    )


@router.post("/reels/{reel_id}/confirm")
def confirm_reel_upload(trip_id: str, reel_id: str, body: ConfirmReelRequest, user=Depends(require_user)):
    """Step 2: After client PUT to GCS, mark reel as uploaded and make it public."""
    try:
        return service.confirm_reel_upload(trip_id, reel_id, user.id, body.duration)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/reels")
def list_reels(trip_id: str, user=Depends(require_user)):
    return {"reels": service.list_reels(trip_id, user.id)}


@router.delete("/reels/{reel_id}", status_code=204)
def delete_reel(trip_id: str, reel_id: str, user=Depends(require_user)):
    service.delete_reel(trip_id, reel_id, user.id)


# ── Saved-reel attachments (library reels linked to this trip) ────────────────

@router.get("/saved-reels")
def list_attached_saved_reels(trip_id: str, user=Depends(require_user)):
    return {"reels": service.list_attached_saved_reels(trip_id, user.id)}


@router.post("/saved-reels", status_code=201)
def attach_saved_reel(trip_id: str, body: AttachSavedReelRequest, user=Depends(require_user)):
    try:
        return service.attach_saved_reel(trip_id, user.id, body.reel_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/saved-reels/{reel_id}", status_code=204)
def detach_saved_reel(trip_id: str, reel_id: str, user=Depends(require_user)):
    service.detach_saved_reel(trip_id, user.id, reel_id)


# ── Bookings ──────────────────────────────────────────────────────────────────

@router.get("/bookings")
def list_bookings(trip_id: str, user=Depends(require_user)):
    return {"bookings": service.list_bookings(trip_id, user.id)}


@router.post("/bookings", status_code=201)
def create_booking(trip_id: str, body: CreateBookingRequest, user=Depends(require_user)):
    return service.create_booking(
        trip_id=trip_id,
        user_id=user.id,
        type=body.type,
        category=body.category,
        title=body.title,
        description=body.description,
        details=body.details,
        status=body.status,
        price=body.price,
        price_unit=body.price_unit,
        attachment_url=body.attachment_url,
        attachment_name=body.attachment_name,
    )


@router.patch("/bookings/{booking_id}")
def update_booking(trip_id: str, booking_id: str, body: UpdateBookingRequest, user=Depends(require_user)):
    try:
        return service.update_booking(trip_id, booking_id, user.id, body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/bookings/{booking_id}", status_code=204)
def delete_booking(trip_id: str, booking_id: str, user=Depends(require_user)):
    service.delete_booking(trip_id, booking_id, user.id)


@router.post("/bookings/{booking_id}/upload-url")
def init_booking_file_upload(trip_id: str, booking_id: str, body: InitBookingFileRequest, user=Depends(require_user)):
    """Get a signed URL to attach a confirmation file (PDF / screenshot) to a booking."""
    return service.init_booking_file_upload(trip_id, booking_id, user.id, body.filename, body.content_type)


@router.post("/bookings/{booking_id}/confirm-file")
def confirm_booking_file(trip_id: str, booking_id: str, user=Depends(require_user)):
    """After uploading the file to GCS, call this to make it public and update the booking record."""
    try:
        return service.confirm_booking_file(trip_id, booking_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── File upload (Supabase Storage) ───────────────────────────────────────────

def _sanitize_filename(filename: str) -> str:
    # Decode percent-encoded spaces and characters
    decoded = urllib.parse.unquote(filename)
    # Replace spaces with underscores
    with_underscores = decoded.replace(" ", "_")
    # Keep only alphanumeric, dots, underscores, and hyphens
    sanitized = re.sub(r"[^\w\.\-]", "", with_underscores)
    return sanitized or "upload"


@router.post("/files/upload", status_code=200)
async def upload_trip_file(
    trip_id: str,
    file: UploadFile = File(...),
    user=Depends(require_user),
):
    """
    Upload any file (image or PDF) to Google Cloud Storage.
    Returns { url, path, filename } — store the `url` and pass it when
    creating a booking or document.
    """
    content = await file.read()
    content_type = file.content_type or "application/octet-stream"
    safe_name = _sanitize_filename(file.filename or "upload")
    path = f"trips/{trip_id}/{user.id}/{uuid.uuid4()}_{safe_name}"

    try:
        url = gcs.upload_bytes(path, content, content_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GCS upload failed: {e}")

    return {"url": url, "path": path, "filename": safe_name}


# ── Documents ─────────────────────────────────────────────────────────────────

@router.post("/documents/create", status_code=201)
def create_document_direct(trip_id: str, body: CreateDocumentDirectRequest, user=Depends(require_user)):
    """
    Create a document record using a file URL already uploaded via /files/upload.
    Simpler alternative to the GCS init/confirm flow.
    """
    return service.create_document_direct(
        trip_id=trip_id,
        user_id=user.id,
        name=body.name,
        category=body.category,
        filename=body.filename,
        file_url=body.file_url,
    )


@router.post("/documents/init", status_code=201)
def init_document_upload(trip_id: str, body: InitDocumentUploadRequest, user=Depends(require_user)):
    """Step 1: Create record + get signed GCS PUT URL for a travel document."""
    return service.init_document_upload(
        trip_id=trip_id,
        user_id=user.id,
        name=body.name,
        category=body.category,
        filename=body.filename,
        content_type=body.content_type,
    )


@router.post("/documents/{doc_id}/confirm")
def confirm_document_upload(trip_id: str, doc_id: str, body: ConfirmDocumentRequest, user=Depends(require_user)):
    """Step 2: After client PUT to GCS, mark document as uploaded and make it public."""
    try:
        return service.confirm_document_upload(trip_id, doc_id, user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/documents")
def list_documents(trip_id: str, user=Depends(require_user)):
    return {"documents": service.list_documents(trip_id, user.id)}


@router.delete("/documents/{doc_id}", status_code=204)
def delete_document(trip_id: str, doc_id: str, user=Depends(require_user)):
    """Delete document record and remove file from GCS."""
    service.delete_document(trip_id, doc_id, user.id)
