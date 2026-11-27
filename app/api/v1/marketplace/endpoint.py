"""
Marketplace partner AI — turn a brochure, PDF or pasted itinerary into a
listing draft.

  POST /api/v1/marketplace/extract/text   {text, kind_hint?, generate_copy?}
  POST /api/v1/marketplace/extract/file   multipart: file, kind_hint?

Both are authenticated (a partner is a signed-in Safar user) and rate
limited — each call is two Gemini round-trips, so this is the most expensive
endpoint a partner can hit.

The website proxies these through its own /api/ai/import route, which is what
records the ai_import_jobs audit row. This service stays stateless.
"""
import logging
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.v1.marketplace.schemas import (
    ExtractResponse,
    ExtractTextRequest,
    MatchRequest,
    MatchResponse,
)
from app.core.limiter import limiter
from app.deps.auth import require_user
from app.services.marketplace_extract import extract_listing
from app.services.marketplace_match import DayPlan, MatchContext, match_inventory

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/marketplace", tags=["marketplace"])

# Gemini reads PDFs natively, so scans and text-layer PDFs take the same path.
ALLOWED_MIME = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES_HINT = 40


def _handle(exc: Exception) -> HTTPException:
    logger.exception("[marketplace] extraction failed: %s", type(exc).__name__)
    return HTTPException(
        502,
        "We couldn't read that right now. Please try again, or paste the details as text.",
    )


@router.post("/match", response_model=MatchResponse)
# Unauthenticated on purpose: the planner runs this for signed-out visitors
# too, and it only ever reads published inventory. The cap is generous because
# one trip generation issues one call and the read is memoised per city.
@limiter.limit("120/minute")
def match(request: Request, body: MatchRequest) -> MatchResponse:
    """
    Rank marketplace inventory against a trip intent.

    Ranking, filtering, budget and date validation and trust scoring all live
    behind this endpoint — the planner sends intent and renders whatever comes
    back. Commission, subscription tier and featured flags are deliberately
    absent from the ranking; see app/services/marketplace_match.py.

    Returning nothing is a valid, common and correct answer.
    """
    depart = None
    if body.depart_date:
        try:
            depart = date.fromisoformat(body.depart_date)
        except ValueError:
            depart = None  # a bad date degrades the match, never fails it

    ctx = MatchContext(
        destination=body.destination.strip(),
        days=body.days,
        travellers=body.travellers,
        origin=body.origin,
        budget=body.budget,
        budget_is_total=body.budget_is_total,
        interests=[i for i in body.interests if i][:20],
        travel_style=body.travel_style,
        depart_date=depart,
        day_plan=[DayPlan(day=d.day, title=d.title, places=d.places) for d in body.day_plan],
    )

    try:
        return MatchResponse(**match_inventory(ctx))
    except Exception as exc:  # noqa: BLE001
        # The planner must survive a marketplace outage untouched.
        logger.exception("[marketplace] match failed: %s", type(exc).__name__)
        return MatchResponse(meta={"error": "match_failed"})


@router.post("/extract/text", response_model=ExtractResponse)
@limiter.limit("20/hour")
async def extract_from_text(
    request: Request,
    body: ExtractTextRequest,
    user=Depends(require_user),
) -> ExtractResponse:
    """Method 2 — the partner pastes their itinerary in."""
    try:
        result = await extract_listing(
            text=body.text,
            kind_hint=body.kind_hint,
            generate_copy=body.generate_copy,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc

    return ExtractResponse(**result)


@router.post("/extract/file", response_model=ExtractResponse)
@limiter.limit("10/hour")
async def extract_from_file(
    request: Request,
    file: UploadFile = File(...),
    kind_hint: str | None = Form(default=None),
    generate_copy: bool = Form(default=True),
    user=Depends(require_user),
) -> ExtractResponse:
    """Methods 1 and 3 — a PDF, a brochure or a photo of one."""
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(415, "Upload a PDF, JPG, PNG or WebP.")

    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "That file is over 10 MB. Please upload a smaller one.")

    if kind_hint not in {"hotel", "experience", "group_trip", None, ""}:
        kind_hint = None

    try:
        result = await extract_listing(
            file_bytes=data,
            mime_type=mime,
            kind_hint=kind_hint or None,
            generate_copy=generate_copy,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc

    return ExtractResponse(**result)
