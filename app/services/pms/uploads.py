"""
What a file actually is, as opposed to what it says it is.

The uploader validated `UploadFile.content_type` — a header the client writes —
and then used it twice: to pick the stored extension, and as the `Content-Type`
on a publicly readable object in a bucket the marketplace also serves from. So
"this is a PNG" was a claim by whoever was uploading, believed without being
checked, and written back out as if we had verified it.

Nothing dramatic follows from that today: GCS serves what it is told, browsers
do not execute an `image/png`, and the route is behind a hotelier's session with
the `rates` permission. But "the attacker would have to be a hotel we onboarded"
is a control that expires the moment we onboard a hotel, and §32 of the brief is
explicit: do not trust the filename or the client MIME type.

**So the bytes decide.** Every format here has a signature in its first few
bytes, and the sniff is the only thing that picks the extension and the stored
content type. A file whose header disagrees with its claim is refused rather
than corrected — a client that lies about one field is not a client whose other
fields are worth guessing at.

The dimension read is the second half. A 40,000 × 40,000 PNG is a few hundred
kilobytes on the wire and a memory exhaustion for anything that later decodes
it, and it passes a size check comfortably. Read from the header rather than by
decoding, because decoding it is the thing being avoided.
"""
import struct
from dataclasses import dataclass

# The four formats a hotel may upload, by what their bytes actually begin with.
# Deliberately the same four the route already allowed — this is a check that
# was missing, not a policy change.
JPEG = "image/jpeg"
PNG = "image/png"
WEBP = "image/webp"
AVIF = "image/avif"
PDF = "application/pdf"

EXTENSIONS = {JPEG: "jpg", PNG: "png", WEBP: "webp", AVIF: "avif", PDF: "pdf"}

# Nothing sensible is smaller than this, and a shorter file cannot carry a
# header to read.
_MIN_BYTES = 32

# A photograph from a phone is around 4000 × 3000. Ten thousand a side is four
# times that in each direction and is not a photograph of a room.
MAX_DIMENSION = 12000


@dataclass(frozen=True)
class Sniffed:
    mime: str
    extension: str
    width: int | None
    height: int | None


class RejectedUpload(Exception):
    """
    Not this file. Carries the sentence a hotelier should read.

    `reason` is for the log and names what was actually wrong; `message` is what
    the route returns, and never says "your magic bytes did not match" to
    somebody trying to upload a photograph of a bedroom.
    """

    def __init__(self, reason: str, message: str, status: int = 415):
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.status = status


def _png_size(data: bytes) -> tuple[int, int] | None:
    # IHDR is always the first chunk, and width/height are its first eight bytes.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """
    Walk the segment markers to SOFn, which is the only one carrying dimensions.

    A JPEG is a chain of length-prefixed segments and the frame header can sit
    behind any amount of EXIF, so this is a walk rather than a fixed offset.
    Bounded by the data we have; a truncated file simply yields no answer, which
    the caller treats as "unknown" rather than as a failure.
    """
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0–SOF15, minus the four that are not frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if length < 2:
            return None
        i += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    # Only the simple lossy/lossless headers; an extended (VP8X) file is rare
    # from a phone and returning None for it is honest.
    if len(data) < 30:
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if fourcc == b"VP8L" and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    return None


def _detect(data: bytes) -> str | None:
    """What these bytes are. None means nothing we accept."""
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return PNG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    # AVIF is an ISO-BMFF file: a `ftyp` box whose brand says avif. Checked in
    # the compatible-brands list too, because encoders differ about which one
    # they put in the major slot.
    if data[4:8] == b"ftyp" and (b"avif" in data[8:32] or b"avis" in data[8:32]):
        return AVIF
    if data[:5] == b"%PDF-":
        return PDF
    return None


def _dimensions(mime: str, data: bytes) -> tuple[int | None, int | None]:
    reader = {PNG: _png_size, JPEG: _jpeg_size, WEBP: _webp_size}.get(mime)
    if reader is None:
        return None, None
    try:
        found = reader(data)
    except (struct.error, IndexError, ValueError):
        return None, None
    return found if found else (None, None)


def sniff(
    data: bytes,
    *,
    allowed: set[str],
    claimed: str | None = None,
    max_bytes: int,
) -> Sniffed:
    """
    Decide what this file is, or refuse it.

    `claimed` is the client's header and is used for **one** thing: noticing
    that it disagrees with the bytes, which is worth a distinct log line. It
    never picks the extension and never becomes the stored content type.
    """
    if not data:
        raise RejectedUpload("empty upload", "No file received.", 400)
    if len(data) < _MIN_BYTES:
        raise RejectedUpload(
            f"{len(data)} bytes is too short to be a file",
            "That file looks empty or incomplete.", 400,
        )
    if len(data) > max_bytes:
        raise RejectedUpload(
            f"{len(data)} bytes exceeds {max_bytes}",
            f"That file is over {max_bytes // (1024 * 1024)} MB. "
            "Please upload a smaller one.", 413,
        )

    mime = _detect(data)
    if mime is None or mime not in allowed:
        raise RejectedUpload(
            f"content sniffed as {mime or 'unrecognised'}, claimed {claimed!r}",
            _refusal(allowed),
        )

    claimed_norm = (claimed or "").split(";")[0].strip().lower()
    if claimed_norm and claimed_norm != mime:
        # Refused rather than silently corrected. Usually a browser guessing
        # from an extension, occasionally not; either way a file whose header
        # contradicts its declaration is not one to store and serve.
        raise RejectedUpload(
            f"declared {claimed_norm!r} but the bytes are {mime}",
            "That file's type doesn't match its contents. "
            "Please re-save it and try again.",
        )

    width, height = _dimensions(mime, data)
    if width and height and max(width, height) > MAX_DIMENSION:
        raise RejectedUpload(
            f"{width}x{height} exceeds {MAX_DIMENSION}px",
            f"That image is {width}×{height}, which is larger than we can "
            "process. Please resize it and try again.",
        )

    return Sniffed(mime=mime, extension=EXTENSIONS[mime], width=width, height=height)


def _refusal(allowed: set[str]) -> str:
    names = {JPEG: "JPG", PNG: "PNG", WEBP: "WebP", AVIF: "AVIF", PDF: "PDF"}
    listed = [names[m] for m in (JPEG, PNG, WEBP, AVIF, PDF) if m in allowed]
    if len(listed) > 1:
        listed = [", ".join(listed[:-1]) + " or " + listed[-1]]
    return f"Please upload a {listed[0]} file."
