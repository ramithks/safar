"""
Referral + travel wallet + community showcase API (growth OS Phase 2).

  GET  /api/v1/referral/me        — my code, member #, wallet, referral stats
  POST /api/v1/referral/redeem    — apply a friend's code (once)
  GET  /api/v1/community/live     — public group trips (NO auth — showcase)
  GET  /api/v1/community/recent   — recent joiners strip (NO auth — showcase)

Routes are plain `def` (supabase client is sync — FastAPI threadpool), same
convention as groups/endpoint.py.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.limiter import limiter
from app.deps.auth import require_user

from . import service

router = APIRouter(tags=["referral"])


class RedeemRequest(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    display_name: str | None = Field(default=None, max_length=80)


@router.get("/referral/me")
def my_referral(user=Depends(require_user), display_name: str | None = None):
    return service.me(str(user.id), display_name)


@router.post("/referral/redeem")
@limiter.limit("10/hour")
def redeem_code(request: Request, body: RedeemRequest, user=Depends(require_user)):
    try:
        return service.redeem(str(user.id), body.code, body.display_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/community/live")
@limiter.limit("60/minute")
def community_live(request: Request, limit: int = 8):
    """Public — powers the community/homepage group-trip rails."""
    return {"groups": service.live_groups(min(max(limit, 1), 20))}


@router.get("/community/recent")
@limiter.limit("60/minute")
def community_recent(request: Request, limit: int = 12):
    """Public — the 'recent travelers' social-proof strip."""
    return {"travelers": service.recent_travelers(min(max(limit, 1), 30))}
