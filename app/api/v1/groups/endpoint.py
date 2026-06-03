from fastapi import APIRouter, Depends, HTTPException

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


# ── Groups ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_group(body: CreateGroupRequest, user=Depends(require_user)):
    return service.create_group(
        user_id=user.id,
        name=body.name,
        destination=body.destination,
        trip_id=body.trip_id,
        display_name=body.display_name,
    )


@router.get("/{group_id}")
async def get_group(group_id: str, user=Depends(require_user)):
    group = service.get_group(group_id, user.id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.post("/join/{invite_code}")
async def join_group(invite_code: str, body: JoinGroupRequest, user=Depends(require_user)):
    try:
        return service.join_group(invite_code, user.id, body.display_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Messages ──────────────────────────────────────────────────────────────────

@router.get("/{group_id}/messages")
async def list_messages(group_id: str, limit: int = 50, offset: int = 0, user=Depends(require_user)):
    return {"messages": service.list_messages(group_id, limit, offset)}


@router.post("/{group_id}/messages", status_code=201)
async def send_message(group_id: str, body: SendMessageRequest, user=Depends(require_user)):
    return service.send_message(
        group_id=group_id,
        user_id=user.id,
        sender_name=body.sender_name,
        text=body.text,
        msg_type=body.type,
        poll_data=body.poll_data,
    )


@router.post("/{group_id}/messages/{message_id}/vote")
async def vote_poll(group_id: str, message_id: str, body: VotePollRequest, user=Depends(require_user)):
    try:
        return service.vote_poll(group_id, message_id, body.option_id, user.id, body.voter_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Expenses ──────────────────────────────────────────────────────────────────

@router.get("/{group_id}/expenses")
async def list_expenses(group_id: str, user=Depends(require_user)):
    return service.list_expenses(group_id)


@router.post("/{group_id}/expenses", status_code=201)
async def add_expense(group_id: str, body: AddExpenseRequest, user=Depends(require_user)):
    return service.add_expense(
        group_id=group_id,
        user_id=user.id,
        title=body.title,
        amount=body.amount,
        paid_by=body.paid_by,
        split_between=body.split_between,
        category=body.category,
        date=body.date,
    )


@router.delete("/{group_id}/expenses/{expense_id}", status_code=204)
async def delete_expense(group_id: str, expense_id: str, user=Depends(require_user)):
    service.delete_expense(group_id, expense_id, user.id)


# ── AI ────────────────────────────────────────────────────────────────────────

@router.post("/{group_id}/ai")
async def ai_assist(group_id: str, body: AiAssistRequest, user=Depends(require_user)):
    group = service.get_group(group_id, user.id)
    destination = group.get("destination") if group else None
    answer = await service.ai_assist(group_id, body.query, destination)
    return {"answer": answer}
