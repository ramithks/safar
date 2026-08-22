"""
An upload is what its bytes say, not what its header claims.

The route validated `UploadFile.content_type` — a string the client writes — and
then used it to pick both the stored extension and the `Content-Type` on a
publicly readable object. §32: do not trust the filename or the client MIME type.

Real headers throughout. A test that asserts a sniffer works using bytes the
sniffer's author invented proves the author is consistent, not that the sniffer
is right.
"""
import struct

import pytest

from app.services.pms import uploads

PAD = b"\x00" * 64


def png(width: int = 800, height: int = 600) -> bytes:
    """A real PNG header: signature, then the IHDR chunk carrying the size."""
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00" + PAD)


def jpeg(width: int = 4032, height: int = 3024) -> bytes:
    """SOI, an EXIF segment to walk past, then the SOF0 frame header."""
    exif = b"\xff\xe1" + struct.pack(">H", 18) + b"Exif\x00\x00" + b"\x00" * 10
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9)
    # SOI, then EXIF (which begins \xff\xe1, giving the \xff\xd8\xff
    # signature the sniffer looks for), then the frame header it must walk to.
    return b"\xff\xd8" + exif + sof + PAD


def webp(width: int = 1024, height: int = 768) -> bytes:
    body = (b"VP8 " + struct.pack("<I", 24) + b"\x00" * 3 + b"\x9d\x01\x2a"
            + struct.pack("<HH", width, height) + PAD)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


AVIF = b"\x00\x00\x00\x20ftypavifavifmif1miaf" + PAD
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + PAD

IMAGES = {uploads.JPEG, uploads.PNG, uploads.WEBP, uploads.AVIF}


def sniff(data, claimed=None, allowed=None, max_bytes=5 * 1024 * 1024):
    return uploads.sniff(data, allowed=allowed or IMAGES,
                         claimed=claimed, max_bytes=max_bytes)


# ── the formats a hotel actually uploads ────────────────────────────────────

def test_a_phone_photograph_is_read_correctly():
    found = sniff(jpeg(), claimed="image/jpeg")
    assert found.mime == uploads.JPEG
    assert found.extension == "jpg"
    assert (found.width, found.height) == (4032, 3024)


@pytest.mark.parametrize("data,mime,ext", [
    (png(), uploads.PNG, "png"),
    (webp(), uploads.WEBP, "webp"),
    (AVIF, uploads.AVIF, "avif"),
])
def test_the_other_three_formats(data, mime, ext):
    found = sniff(data, claimed=mime)
    assert (found.mime, found.extension) == (mime, ext)


def test_a_pdf_is_recognised_where_it_is_allowed():
    found = sniff(PDF, claimed="application/pdf", allowed={uploads.PDF} | IMAGES)
    assert found.extension == "pdf"


def test_a_missing_content_type_header_is_fine():
    """
    The header is used for one thing — noticing a contradiction. Its absence is
    not a contradiction, and a client that sends no type is not an attacker.
    """
    assert sniff(png(), claimed=None).mime == uploads.PNG
    assert sniff(png(), claimed="").mime == uploads.PNG


def test_a_charset_parameter_does_not_confuse_the_comparison():
    assert sniff(PDF, claimed="application/pdf; charset=binary",
                 allowed={uploads.PDF}).mime == uploads.PDF


# ── the bug ─────────────────────────────────────────────────────────────────

def test_a_script_claiming_to_be_a_png_is_refused():
    """
    The whole point. Before this, these bytes were stored as `photo.png` with
    `Content-Type: image/png` on a publicly readable object, because the client
    said so.
    """
    payload = b"<html><script>alert(document.cookie)</script></html>" + PAD
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(payload, claimed="image/png")
    assert "unrecognised" in raised.value.reason


def test_an_svg_is_still_refused_and_now_for_the_right_reason():
    """
    Already refused, by its declared type. Now refused by its contents, which is
    the version that also refuses an SVG declaring itself a PNG.
    """
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>' + PAD
    with pytest.raises(uploads.RejectedUpload):
        sniff(svg, claimed="image/svg+xml")
    with pytest.raises(uploads.RejectedUpload):
        sniff(svg, claimed="image/png")


