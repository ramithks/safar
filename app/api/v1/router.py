from fastapi import APIRouter
from app.api.v1.plan_trip.endpoint import router as plan_trip_router
from app.api.v1.groups.endpoint import router as groups_router
from app.api.v1.trips.endpoint import router as trips_router
from app.api.v1.search.endpoint import router as search_router
from app.api.v1.autocomplete.endpoint import router as autocomplete_router
from app.api.v1.onboarding.endpoint import router as onboarding_router
from app.api.v1.discover.endpoint import router as discover_router
from app.api.v1.users.endpoint import router as users_router
from app.api.v1.reels.endpoint import router as reels_router
from app.api.v1.reels.analyze import router as reels_analyze_router
from app.api.v1.reels.jobs import router as reels_jobs_router
from app.api.v1.images import router as images_router
from app.api.v1.health import router as health_router
from app.api.v1.metrics import router as metrics_router

router = APIRouter(prefix="/api/v1")

router.include_router(health_router, tags=["health"])
router.include_router(metrics_router)
router.include_router(plan_trip_router, tags=["ai"])
router.include_router(groups_router)
router.include_router(trips_router)
router.include_router(search_router)
router.include_router(autocomplete_router)
router.include_router(onboarding_router)
router.include_router(discover_router)
router.include_router(users_router)
router.include_router(reels_router)
router.include_router(reels_analyze_router, tags=["reels"])
router.include_router(reels_jobs_router, tags=["reels"])
router.include_router(images_router, tags=["media"])
