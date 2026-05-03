"""
Community endpoints — plan-first verified social layer.

All endpoints require a valid Supabase JWT. Most require the user to be
an approved member (community_users.approval_status == 'approved').

Tables: community_users, community_plans, plan_participants,
plan_messages, community_reports, community_blocks, plan_payments

Paid-plan flow (Razorpay, manual capture):
    1. Host creates plan with is_paid=true, price_paise=N
    2. User taps "Pay & Request" → POST /plans/{id}/initiate-payment
       → backend creates Razorpay order, returns checkout config
    3. RN opens Razorpay SDK; on success posts to /payments/verify
       → backend verifies signature, marks payment authorized,
         inserts plan_participants row in 'requested' state
    4. Host accepts → backend captures payment, marks accepted
       Host rejects → backend refunds, marks rejected
    5. Razorpay webhooks reconcile async events at /payments/webhook
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.config import settings
from app.db.client import get_db
from app.deps.auth import require_user
from app.services import razorpay_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/community")


# ─── Models ──────────────────────────────────────────────────────────────────

class ApplicationPayload(BaseModel):
    name: str
    age: int = Field(ge=18, le=80)
    gender: str
    city: str = "Delhi NCR"
    occupation: str
    instagram_handle: Optional[str] = None
    linkedin_url: Optional[str] = None
    selfie_url: Optional[str] = None
    id_url: Optional[str] = None
    plan_types: list[str] = Field(default_factory=list)
    budget: str
    personality_vibe: str
    referral_code: Optional[str] = None


class CreatePlanPayload(BaseModel):
    title: str
    description: Optional[str] = ""
    location: str
    datetime: str  # ISO timestamp
    max_people: int = Field(default=6, ge=2, le=20)
    gender_pref: str = "any"
    vibe_tags: list[str] = Field(default_factory=list)
    cover_image: Optional[str] = None
    city: Optional[str] = None  # if omitted, derived from creator's profile
    # Paid plans: host sets price; free plans: capped at 10 participants
    is_paid: bool = False
    price_paise: Optional[int] = Field(default=None, ge=100)  # min ₹1
    currency: str = "INR"


class VerifyPaymentPayload(BaseModel):
    plan_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class RespondPayload(BaseModel):
    user_id: str
    action: str  # 'accept' | 'reject'


class MessagePayload(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class ReportPayload(BaseModel):
    target_user_id: str
    reason: str = Field(min_length=1, max_length=500)
    plan_id: Optional[str] = None


class BlockPayload(BaseModel):
    target_user_id: str


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _require_approved(db, user_id: str) -> dict:
    res = (
        db.table("community_users")
        .select("*")
        .eq("id", user_id)
        .maybe_single()
        .execute()
    )
    profile = res.data
    if not profile:
        raise HTTPException(status_code=403, detail="Not a community member")
    if profile.get("approval_status") != "approved":
        raise HTTPException(status_code=403, detail="Community application not approved")
    return profile


def _generate_referral_code(name: str) -> str:
    base = "".join(c for c in name.upper() if c.isalpha())[:4] or "USER"
    suffix = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4))
    return f"{base}-{suffix}"


def _shape_creator(profile: Optional[dict]) -> dict:
    if not profile:
        return {"id": "", "name": "Unknown", "gender": "Other", "occupation": ""}
    return {
        "id": profile["id"],
        "name": profile["name"],
        "gender": profile["gender"],
        "occupation": profile["occupation"],
    }


def _shape_plan(plan: dict, creator: dict, participants: list[dict], my_user_id: str) -> dict:
    accepted = [p for p in participants if p["status"] == "accepted"]
    male_count = sum(1 for p in accepted if (p.get("user") or {}).get("gender") == "Male")
    female_count = sum(1 for p in accepted if (p.get("user") or {}).get("gender") == "Female")
    my = next((p for p in participants if p["user_id"] == my_user_id), None)
    return {
        **plan,
        "creator": _shape_creator(creator),
        "participant_count": len(accepted),
        "male_count": male_count,
        "female_count": female_count,
        "my_status": my["status"] if my else None,
    }


def _load_participants_with_users(db, plan_ids: list[str]) -> dict[str, list[dict]]:
    if not plan_ids:
        return {}
    parts_res = (
        db.table("plan_participants")
        .select("*")
        .in_("plan_id", plan_ids)
        .execute()
    )
    rows = parts_res.data or []
    user_ids = list({r["user_id"] for r in rows})
    users_by_id = {}
    if user_ids:
        u_res = (
            db.table("community_users")
            .select("id,name,gender,occupation")
            .in_("id", user_ids)
            .execute()
        )
        users_by_id = {u["id"]: u for u in (u_res.data or [])}
    grouped: dict[str, list[dict]] = {pid: [] for pid in plan_ids}
    for r in rows:
        r["user"] = users_by_id.get(r["user_id"])
        grouped.setdefault(r["plan_id"], []).append(r)
    return grouped


def _blocked_user_ids(db, user_id: str) -> set[str]:
    res = (
        db.table("community_blocks")
        .select("blocked_id, blocker_id")
        .or_(f"blocker_id.eq.{user_id},blocked_id.eq.{user_id}")
        .execute()
    )
    out: set[str] = set()
    for r in res.data or []:
        if r["blocker_id"] == user_id:
            out.add(r["blocked_id"])
        else:
            out.add(r["blocker_id"])
    return out


# ─── Endpoints ───────────────────────────────────────────────────────────────

@router.get("/status")
async def get_status(user=Depends(require_user)):
    db = get_db()
    res = (
        db.table("community_users")
        .select("*")
        .eq("id", user.id)
        .maybe_single()
        .execute()
    )
    profile = res.data
    if not profile:
        return {"status": "not_applied"}
    return {"status": profile["approval_status"], "user": profile}


@router.post("/apply")
async def submit_application(payload: ApplicationPayload, user=Depends(require_user)):
    db = get_db()

    existing = (
        db.table("community_users")
        .select("id, approval_status")
        .eq("id", user.id)
        .maybe_single()
        .execute()
    )
    if existing.data:
        raise HTTPException(status_code=409, detail="Application already exists")

    referral_used_by: Optional[str] = None
    if payload.referral_code:
        ref = (
            db.table("community_users")
            .select("id")
            .eq("referral_code", payload.referral_code.strip().upper())
            .eq("approval_status", "approved")
            .maybe_single()
            .execute()
        )
        if not ref.data:
            raise HTTPException(status_code=400, detail="Invalid referral code")
        referral_used_by = ref.data["id"]

    db.table("community_users").insert({
        "id": user.id,
        "name": payload.name,
        "age": payload.age,
        "gender": payload.gender,
        "city": payload.city,
        "occupation": payload.occupation,
        "instagram_handle": payload.instagram_handle,
        "linkedin_url": payload.linkedin_url,
        "selfie_url": payload.selfie_url,
        "id_url": payload.id_url,
        "plan_types": payload.plan_types,
        "budget": payload.budget,
        "personality_vibe": payload.personality_vibe,
        "referral_code": _generate_referral_code(payload.name),
        "referral_used_by": referral_used_by,
        "approval_status": "pending",
    }).execute()

    return {"success": True}


@router.get("/plans")
async def list_plans(
    filter: Optional[str] = Query(default=None, pattern="^(today|upcoming)$"),
    city: Optional[str] = Query(default=None, min_length=1, max_length=120),
    user=Depends(require_user),
):
    db = get_db()
    profile = _require_approved(db, user.id)

    # City scoping: prefer the live city the client just resolved (device GPS),
    # fall back to the city stored on the community profile.
    effective_city = (city or "").strip() or (profile.get("city") or "").strip()
    if not effective_city:
        raise HTTPException(status_code=400, detail="City is required (enable location)")

    q = (
        db.table("community_plans")
        .select("*")
        .eq("is_cancelled", False)
        .ilike("city", effective_city)
        .order("datetime", desc=False)
    )

    now = datetime.now(timezone.utc)
    if filter == "today":
        end_of_day = now.replace(hour=23, minute=59, second=59).isoformat()
        q = q.gte("datetime", now.isoformat()).lte("datetime", end_of_day)
    elif filter == "upcoming":
        q = q.gte("datetime", now.isoformat())

    plans_res = q.execute()
    plans = plans_res.data or []
    if not plans:
        return {"plans": []}

    plan_ids = [p["id"] for p in plans]
    creator_ids = list({p["creator_id"] for p in plans})

    creators_res = (
        db.table("community_users")
        .select("id,name,gender,occupation")
        .in_("id", creator_ids)
        .execute()
    )
    creators_by_id = {c["id"]: c for c in (creators_res.data or [])}
    parts_by_plan = _load_participants_with_users(db, plan_ids)

    blocked = _blocked_user_ids(db, user.id)

    out = []
    for p in plans:
        if p["creator_id"] in blocked:
            continue
        out.append(_shape_plan(
            p,
            creators_by_id.get(p["creator_id"]),
            parts_by_plan.get(p["id"], []),
            user.id,
        ))

    return {"plans": out}


@router.get("/plans/{plan_id}")
async def get_plan(plan_id: str, user=Depends(require_user)):
    db = get_db()
    _require_approved(db, user.id)

    plan_res = (
        db.table("community_plans")
        .select("*")
        .eq("id", plan_id)
        .maybe_single()
        .execute()
    )
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan = plan_res.data

    creator_res = (
        db.table("community_users")
        .select("id,name,gender,occupation")
        .eq("id", plan["creator_id"])
        .maybe_single()
        .execute()
    )

    parts_by_plan = _load_participants_with_users(db, [plan_id])
    participants = parts_by_plan.get(plan_id, [])

    return {
        "plan": _shape_plan(plan, creator_res.data, participants, user.id),
        "participants": participants,
    }


@router.post("/plans")
async def create_plan(payload: CreatePlanPayload, user=Depends(require_user)):
    db = get_db()
    profile = _require_approved(db, user.id)

    # Free plans are capped at 10 participants; paid plans use the schema's 2-20 range
    if payload.is_paid:
        if not payload.price_paise or payload.price_paise <= 0:
            raise HTTPException(status_code=400, detail="Paid plans require price_paise > 0")
        if payload.currency != "INR":
            raise HTTPException(status_code=400, detail="Only INR is supported")
    else:
        if payload.max_people > 10:
            raise HTTPException(status_code=400, detail="Free plans are capped at 10 participants")

    plan_city = (payload.city or "").strip() or (profile.get("city") or "").strip()
    if not plan_city:
        raise HTTPException(status_code=400, detail="City is required to create a plan")

    insert_res = db.table("community_plans").insert({
        "creator_id": user.id,
        "title": payload.title,
        "description": payload.description or "",
        "location": payload.location,
        "city": plan_city,
        "datetime": payload.datetime,
        "max_people": payload.max_people,
        "is_paid": payload.is_paid,
        "price_paise": payload.price_paise if payload.is_paid else None,
        "currency": payload.currency or "INR",
        "gender_pref": payload.gender_pref,
        "vibe_tags": payload.vibe_tags,
        "cover_image": payload.cover_image,
    }).execute()
    plan = (insert_res.data or [None])[0]
    if not plan:
        raise HTTPException(status_code=500, detail="Failed to create plan")

    # Host is auto-accepted as a participant
    db.table("plan_participants").insert({
        "plan_id": plan["id"],
        "user_id": user.id,
        "status": "accepted",
    }).execute()

    creator_res = (
        db.table("community_users")
        .select("id,name,gender,occupation")
        .eq("id", user.id)
        .maybe_single()
        .execute()
    )
    parts_by_plan = _load_participants_with_users(db, [plan["id"]])
    return {
        "plan": _shape_plan(plan, creator_res.data, parts_by_plan.get(plan["id"], []), user.id),
    }


@router.post("/plans/{plan_id}/join")
async def join_plan(plan_id: str, user=Depends(require_user)):
    """Free-plan join. Paid plans must use /plans/{id}/initiate-payment."""
    db = get_db()
    _require_approved(db, user.id)

    plan_res = (
        db.table("community_plans")
        .select("creator_id, max_people, is_cancelled, is_paid")
        .eq("id", plan_id)
        .maybe_single()
        .execute()
    )
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan_res.data["is_cancelled"]:
        raise HTTPException(status_code=410, detail="Plan was cancelled")
    if plan_res.data["creator_id"] == user.id:
        raise HTTPException(status_code=400, detail="You are the host of this plan")
    if plan_res.data["is_paid"]:
        raise HTTPException(status_code=400, detail="Paid plan — use /initiate-payment")

    existing = (
        db.table("plan_participants")
        .select("id, status")
        .eq("plan_id", plan_id)
        .eq("user_id", user.id)
        .maybe_single()
        .execute()
    )
    if existing.data:
        return {"status": existing.data["status"]}

    db.table("plan_participants").insert({
        "plan_id": plan_id,
        "user_id": user.id,
        "status": "requested",
    }).execute()

    return {"status": "requested"}


@router.post("/plans/{plan_id}/initiate-payment")
async def initiate_payment(plan_id: str, user=Depends(require_user)):
    """
    Paid-plan join: create a Razorpay order in 'manual capture' mode.
    Returns the checkout config the RN SDK needs.
    """
    db = get_db()
    profile = _require_approved(db, user.id)

    plan_res = (
        db.table("community_plans").select("*").eq("id", plan_id).maybe_single().execute()
    )
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan = plan_res.data

    if not plan["is_paid"]:
        raise HTTPException(status_code=400, detail="Free plan — use /join")
    if plan["is_cancelled"]:
        raise HTTPException(status_code=410, detail="Plan was cancelled")
    if plan["creator_id"] == user.id:
        raise HTTPException(status_code=400, detail="You are the host of this plan")

    accepted_res = (
        db.table("plan_participants").select("id", count="exact")
        .eq("plan_id", plan_id).eq("status", "accepted").execute()
    )
    if (accepted_res.count or 0) >= plan["max_people"]:
        raise HTTPException(status_code=409, detail="Plan is full")

    existing = (
        db.table("plan_payments").select("*")
        .eq("plan_id", plan_id).eq("user_id", user.id).maybe_single().execute()
    )
    if existing.data and existing.data["status"] in ("authorized", "captured"):
        raise HTTPException(status_code=409, detail="You already have an active payment for this plan")

    receipt = f"plan_{plan_id[:8]}_{user.id[:8]}"
    try:
        order = razorpay_client.create_order(
            amount_paise=plan["price_paise"],
            currency=plan["currency"],
            receipt=receipt,
            notes={"plan_id": plan_id, "user_id": user.id},
        )
    except Exception as e:
        logger.error("Razorpay order creation failed: %s", e)
        raise HTTPException(status_code=502, detail="Payment provider error")

    db.table("plan_payments").upsert({
        "plan_id": plan_id,
        "user_id": user.id,
        "razorpay_order_id": order["id"],
        "amount_paise": plan["price_paise"],
        "currency": plan["currency"],
        "status": "created",
    }, on_conflict="plan_id,user_id").execute()

    return {
        "order_id": order["id"],
        "key_id": settings.razorpay_key_id,
        "amount": plan["price_paise"],
        "currency": plan["currency"],
        "name": "TravBuddy",
        "description": plan["title"],
        "prefill": {
            "email": getattr(user, "email", "") or "",
            "contact": (profile or {}).get("phone_number", "") or "",
            "name": (profile or {}).get("name", "") or "",
        },
    }


@router.post("/payments/verify")
async def verify_payment(payload: VerifyPaymentPayload, user=Depends(require_user)):
    """RN posts the (order_id, payment_id, signature) tuple after Razorpay checkout."""
    db = get_db()

    if not razorpay_client.verify_payment_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="Invalid payment signature")

    payment_res = (
        db.table("plan_payments").select("*")
        .eq("razorpay_order_id", payload.razorpay_order_id).maybe_single().execute()
    )
    if not payment_res.data:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment_res.data["user_id"] != user.id:
        raise HTTPException(status_code=403, detail="Not your payment")
    if payment_res.data["plan_id"] != payload.plan_id:
        raise HTTPException(status_code=400, detail="Plan mismatch")

    db.table("plan_payments").update({
        "razorpay_payment_id": payload.razorpay_payment_id,
        "status": "authorized",
    }).eq("razorpay_order_id", payload.razorpay_order_id).execute()

    db.table("plan_participants").upsert({
        "plan_id": payload.plan_id,
        "user_id": user.id,
        "status": "requested",
    }, on_conflict="plan_id,user_id").execute()

    return {"status": "authorized"}


@router.post("/payments/webhook")
async def razorpay_webhook(request: Request):
    """
    Public endpoint hit by Razorpay for async payment events.
    Verifies signature, then reconciles plan_payments.status idempotently.
    """
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")

    if not razorpay_client.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = data.get("event", "")
    payload_obj = data.get("payload", {})
    db = get_db()

    if event in ("payment.authorized", "payment.captured", "payment.failed"):
        p = (payload_obj.get("payment") or {}).get("entity") or {}
        order_id = p.get("order_id")
        if not order_id:
            return {"received": True}
        update: dict = {"razorpay_payment_id": p.get("id")}
        if event == "payment.authorized":
            update["status"] = "authorized"
        elif event == "payment.captured":
            update["status"] = "captured"
        elif event == "payment.failed":
            update["status"] = "failed"
            update["failure_reason"] = p.get("error_description") or "Payment failed"
        db.table("plan_payments").update(update).eq("razorpay_order_id", order_id).execute()

    elif event == "refund.processed":
        r = (payload_obj.get("refund") or {}).get("entity") or {}
        payment_id = r.get("payment_id")
        if payment_id:
            db.table("plan_payments").update({
                "status": "refunded",
                "razorpay_refund_id": r.get("id"),
            }).eq("razorpay_payment_id", payment_id).execute()

    return {"received": True}


@router.post("/plans/{plan_id}/respond")
async def respond_to_request(plan_id: str, payload: RespondPayload, user=Depends(require_user)):
    db = get_db()

    plan_res = (
        db.table("community_plans")
        .select("creator_id, max_people, is_paid")
        .eq("id", plan_id)
        .maybe_single()
        .execute()
    )
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan = plan_res.data
    if plan["creator_id"] != user.id:
        raise HTTPException(status_code=403, detail="Only the host can respond")

    if payload.action not in ("accept", "reject"):
        raise HTTPException(status_code=400, detail="action must be accept|reject")

    new_status = "accepted" if payload.action == "accept" else "rejected"

    if new_status == "accepted":
        accepted_count_res = (
            db.table("plan_participants")
            .select("id", count="exact")
            .eq("plan_id", plan_id)
            .eq("status", "accepted")
            .execute()
        )
        if (accepted_count_res.count or 0) >= plan["max_people"]:
            raise HTTPException(status_code=409, detail="Plan is full")

    # Paid plan: capture on accept, refund on reject. Free plan: no payment touch.
    if plan["is_paid"]:
        payment_res = (
            db.table("plan_payments").select("*")
            .eq("plan_id", plan_id).eq("user_id", payload.user_id).maybe_single().execute()
        )
        if not payment_res.data:
            raise HTTPException(status_code=409, detail="No payment record for this user")
        payment = payment_res.data

        if new_status == "accepted":
            if payment["status"] != "authorized":
                raise HTTPException(
                    status_code=409,
                    detail=f"Payment not in authorized state (got {payment['status']})",
                )
            try:
                razorpay_client.capture_payment(
                    payment["razorpay_payment_id"], payment["amount_paise"], payment["currency"]
                )
                db.table("plan_payments").update({"status": "captured"}).eq("id", payment["id"]).execute()
            except Exception as e:
                logger.error("Razorpay capture failed for payment %s: %s", payment["id"], e)
                raise HTTPException(status_code=502, detail="Payment capture failed")
        else:
            if payment["status"] in ("authorized", "captured"):
                try:
                    refund = razorpay_client.refund_payment(payment["razorpay_payment_id"])
                    db.table("plan_payments").update({
                        "status": "refunded",
                        "razorpay_refund_id": refund.get("id"),
                    }).eq("id", payment["id"]).execute()
                except Exception as e:
                    # Don't block rejection on refund API failure — webhook reconciles, support follows up
                    logger.error("Razorpay refund failed for payment %s: %s", payment["id"], e)

    update_res = (
        db.table("plan_participants")
        .update({"status": new_status})
        .eq("plan_id", plan_id)
        .eq("user_id", payload.user_id)
        .execute()
    )
    if not update_res.data:
        raise HTTPException(status_code=404, detail="Request not found")

    return {"success": True}


@router.get("/plans/{plan_id}/messages")
async def list_messages(plan_id: str, user=Depends(require_user)):
    db = get_db()
    _require_approved(db, user.id)

    # Caller must be the host or an accepted participant
    plan_res = (
        db.table("community_plans")
        .select("creator_id")
        .eq("id", plan_id)
        .maybe_single()
        .execute()
    )
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan_res.data["creator_id"] != user.id:
        part_res = (
            db.table("plan_participants")
            .select("status")
            .eq("plan_id", plan_id)
            .eq("user_id", user.id)
            .maybe_single()
            .execute()
        )
        if not part_res.data or part_res.data["status"] != "accepted":
            raise HTTPException(status_code=403, detail="Not a member of this plan")

    msgs_res = (
        db.table("plan_messages")
        .select("*")
        .eq("plan_id", plan_id)
        .order("created_at", desc=False)
        .execute()
    )
    messages = msgs_res.data or []
    sender_ids = list({m["sender_id"] for m in messages})

    senders_by_id: dict[str, str] = {}
    if sender_ids:
        s_res = (
            db.table("community_users")
            .select("id,name")
            .in_("id", sender_ids)
            .execute()
        )
        senders_by_id = {s["id"]: s["name"] for s in (s_res.data or [])}

    return {
        "messages": [
            {**m, "sender_name": senders_by_id.get(m["sender_id"], "Unknown")}
            for m in messages
        ]
    }


@router.post("/plans/{plan_id}/messages")
async def send_message(plan_id: str, payload: MessagePayload, user=Depends(require_user)):
    db = get_db()
    _require_approved(db, user.id)

    # Same access check as list_messages
    plan_res = (
        db.table("community_plans")
        .select("creator_id")
        .eq("id", plan_id)
        .maybe_single()
        .execute()
    )
    if not plan_res.data:
        raise HTTPException(status_code=404, detail="Plan not found")

    if plan_res.data["creator_id"] != user.id:
        part_res = (
            db.table("plan_participants")
            .select("status")
            .eq("plan_id", plan_id)
            .eq("user_id", user.id)
            .maybe_single()
            .execute()
        )
        if not part_res.data or part_res.data["status"] != "accepted":
            raise HTTPException(status_code=403, detail="Not a member of this plan")

    insert_res = db.table("plan_messages").insert({
        "plan_id": plan_id,
        "sender_id": user.id,
        "content": payload.content,
    }).execute()
    msg = (insert_res.data or [None])[0]
    if not msg:
        raise HTTPException(status_code=500, detail="Failed to send message")

    me_res = (
        db.table("community_users")
        .select("name")
        .eq("id", user.id)
        .maybe_single()
        .execute()
    )

    return {"message": {**msg, "sender_name": (me_res.data or {}).get("name", "Unknown")}}


@router.post("/report")
async def report_user(payload: ReportPayload, user=Depends(require_user)):
    db = get_db()
    if payload.target_user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot report yourself")

    db.table("community_reports").insert({
        "reporter_id": user.id,
        "target_user_id": payload.target_user_id,
        "plan_id": payload.plan_id,
        "reason": payload.reason,
    }).execute()
    return {"success": True}


@router.post("/block")
async def block_user(payload: BlockPayload, user=Depends(require_user)):
    db = get_db()
    if payload.target_user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot block yourself")

    db.table("community_blocks").upsert({
        "blocker_id": user.id,
        "blocked_id": payload.target_user_id,
    }).execute()
    return {"success": True}


@router.get("/referral/validate")
async def validate_referral(code: str = Query(min_length=1), user=Depends(require_user)):
    db = get_db()
    res = (
        db.table("community_users")
        .select("id,name")
        .eq("referral_code", code.strip().upper())
        .eq("approval_status", "approved")
        .maybe_single()
        .execute()
    )
    if not res.data:
        return {"valid": False}
    return {"valid": True, "referrer_name": res.data["name"]}