def test_a_real_image_lying_about_which_format_it_is_is_refused_not_corrected():
    """
    Refused rather than silently relabelled. Usually this is a browser guessing
    from a filename; occasionally it is not, and a client whose header
    contradicts its bytes is not one whose other fields are worth guessing at.
    """
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(png(), claimed="image/jpeg")
    assert "declared 'image/jpeg'" in raised.value.reason
    assert "magic" not in raised.value.message.lower()


def test_a_pdf_cannot_be_uploaded_into_the_photo_gallery():
    """
    A real PDF, correctly declared, into a route that allows only images. The
    allow list is enforced against the sniffed type, not the claimed one.
    """
    with pytest.raises(uploads.RejectedUpload):
        sniff(PDF, claimed="application/pdf", allowed=IMAGES)


# ── the checks a size limit cannot make ─────────────────────────────────────

def test_a_decompression_bomb_passes_the_size_check_and_is_still_refused():
    """
    40,000 × 40,000 is a few hundred bytes of header and 6.4 GB decoded. The
    5 MB ceiling has nothing to say about it.
    """
    bomb = png(40000, 40000)
    assert len(bomb) < 5 * 1024 * 1024
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(bomb, claimed="image/png")
    assert "40000x40000" in raised.value.reason
    # And the hotelier is told something they can act on.
    assert "resize" in raised.value.message.lower()


def test_an_ordinary_large_photograph_is_not_caught_by_the_dimension_rule():
    """
    The rule must be invisible to the case it exists alongside: a 4032 × 3024
    phone photograph is the ordinary upload and 12,000 is well clear of it.
    """
    assert sniff(jpeg(4032, 3024), claimed="image/jpeg").width == 4032
    assert sniff(png(6000, 4000), claimed="image/png").height == 4000


def test_a_format_whose_dimensions_we_cannot_read_is_still_accepted():
    """
    AVIF's size lives behind a box walk this does not do. Unknown dimensions
    must mean "unchecked", not "rejected" — refusing every AVIF because we
    cannot measure it would be a worse product for no security gain, since the
    format is still verified.
    """
    found = sniff(AVIF, claimed="image/avif")
    assert found.mime == uploads.AVIF
    assert found.width is None


# ── the edges ───────────────────────────────────────────────────────────────

def test_an_empty_upload_is_a_400_and_not_a_415():
    """
    "You sent nothing" and "we don't accept that" are different problems and a
    hotelier acts differently on each.
    """
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(b"")
    assert raised.value.status == 400


def test_something_too_short_to_have_a_header_is_refused_cleanly():
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(b"\x89PNG")
    assert raised.value.status == 400


def test_the_size_ceiling_still_applies_and_says_the_number():
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(png() + b"\x00" * (6 * 1024 * 1024), claimed="image/png")
    assert raised.value.status == 413
    assert "5 MB" in raised.value.message


def test_a_truncated_jpeg_is_accepted_without_dimensions_rather_than_crashing():
    """
    A walk over segment lengths is a walk over attacker-controlled numbers. It
    must run out of data rather than off the end of it.
    """
    found = sniff(b"\xff\xd8" + b"\xff\xe1\x00\xff" + b"\x00" * 40, claimed="image/jpeg")
    assert found.mime == uploads.JPEG
    assert found.width is None


def test_a_jpeg_with_an_absurd_segment_length_does_not_loop():
    sniff(b"\xff\xd8" + b"\xff\xe1\x00\x00" + b"\x00" * 60, claimed="image/jpeg")


def test_the_refusal_names_the_formats_a_hotelier_can_use():
    with pytest.raises(uploads.RejectedUpload) as raised:
        sniff(b"not a picture at all, just some prose about a hotel room" + PAD)
    assert "JPG, PNG, WebP or AVIF" in raised.value.message
