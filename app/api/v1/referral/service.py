"""
Referral + travel-wallet service (docs/growth_os_plan.md Phase 2).

Rules enforced here:
  * One code per user, minted lazily on first ask ("TB-NAME-3F2A").
  * One redemption per user (`referred_by` set exactly once); self-referral
    and referral cycles (A refers B, B refers A) are rejected.
  * ₹500 lands for the NEW user immediately on redeem (`referral_signup`);
    the REFERRER's ₹500 (`referral_reward`) is awarded only when the referred
    user completes their first real action (trip generated / group joined) —
    the fraud guard. Awarding is idempotent via the ledger's unique
    (user_id, reason, ref_id) index.
  * Wallet balance is always SUM(ledger) — never a stored counter.

Sync functions (supabase client) — run via FastAPI threadpool like groups.
"""
from __future__ import annotations

import logging
import re
import secrets

logger = logging.getLogger(__name__)

SIGNUP_CREDIT = 500      # ₹ for the new user, at redeem
REFERRER_CREDIT = 500    # ₹ for the code owner, at referred user's first action


def _db():
    from app.db.client import get_db
    return get_db()


def _one_or_none(q):
    res = q.limit(1).execute()
    return res.data[0] if res.data else None


def _mint_code(display_name: str | None) -> str:
    stem = re.sub(r"[^A-Z]", "", (display_name or "").upper())[:6] or "BUDDY"
    return f"TB-{stem}-{secrets.token_hex(2).upper()}"


def get_or_create_code(user_id: str, display_name: str | None = None) -> dict:
    """The user's referral row, minted on first ask. Collision-retried."""
    db = _db()
    row = _one_or_none(db.table("referral_codes").select("*").eq("user_id", user_id))
    if row:
        return row
    for _ in range(4):
        try:
            res = db.table("referral_codes").insert({
                "user_id": user_id,
                "code": _mint_code(display_name),
                "display_name": (display_name or "").strip() or None,
            }).execute()
            return res.data[0]
        except Exception as exc:  # noqa: BLE001 — unique collision on code OR user_id race
            if "referral_codes_pkey" in str(exc):
                row = _one_or_none(db.table("referral_codes").select("*").eq("user_id", user_id))
                if row:
                    return row
            logger.debug("[referral] code mint retry for %s: %s", user_id, exc)
    raise RuntimeError("could not mint referral code")


def wallet(user_id: str) -> dict:
    """Balance + recent ledger + referral stats — the Travel Wallet payload."""
    db = _db()
    rows = (db.table("credit_ledger").select("amount, reason, note, created_at")
            .eq("user_id", user_id).order("created_at", desc=True).limit(50)
            .execute().data or [])
    referred = (db.table("referral_codes").select("user_id")
                .eq("referred_by", user_id).execute().data or [])
    rewarded = sum(1 for r in rows if r["reason"] == "referral_reward")
    return {
        "balance": sum(r["amount"] for r in rows),
        "ledger": rows[:10],
        "referred_count": len(referred),
        "pending_rewards": max(len(referred) - rewarded, 0),
    }


def me(user_id: str, display_name: str | None = None) -> dict:
    code_row = get_or_create_code(user_id, display_name)
    return {
        "code": code_row["code"],
        "member_no": code_row["member_no"],
        "display_name": code_row.get("display_name"),
        "redeemed": bool(code_row.get("referred_by")),
        **wallet(user_id),
    }


