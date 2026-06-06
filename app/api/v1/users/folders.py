"""
GET    /api/v1/users/folders                         →  list user's folders (with item count)
POST   /api/v1/users/folders                         →  create a folder
DELETE /api/v1/users/folders/{folder_id}             →  delete folder (items cascade)
GET    /api/v1/users/folders/{folder_id}/items       →  list items in a folder
POST   /api/v1/users/folders/{folder_id}/items       →  add item to folder
DELETE /api/v1/users/folders/{folder_id}/items/{item_id}  →  remove item from folder
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users/folders", tags=["folders"])


class CreateFolderRequest(BaseModel):
    name: str
    emoji: str = "📁"
    item_type: str = "mixed"


class AddItemRequest(BaseModel):
    item_id: str
    item_type: str
    item_snapshot: dict = {}


@router.get("")
async def list_folders(user=Depends(require_user)):
    try:
        db = get_db()
        folders_res = (
            db.table("saved_folders")
            .select("*")
            .eq("user_id", str(user.id))
            .order("created_at", desc=True)
            .execute()
        )
        folders = folders_res.data or []
        if not folders:
            return []

        # Attach item counts
        folder_ids = [f["id"] for f in folders]
        counts_res = (
            db.table("folder_items")
            .select("folder_id")
            .in_("folder_id", folder_ids)
            .execute()
        )
        count_map: dict[str, int] = {}
        for row in (counts_res.data or []):
            fid = row["folder_id"]
            count_map[fid] = count_map.get(fid, 0) + 1

        for f in folders:
            f["item_count"] = count_map.get(f["id"], 0)

        return folders
    except Exception as exc:
        logger.warning("[folders] list error: %s", exc)
        return []


@router.post("", status_code=201)
async def create_folder(body: CreateFolderRequest, user=Depends(require_user)):
    allowed_types = {"trips", "nights", "reels", "mixed"}
    if body.item_type not in allowed_types:
        raise HTTPException(status_code=422, detail=f"item_type must be one of {allowed_types}")
    db = get_db()
    try:
        res = (
            db.table("saved_folders")
            .insert({
                "user_id": str(user.id),
                "name": body.name.strip(),
                "emoji": body.emoji,
                "item_type": body.item_type,
            })
            .select()
            .single()
            .execute()
        )
        folder = res.data
        folder["item_count"] = 0
        return folder
    except Exception as exc:
        logger.error("[folders] create error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create folder")


@router.delete("/{folder_id}", status_code=204)
async def delete_folder(folder_id: str, user=Depends(require_user)):
    db = get_db()
    try:
        db.table("saved_folders").delete().eq("id", folder_id).eq("user_id", str(user.id)).execute()
    except Exception as exc:
        logger.error("[folders] delete error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete folder")


@router.get("/{folder_id}/items")
async def list_folder_items(folder_id: str, user=Depends(require_user)):
    db = get_db()
    try:
        # Verify ownership
        folder_res = (
            db.table("saved_folders")
            .select("id")
            .eq("id", folder_id)
            .eq("user_id", str(user.id))
            .maybe_single()
            .execute()
        )
        if not folder_res.data:
            raise HTTPException(status_code=404, detail="Folder not found")

        items_res = (
            db.table("folder_items")
            .select("*")
            .eq("folder_id", folder_id)
            .order("added_at", desc=True)
            .execute()
        )
        return items_res.data or []
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("[folders] list_items error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to fetch items")


@router.post("/{folder_id}/items", status_code=201)
async def add_item(folder_id: str, body: AddItemRequest, user=Depends(require_user)):
    db = get_db()
    try:
        folder_res = (
            db.table("saved_folders")
            .select("id")
            .eq("id", folder_id)
            .eq("user_id", str(user.id))
            .maybe_single()
            .execute()
        )
        if not folder_res.data:
            raise HTTPException(status_code=404, detail="Folder not found")

        res = (
            db.table("folder_items")
            .upsert({
                "folder_id": folder_id,
                "item_id": body.item_id,
                "item_type": body.item_type,
                "item_snapshot": body.item_snapshot,
            }, on_conflict="folder_id,item_id,item_type")
            .select()
            .single()
            .execute()
        )
        return res.data
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[folders] add_item error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to add item to folder")


@router.delete("/{folder_id}/items/{item_id}", status_code=204)
async def remove_item(folder_id: str, item_id: str, user=Depends(require_user)):
    db = get_db()
    try:
        folder_res = (
            db.table("saved_folders")
            .select("id")
            .eq("id", folder_id)
            .eq("user_id", str(user.id))
            .maybe_single()
            .execute()
        )
        if not folder_res.data:
            raise HTTPException(status_code=404, detail="Folder not found")

        db.table("folder_items").delete().eq("folder_id", folder_id).eq("id", item_id).execute()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[folders] remove_item error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to remove item")
