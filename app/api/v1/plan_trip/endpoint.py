from fastapi import APIRouter, Depends, Request

from app.core.limiter import limiter
from app.deps.auth import require_user

from .models import PlanRequest
from app.agents.supervisor import supervisor_run

router = APIRouter()


@router.post("/plan")
@limiter.limit("10/hour")
async def plan(request: Request, body: PlanRequest, user=Depends(require_user)):
    """
    Unified planner — transport + activity itinerary + hotels in one call.
    Runs journey + itinerary agents in parallel via SupervisorAgent,
    which applies budget intervention and constraint validation before returning.
    Rate limited to 10 requests per hour per IP.
    """
    return await supervisor_run(body, user.id)
