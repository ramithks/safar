from fastapi import APIRouter, Depends

from app.deps.auth import require_user

from .models import PlanRequest
from .plan import run_plan

router = APIRouter()


@router.post("/plan")
async def plan(body: PlanRequest, user=Depends(require_user)):
    """
    Unified planner — transport + activity itinerary + hotels in one call.
    Runs plan-journey and plan-trip in parallel and merges the results.
    """
    return await run_plan(body, user.id)
