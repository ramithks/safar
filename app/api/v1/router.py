from fastapi import APIRouter
from app.api.v1 import analyze, jobs, health
from app.api.v1 import cities_feed, trips_feed, city, saved_plans, save_trip, plan_trip, plan_day_out
from app.api.v1 import bulk_explore, community

router = APIRouter(prefix="/api/v1")

# ── Reel analysis (existing) ──────────────────────────────────────────────────
router.include_router(analyze.router, tags=["reels"])
router.include_router(jobs.router, tags=["jobs"])
router.include_router(health.router, tags=["health"])

# ── TravBuddy app routes ──────────────────────────────────────────────────────
router.include_router(cities_feed.router, tags=["feeds"])
router.include_router(trips_feed.router, tags=["feeds"])
router.include_router(city.router, tags=["city"])
router.include_router(saved_plans.router, tags=["plans"])
router.include_router(save_trip.router, tags=["plans"])
router.include_router(plan_trip.router, tags=["ai"])
router.include_router(plan_day_out.router, tags=["ai"])
router.include_router(bulk_explore.router, tags=["explore"])
router.include_router(community.router, tags=["community"])
