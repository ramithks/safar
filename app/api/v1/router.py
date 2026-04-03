from fastapi import APIRouter
from app.api.v1 import analyze, jobs, health

router = APIRouter(prefix="/api/v1")
router.include_router(analyze.router, tags=["reels"])
router.include_router(jobs.router, tags=["jobs"])
router.include_router(health.router, tags=["health"])
