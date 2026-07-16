"""
API v3 — the TripDoc CRUD surface (docs/tripstate_removal_plan.md §4).

One JSON document per trip. Generate → save JSON → return JSON. Selections
are a single UPDATE and never invoke planning logic. No SSE — clients poll
GET while status == "planning".
"""
from fastapi import APIRouter

from app.api.v3.trips import router as trips_router

router = APIRouter(prefix="/api/v3")

router.include_router(trips_router, prefix="/trips", tags=["trips-v3"])
