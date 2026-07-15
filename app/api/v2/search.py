"""
API v2 — search-store surface: the progressive-loading + personalization
endpoints the workspace frontend reads from.

  GET  /trips/{id}/options          — ranked, view-filtered, memory-personalized shortlist
  GET  /results/{id}                — full raw payload (booking/detail; lazy)
  POST /results/{id}/view           — hide / favourite / pin / annotate one result
  GET  /trips/{id}/recommendations  — precomputed reco scores
  GET  /trips/{id}/memory           — active trip rules
  PUT  /trips/{id}/memory           — set/replace a durable rule

All reads are served from the search store, never a fat trip_states.state blob.
Writes go through the service role after an ownership check.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.client import get_db, run_db
from app.deps.auth import require_user
from app.trip_state import store
from app.trip_state.retrieval import RetrievalService
from app.trip_state.memory import MemoryService, get_rules
from app.trip_state.views import ViewLayer
from app.reco.index import RecommendationIndex

logger = logging.getLogger(__name__)
router = APIRouter()


# ── auth helpers (mirror trip_state.endpoint) ────────────────────────────────
async def _trip_row(trip_id: str) -> dict:
    row = await run_db(store.get_trip_state, trip_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trip not found")
    return row


def _authorize_read(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id and row["visibility"] not in ("link", "public"):
        raise HTTPException(status_code=404, detail="Trip not found")


def _authorize_write(row: dict, user_id: str) -> None:
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not your trip")


def _result_trip_id(result_id: str) -> str | None:
    res = (get_db().table("search_results").select("trip_id")
           .eq("id", result_id).limit(1).execute().data)
    return res[0]["trip_id"] if res else None


# ── options: the personalized shortlist ──────────────────────────────────────
@router.get("/trips/{trip_id}/options")
async def get_options(trip_id: str, user=Depends(require_user),
                      scope: str = "", kind: str = "transport",
                      mode: str | None = None, limit: int = 6):
    row = await _trip_row(trip_id)
    _authorize_read(row, str(user.id))
    rules = await get_rules(trip_id, scope=scope, user_id=str(user.id))
    options = await run_db(lambda: RetrievalService.resolve_options(
        trip_id, scope_id=scope, kind=kind, mode=mode, rules=rules, limit=limit))
    return {"scope": scope, "kind": kind, "mode": mode,
            "applied_rules": rules.get("described") or [], "options": options}


# ── raw hydration (lazy; booking/detail) ─────────────────────────────────────
@router.get("/results/{result_id}")
async def get_result_raw(result_id: str, user=Depends(require_user)):
    trip_id = await run_db(_result_trip_id, result_id)
    if not trip_id:
        raise HTTPException(status_code=404, detail="Result not found")
    _authorize_read(await _trip_row(trip_id), str(user.id))
    raws = await run_db(RetrievalService.hydrate_raw, [result_id])
    if result_id not in raws:
        raise HTTPException(status_code=404, detail="Result not found")
    return {"id": result_id, "raw": raws[result_id]}


# ── view layer: hide / favourite / pin / annotate ────────────────────────────
class ViewBody(BaseModel):
    visible: bool | None = None
    favorite: bool | None = None
    selected: bool | None = None
    score_override: float | None = None
    notes: str | None = None


@router.post("/results/{result_id}/view")
async def set_result_view(result_id: str, body: ViewBody, user=Depends(require_user)):
    trip_id = await run_db(_result_trip_id, result_id)
    if not trip_id:
        raise HTTPException(status_code=404, detail="Result not found")
    _authorize_write(await _trip_row(trip_id), str(user.id))
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="No view fields to set")
    await run_db(lambda: ViewLayer.set_view(trip_id, result_id, source="user", **fields))
    return {"ok": True, "result_id": result_id, "view": fields}


# ── recommendations ──────────────────────────────────────────────────────────
@router.get("/trips/{trip_id}/recommendations")
async def get_recommendations(trip_id: str, user=Depends(require_user), scope: str | None = None):
    row = await _trip_row(trip_id)
    _authorize_read(row, str(user.id))
    scores = await run_db(lambda: RecommendationIndex.load_scores(trip_id, scope))
    return {"scope": scope, "scores": scores}


# ── trip memory ──────────────────────────────────────────────────────────────
class MemoryBody(BaseModel):
    key: str
    value: object
    scope: str = ""
    confidence: float = 0.8


@router.get("/trips/{trip_id}/memory")
async def get_memory(trip_id: str, user=Depends(require_user)):
    row = await _trip_row(trip_id)
    _authorize_read(row, str(user.id))
    rows = await run_db(MemoryService.get_trip_rows, trip_id)
    return {"rules": rows, "described": MemoryService.describe(rows)}


@router.put("/trips/{trip_id}/memory")
async def put_memory(trip_id: str, body: MemoryBody, user=Depends(require_user)):
    row = await _trip_row(trip_id)
    _authorize_write(row, str(user.id))
    await run_db(lambda: MemoryService.upsert(
        trip_id, body.key, body.value, scope=body.scope,
        confidence=body.confidence, source="explicit"))
    return {"ok": True, "key": body.key}
