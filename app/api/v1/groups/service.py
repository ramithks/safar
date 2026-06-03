"""
Group service — Supabase CRUD + Gemini AI for trip squads.
"""
import secrets
from app.db.client import get_db
from app.services.vertex import vertex_generate


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db():
    return get_db()


def _require_member(db, group_id: str, user_id: str):
    """Raise ValueError if user is not a group member."""
    res = db.table("group_members").select("id").eq("group_id", group_id).eq("user_id", user_id).maybe_single().execute()
    if not res.data:
        raise ValueError("Not a group member")


# ── Groups ────────────────────────────────────────────────────────────────────

def create_group(user_id: str, name: str, destination: str | None, trip_id: str | None, display_name: str) -> dict:
    db = _db()
    invite_code = secrets.token_urlsafe(8)

    res = db.table("trip_groups").insert({
        "name": name,
        "destination": destination,
        "trip_id": trip_id,
        "created_by": user_id,
        "invite_code": invite_code,
    }).execute()
    group = res.data[0]
    group_id = group["id"]

    db.table("group_members").insert({
        "group_id": group_id,
        "user_id": user_id,
        "display_name": display_name,
        "role": "leader",
    }).execute()

    return {
        **group,
        "role": "leader",
        "is_member": True,
        "member_count": 1,
        "members": [{"user_id": user_id, "display_name": display_name, "role": "leader"}],
    }


def get_group(group_id: str, user_id: str) -> dict | None:
    db = _db()
    res = db.table("trip_groups").select("*").eq("id", group_id).maybe_single().execute()
    if not res.data:
        return None
    group = res.data

    members_res = db.table("group_members").select("*").eq("group_id", group_id).execute()
    members = members_res.data or []

    my = next((m for m in members if m["user_id"] == user_id), None)
    return {
        **group,
        "members": members,
        "member_count": len(members),
        "role": my["role"] if my else None,
        "is_member": my is not None,
    }


def join_group(invite_code: str, user_id: str, display_name: str) -> dict:
    db = _db()
    res = db.table("trip_groups").select("*").eq("invite_code", invite_code).maybe_single().execute()
    if not res.data:
        raise ValueError("Invalid invite code")
    group_id = res.data["id"]

    # Idempotent — return existing membership if already joined
    existing = db.table("group_members").select("id").eq("group_id", group_id).eq("user_id", user_id).maybe_single().execute()
    if not existing.data:
        db.table("group_members").insert({
            "group_id": group_id,
            "user_id": user_id,
            "display_name": display_name,
            "role": "member",
        }).execute()

    return get_group(group_id, user_id)


# ── Messages ──────────────────────────────────────────────────────────────────

