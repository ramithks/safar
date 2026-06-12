"""
GET  /api/v1/reels/saved          →  user's saved reels
POST /api/v1/reels/{reel_id}/like →  toggle like on saved reel

Completed pipeline jobs are auto-saved into saved_reels (see
app.storage.supabase_client.auto_save_completed_job) — there is no manual
save step.

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

from app.deps.auth import require_user
from app.db.client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reels", tags=["reels"])


@router.get("/saved")
def get_saved_reels(user=Depends(require_user)):
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


@router.post("/{reel_id}/like")
def toggle_like(reel_id: str, user=Depends(require_user)):
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
