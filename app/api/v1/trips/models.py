from pydantic import BaseModel
from typing import Optional


# ── Reels ──────────────────────────────────────────────────────────────────────

class InitReelUploadRequest(BaseModel):
    title: str
    category: str = "Memory"
    filename: str                       # e.g. "clip_001.mp4"
    content_type: str = "video/mp4"     # MIME type for signed URL


class ConfirmReelRequest(BaseModel):
    duration: Optional[str] = None      # e.g. "0:45"


class AttachSavedReelRequest(BaseModel):
    reel_id: str                        # saved_reels.id to attach to this trip


# ── Bookings ───────────────────────────────────────────────────────────────────

class CreateBookingRequest(BaseModel):
    type: str                           # 'flight' | 'train' | 'bus' | 'hotel' | 'cab' | 'activity'
    category: str                       # 'Flight' | 'Transport' | 'Stay' | 'Activity'
    title: str
    description: Optional[str] = None
    details: Optional[str] = None
    status: str = "Planned"             # 'Confirmed' | 'Pending' | 'Planned'
    price: Optional[str] = None
    price_unit: Optional[str] = None
    attachment_url: Optional[str] = None   # Supabase Storage public URL of the attached file
    attachment_name: Optional[str] = None  # original filename for display


class UpdateBookingRequest(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    details: Optional[str] = None
    price: Optional[str] = None


class InitBookingFileRequest(BaseModel):
    filename: str                       # e.g. "booking_confirmation.pdf"
    content_type: str = "application/pdf"


# ── Documents ──────────────────────────────────────────────────────────────────

class InitDocumentUploadRequest(BaseModel):
    name: str                           # Display name, e.g. "Passport Front & Back"
    category: str                       # 'Passport' | 'Aadhar Card' | 'Visa' | 'Driving License' | 'Other'
    filename: str                       # e.g. "passport_scan.pdf"
    content_type: str = "application/pdf"


class ConfirmDocumentRequest(BaseModel):
    pass  # confirmation has no extra body; doc_id from path is sufficient


# ── Direct (Supabase Storage) create ──────────────────────────────────────────

class CreateDocumentDirectRequest(BaseModel):
    name: str        # Display name, e.g. "My Passport"
    category: str    # 'Passport' | 'Aadhar Card' | 'Visa' | 'Driving License' | 'Other'
    filename: str    # original filename for display
    file_url: str    # Supabase Storage public URL (returned by /files/upload)
