import logging
import time

import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.limiter import limiter
from app.deps.auth import require_user

from .models import PlanRequest
from app.agents.supervisor import supervisor_run

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/plan")
@limiter.limit("10/hour")
async def plan(request: Request, body: PlanRequest, user=Depends(require_user)):
    """
    Unified planner — transport + activity itinerary + hotels in one call.
    Runs journey + itinerary agents in parallel via SupervisorAgent,
    which applies budget intervention and constraint validation before returning.
    Rate limited to 10 requests per hour per user (per IP when unauthenticated).
    """
    uid_short = str(user.id)[:8]
    logger.info(
        "[plan] START user=%s %s → %s | days=%d budget=%s travelers=%d",
        uid_short, body.origin, body.destination,
        body.days, body.budget, body.travelers,
    )
    t0 = time.perf_counter()

    try:
        result = await supervisor_run(body, user.id)

        elapsed = time.perf_counter() - t0
        logger.info(
            "[plan] DONE user=%s %s → %s | trip_id=%s elapsed=%.1fs",
            uid_short, body.origin, body.destination,
            result.get("trip_id", "n/a"), elapsed,
        )
        return result

    except Exception as exc:
        elapsed = time.perf_counter() - t0
        logger.error(
            "[plan] FAILED user=%s %s → %s | elapsed=%.1fs error=%s",
            uid_short, body.origin, body.destination, elapsed, exc,
            exc_info=True,
        )
        sentry_sdk.capture_exception(exc)
        raise HTTPException(status_code=500, detail="Trip planning failed. Please try again.") from exc