def redeem(user_id: str, code: str, display_name: str | None = None) -> dict:
    """Attach `code`'s owner as this user's referrer and credit the signup
    bonus. Idempotent-hostile by design: second attempts 400."""
    db = _db()
    code = (code or "").strip().upper()
    owner = _one_or_none(db.table("referral_codes").select("user_id, code").eq("code", code))
    if not owner:
        raise ValueError("That referral code doesn't exist.")
    if owner["user_id"] == user_id:
        raise ValueError("You can't redeem your own code.")

    mine = get_or_create_code(user_id, display_name)
    if mine.get("referred_by"):
        raise ValueError("You've already used a referral code.")
    # Cycle guard: my code's owner can't be someone I referred.
    owner_row = _one_or_none(
        db.table("referral_codes").select("referred_by").eq("user_id", owner["user_id"]))
    if owner_row and owner_row.get("referred_by") == user_id:
        raise ValueError("You two are already connected — code not applicable.")

    db.table("referral_codes").update({"referred_by": owner["user_id"]}) \
        .eq("user_id", user_id).is_("referred_by", "null").execute()
    # Re-read: a concurrent redeem may have won the conditional update.
    mine = _one_or_none(db.table("referral_codes").select("referred_by").eq("user_id", user_id))
    if not mine or mine.get("referred_by") != owner["user_id"]:
        raise ValueError("You've already used a referral code.")

    _credit(user_id, SIGNUP_CREDIT, "referral_signup", ref_id=owner["user_id"],
            note=f"Welcome bonus — joined with {code}")
    return {"ok": True, "credited": SIGNUP_CREDIT}


def _credit(user_id: str, amount: int, reason: str, *, ref_id: str | None, note: str) -> bool:
    """Append one ledger row. The partial unique index makes (user, reason,
    ref) idempotent — a duplicate award is a silent no-op."""
    db = _db()
    try:
        db.table("credit_ledger").insert({
            "user_id": user_id, "amount": amount, "reason": reason,
            "ref_id": ref_id, "note": note,
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        if "idx_credit_ledger_once" in str(exc) or "23505" in str(exc):
            return False
        raise


def award_referrer_on_first_action(referred_user_id: str, action: str) -> None:
    """Called when a referred user completes their first real action (trip
    generation finished, group joined). Pays the REFERRER their ₹500 exactly
    once. Best-effort: never raises into the calling flow."""
    try:
        db = _db()
        row = _one_or_none(db.table("referral_codes")
                           .select("referred_by").eq("user_id", referred_user_id))
        referrer = (row or {}).get("referred_by")
        if not referrer:
            return
        if _credit(referrer, REFERRER_CREDIT, "referral_reward",
                   ref_id=referred_user_id,
                   note=f"Your friend completed their first {action} 🎉"):
            logger.info("[referral] rewarded %s for %s's first %s",
                        referrer, referred_user_id, action)
    except Exception as exc:  # noqa: BLE001 — growth layer must not break product flows
        logger.warning("[referral] award failed for %s: %s", referred_user_id, exc)


# ── public group trips (community/homepage showcase) ─────────────────────────

def live_groups(limit: int = 8) -> list[dict]:
    """Sanitized public group trips, newest first. Only fields safe to show
    strangers; the invite_code is exposed deliberately — public groups WANT
    joiners (that's what is_public means)."""
    db = _db()
    rows = (db.table("trip_groups")
            .select("id, name, destination, trip_date, max_members, invite_code, created_at, group_members(id)")
            .eq("is_public", True)
            .order("created_at", desc=True).limit(limit).execute().data or [])
    out = []
    for g in rows:
        members = len(g.get("group_members") or [])
        seats = (g["max_members"] - members) if g.get("max_members") else None
        out.append({
            "id": g["id"], "name": g["name"], "destination": g["destination"],
            "trip_date": g.get("trip_date"), "member_count": members,
            "seats_left": seats, "invite_code": g["invite_code"],
        })
    return out


def recent_travelers(limit: int = 12) -> list[dict]:
    """Social-proof strip: recent community joiners (first name + when).
    Reads referral_codes (community membership) — display_name only."""
    db = _db()
    rows = (db.table("referral_codes")
            .select("display_name, member_no, created_at")
            .order("created_at", desc=True).limit(limit).execute().data or [])
    return [
        {"name": (r.get("display_name") or "A traveler").split(" ")[0],
         "member_no": r["member_no"], "joined_at": r["created_at"]}
        for r in rows
    ]
