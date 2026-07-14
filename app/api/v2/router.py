"""
API v2 — the planner surface the website runs on.

/api/v2/plan/* is the SAME persistent-trip workspace as /api/v1/trip-state/*
(one shared router, two mounts): create → stream generation → copilot edits.
It is versioned v2 because /api/v1/plan — the classic one-shot planner — is
still what the mobile app calls; that contract must not move.
"""
from fastapi import APIRouter

from app.api.v1.trip_state.endpoint import router as trip_state_router

router = APIRouter(prefix="/api/v2")

router.include_router(trip_state_router, prefix="/plan", tags=["plan-v2"])
