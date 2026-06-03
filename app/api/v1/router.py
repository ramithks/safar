from fastapi import APIRouter
from app.api.v1.plan_trip.endpoint import router as plan_trip_router
from app.api.v1.groups.endpoint import router as groups_router
from app.api.v1.trips.endpoint import router as trips_router

router = APIRouter(prefix="/api/v1")

router.include_router(plan_trip_router, tags=["ai"])
router.include_router(groups_router)
router.include_router(trips_router)