def list_messages(group_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    db = _db()
    res = (
        db.table("group_messages")
        .select("*")
        .eq("group_id", group_id)
        .order("created_at", desc=False)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return res.data or []


def send_message(
    group_id: str,
    user_id: str,
    sender_name: str,
    text: str,
    msg_type: str = "text",
    poll_data: dict | None = None,
) -> dict:
    db = _db()
    payload: dict = {
        "group_id": group_id,
        "user_id": user_id,
        "sender_name": sender_name,
        "text": text,
        "type": msg_type,
    }
    if poll_data:
        payload["poll_data"] = poll_data
    res = db.table("group_messages").insert(payload).execute()
    return res.data[0]


def vote_poll(group_id: str, message_id: str, option_id: str, user_id: str, voter_name: str) -> dict:
    db = _db()
    res = (
        db.table("group_messages")
        .select("*")
        .eq("id", message_id)
        .eq("group_id", group_id)
        .maybe_single()
        .execute()
    )
    if not res.data:
        raise ValueError("Message not found")

    poll = dict(res.data.get("poll_data") or {})
    options = poll.get("options") or []

    # One vote per person — remove from all then add to chosen
    for opt in options:
        opt["votes"] = [v for v in (opt.get("votes") or []) if v != voter_name]

    for opt in options:
        if opt["id"] == option_id:
            opt.setdefault("votes", []).append(voter_name)
            break

    poll["options"] = options
    updated = db.table("group_messages").update({"poll_data": poll}).eq("id", message_id).execute()
    return updated.data[0]


# ── Expenses ──────────────────────────────────────────────────────────────────

def list_expenses(group_id: str) -> dict:
    db = _db()
    res = (
        db.table("group_expenses")
        .select("*")
        .eq("group_id", group_id)
        .order("created_at", desc=False)
        .execute()
    )
    expenses = res.data or []

    paid_map: dict[str, float] = {}
    net_map: dict[str, float] = {}

    for e in expenses:
        payer = e["paid_by"]
        amount = float(e["amount"])
        split = e.get("split_between") or []

        paid_map[payer] = paid_map.get(payer, 0.0) + amount
        net_map[payer] = net_map.get(payer, 0.0) + amount

        if split:
            share = amount / len(split)
            for person in split:
                net_map[person] = net_map.get(person, 0.0) - share

    # Minimum-transaction settlements
    debtors  = sorted([(k, v) for k, v in net_map.items() if v < -0.01], key=lambda x: x[1])
    creditors = sorted([(k, v) for k, v in net_map.items() if v > 0.01], key=lambda x: -x[1])

    settlements: list[dict] = []
    dc = [[k, v] for k, v in debtors]
    cc = [[k, v] for k, v in creditors]
    i = j = 0
    while i < len(dc) and j < len(cc):
        amount = min(-dc[i][1], cc[j][1])
        settlements.append({"from": dc[i][0], "to": cc[j][0], "amount": round(amount, 2)})
        dc[i][1] += amount
        cc[j][1] -= amount
        if abs(dc[i][1]) < 0.01:
            i += 1
        if abs(cc[j][1]) < 0.01:
            j += 1

    return {
        "expenses": expenses,
        "total_spent": round(sum(float(e["amount"]) for e in expenses), 2),
        "paid_summary": paid_map,
        "net_balances": net_map,
        "settlements": settlements,
    }


def add_expense(
    group_id: str,
    user_id: str,
    title: str,
    amount: float,
    paid_by: str,
    split_between: list[str],
    category: str,
    date: str,
) -> dict:
    db = _db()
    res = db.table("group_expenses").insert({
        "group_id": group_id,
        "title": title,
        "amount": amount,
        "paid_by": paid_by,
        "paid_by_user_id": user_id,
        "split_between": split_between,
        "category": category,
        "date": date,
    }).execute()
    return res.data[0]


def delete_expense(group_id: str, expense_id: str, user_id: str) -> None:
    db = _db()
    db.table("group_expenses").delete().eq("id", expense_id).eq("group_id", group_id).eq("paid_by_user_id", user_id).execute()


# ── AI Assistant ──────────────────────────────────────────────────────────────

async def ai_assist(group_id: str, query: str, destination: str | None) -> str:
    db = _db()

    msgs_res = (
        db.table("group_messages")
        .select("sender_name, text, type, created_at")
        .eq("group_id", group_id)
        .order("created_at", desc=True)
        .limit(15)
        .execute()
    )
    messages = list(reversed(msgs_res.data or []))

    exp_res = (
        db.table("group_expenses")
        .select("title, amount, paid_by, split_between, category")
        .eq("group_id", group_id)
        .execute()
    )
    expenses = exp_res.data or []
    total_spent = sum(float(e["amount"]) for e in expenses)

    chat_ctx = "\n".join(f"{m['sender_name']}: {m['text']}" for m in messages) or "No messages yet."
    exp_ctx  = "\n".join(f"- {e['title']}: ₹{e['amount']} paid by {e['paid_by']}" for e in expenses) or "No expenses yet."

    prompt = f"""You are a helpful AI assistant for a travel group trip to {destination or 'their destination'}.

Recent group chat (last 15 messages):
{chat_ctx}

Group expenses:
{exp_ctx}
Total spent: ₹{total_spent:.0f}

User question: {query}

Instructions:
- Answer concisely (under 150 words).
- If asked about expense splits or who owes whom, calculate precisely from the data.
- If asked to add/remove itinerary activities, describe the change you'd make.
- Be friendly and travel-focused.
"""

    try:
        raw = await vertex_generate(prompt, timeout=30.0)
        return (raw or "I'm here to help! Ask me anything about your trip.").strip()
    except Exception:
        return "Sorry, I couldn't process that right now. Try again in a moment."
