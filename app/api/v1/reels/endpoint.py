"""
GET  /api/v1/reels/saved          →  user's saved reels
GET  /api/v1/reels/developing     →  reels being processed (reel_jobs)
POST /api/v1/reels/save           →  save a reel from a job
POST /api/v1/reels/{reel_id}/like →  toggle like on saved reel

Required Supabase tables (create in dashboard):

  CREATE TABLE saved_reels (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users,
    job_id text,
    title text NOT NULL,
    category text NOT NULL,
    thumbnail_url text DEFAULT '',
    description text DEFAULT '',
    vibes text[] DEFAULT '{}',
    likes_display text DEFAULT '0 likes',
    likes_count int DEFAULT 0,
    duration text DEFAULT '0:00 mins',
    is_liked boolean DEFAULT false,
    created_at timestamptz DEFAULT now()
  );
  ALTER TABLE saved_reels ENABLE ROW LEVEL SECURITY;
  CREATE POLICY "Users own reels" ON saved_reels FOR ALL USING (auth.uid() = user_id);
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.deps.auth import require_user
from app.db.client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reels", tags=["reels"])


class SaveReelRequest(BaseModel):
    job_id: str
    category: str
    title: str
    image: str = ""
    description: str = ""
    vibes: list[str] = []


@router.get("/saved")
async def get_saved_reels(user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("saved_reels")
            .select("*")
            .eq("user_id", str(user.id))
            .order("created_at", desc=True)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.warning("[reels] get_saved error: %s", exc)
        return []


@router.get("/developing")
async def get_developing_reels(user=Depends(require_user)):
    try:
        db = get_db()
        res = (
            db.table("reel_jobs")
            .select("*")
            .eq("user_id", str(user.id))
            .in_("status", ["pending", "processing", "completed"])
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.warning("[reels] get_developing error: %s", exc)
        return []


@router.post("/save", status_code=201)
async def save_reel(body: SaveReelRequest, user=Depends(require_user)):
    db = get_db()
    try:
        res = (
            db.table("saved_reels")
            .insert({
                "user_id": str(user.id),
                "job_id": body.job_id,
                "title": body.title,
                "category": body.category,
                "thumbnail_url": body.image,
                "description": body.description or "Discovered with TravBuddy AI.",
                "vibes": body.vibes or ["AI Curated"],
                "is_liked": False,
            })
            .select()
            .single()
            .execute()
        )

        # Mark job as saved so it leaves the developing queue
        try:
            db.table("reel_jobs").update({"status": "saved"}).eq("id", body.job_id).eq("user_id", str(user.id)).execute()
        except Exception:
            pass

        return res.data
    except Exception as exc:
        logger.error("[reels] save_reel error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to save reel")


@router.post("/{reel_id}/like")
async def toggle_like(reel_id: str, user=Depends(require_user)):
    db = get_db()
    try:
        res = (
            db.table("saved_reels")
            .select("is_liked")
            .eq("id", reel_id)
            .eq("user_id", str(user.id))
            .maybe_single()
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Reel not found")

        new_liked = not res.data["is_liked"]
        db.table("saved_reels").update({"is_liked": new_liked}).eq("id", reel_id).eq("user_id", str(user.id)).execute()
        return {"is_liked": new_liked}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[reels] toggle_like error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to toggle like")
