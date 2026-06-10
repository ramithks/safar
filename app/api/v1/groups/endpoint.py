import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.limiter import limiter
from app.deps.auth import require_user
from .models import (
    CreateGroupRequest,
    JoinGroupRequest,
    SendMessageRequest,
    VotePollRequest,
    AddExpenseRequest,
    AiAssistRequest,
)
from . import service

router = APIRouter(prefix="/groups", tags=["groups"])

# NOTE: routes here are plain `def`, not `async def` — the supabase client is
# synchronous, so FastAPI runs these in its threadpool instead of blocking the
# event loop. Only ai_assist stays async (it awaits Vertex).
# PermissionError from service._require_member is mapped to 403 in main.py.


# ── Groups ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_group(body: CreateGroupRequest, user=Depends(require_user)):
    try:
        return service.create_group(
            user_id=user.id,
            name=body.name,
            destination=body.destination,
            trip_id=body.trip_id,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/by-trip/{trip_id}")
def get_group_by_trip(trip_id: str, user=Depends(require_user)):
    group = service.get_group_by_trip_id(trip_id, user.id)
    if not group:
        raise HTTPException(status_code=404, detail="No group found for this trip")
    return group


@router.get("/{group_id}")
def get_group(group_id: UUID, user=Depends(require_user)):
    group = service.get_group(str(group_id), user.id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.post("/join/{invite_code}")
def join_group(invite_code: str, body: JoinGroupRequest, user=Depends(require_user)):
    try:
        return service.join_group(invite_code, user.id, body.display_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Messages ──────────────────────────────────────────────────────────────────

@router.get("/{group_id}/messages")
def list_messages(group_id: UUID, limit: int = 50, offset: int = 0, user=Depends(require_user)):
    return {"messages": service.list_messages(str(group_id), user.id, limit, offset)}


@router.post("/{group_id}/messages", status_code=201)
def send_message(group_id: UUID, body: SendMessageRequest, user=Depends(require_user)):
    try:
        return service.send_message(
            group_id=str(group_id),
            user_id=user.id,
            sender_name=body.sender_name,
            text=body.text,
            msg_type=body.type,
            poll_data=body.poll_data,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{group_id}/messages/{message_id}/vote")
def vote_poll(group_id: UUID, message_id: UUID, body: VotePollRequest, user=Depends(require_user)):
    try:
        return service.vote_poll(str(group_id), str(message_id), body.option_id, user.id, body.voter_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Expenses ──────────────────────────────────────────────────────────────────

@router.get("/{group_id}/expenses")
def list_expenses(group_id: UUID, user=Depends(require_user)):
    return service.list_expenses(str(group_id), user.id)


@router.post("/{group_id}/expenses", status_code=201)
def add_expense(group_id: UUID, body: AddExpenseRequest, user=Depends(require_user)):
    try:
        return service.add_expense(
            group_id=str(group_id),
            user_id=user.id,
            title=body.title,
            amount=body.amount,
            paid_by=body.paid_by,
            split_between=body.split_between,
            category=body.category,
            date=body.date,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{group_id}/expenses/{expense_id}", status_code=204)
def delete_expense(group_id: UUID, expense_id: UUID, user=Depends(require_user)):
    service.delete_expense(str(group_id), str(expense_id), user.id)


# ── AI ────────────────────────────────────────────────────────────────────────

@router.post("/{group_id}/ai")
@limiter.limit("30/hour")  # Vertex call — cap per-user spend
async def ai_assist(request: Request, group_id: UUID, body: AiAssistRequest, user=Depends(require_user)):
    group = await asyncio.to_thread(service.get_group, str(group_id), user.id)
    destination = group.get("destination") if group else None
    answer = await service.ai_assist(str(group_id), user.id, body.query, destination)
    return {"answer": answer}
