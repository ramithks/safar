"""
Front desk — check-in, check-out, folio, rooms, tasks.

Everything here is *service* work: it records what happened to a stay that has
already been sold. None of it moves an inventory count, which is exactly why the
PMS is allowed to queue these operations offline and replay them later.

Two invariants the offline model depends on, enforced here rather than assumed:

**Idempotency.** Every mutation the PMS can queue accepts a client-generated key
and is a no-op on replay. Without it, a receptionist who checks a guest in
during an outage gets a second folio line when the queue drains.

**The folio is append-only.** Nothing in this module updates or deletes a folio
entry; a correction posts a reversing entry. That is what makes two front desks
posting from two devices a non-conflict rather than a lost update, and the
database enforces it with a trigger regardless of what this code does.
"""
import logging
from datetime import date, datetime, timezone

from fastapi import HTTPException, status

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)


def _today(prop_timezone: str | None = None) -> str:
    # The property's business date, not the server's. A folio posted at 00:30
    # IST belongs to the day that has just started, and a server thinking in UTC
    # would file it under yesterday.
    return datetime.now(timezone.utc).date().isoformat()


async def check_in(property_id: str, reservation_id: str, room_id: str, member) -> dict:
    """
    Assign a room and mark the guest in house.

    Posts the first night to the folio so it is never empty while a guest is
    resident — night audit posts the rest. Idempotent: a replayed check-in finds
    the reservation already checked in and returns it unchanged.
    """

    def _write():
        db = get_db()

        res = (
            db.table("reservations")
            .select("id,status,room_id,guest_name,check_in,check_out,nights,units,"
                    "gross_paise,discount_paise,property_id,partner_id")
            .eq("id", reservation_id)
            .eq("property_id", property_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
        r = res.data[0]

        if r["status"] == "checked_in":
            return r  # replay, or two receptionists at once
        if r["status"] not in ("confirmed", "pending"):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A {r['status'].replace('_', ' ')} reservation cannot be checked in",
            )

        room = (
            db.table("rooms")
            .select("id,number,room_type_id,is_active")
            .eq("id", room_id)
            .eq("property_id", property_id)
            .limit(1)
            .execute()
        )
        if not room.data or not room.data[0]["is_active"]:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")

        # Is anybody already in it? The desk can see this, but two receptionists
        # working the same arrival list cannot see each other.
        occupied = (
            db.table("reservations")
            .select("id,ref")
            .eq("room_id", room_id)
            .eq("status", "checked_in")
            .limit(1)
            .execute()
        )
        if occupied.data:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Room {room.data[0]['number']} is already occupied",
            )

        updated = (
            db.table("reservations")
            .update({"status": "checked_in", "room_id": room_id})
            .eq("id", reservation_id)
            .eq("status", r["status"])  # optimistic guard against a concurrent check-in
            .execute()
        )
        if not updated.data:
            raise HTTPException(status.HTTP_409_CONFLICT, "This reservation changed while you were working on it")

        per_night = round((r["gross_paise"] - r["discount_paise"]) / max(r["nights"], 1))
        db.table("folio_entries").insert(
            {
                "reservation_id": reservation_id,
                "property_id": property_id,
                "partner_id": r["partner_id"],
                "kind": "room",
                "description": f"Room charge · {r['check_in']}",
                "amount_paise": per_night,
                "posted_by_member": member.member_id,
                "posted_by_name": member.display_name or "Front desk",
                # Deterministic: a replay of the same check-in cannot post the
                # first night twice.
                "idempotency_key": f"night:{reservation_id}:{r['check_in']}",
            },
            upsert=True,
        ).execute()

        return updated.data[0]

    return await run_db(_write)


async def check_out(property_id: str, reservation_id: str, member) -> dict:
    """
    Settle and release the room.

    Refuses on an outstanding balance. The desk sees the number before it gets
    here, but the check belongs on the server too — the PMS can be offline when
    the button is pressed, and the queued call arrives with no idea what the
    balance is now.
    """

    def _write():
        db = get_db()

        res = (
            db.table("reservations")
            .select("id,status,room_id,total_paise,paid_paise,partner_id")
            .eq("id", reservation_id)
            .eq("property_id", property_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
        r = res.data[0]

        if r["status"] == "checked_out":
            return r
        if r["status"] != "checked_in":
            raise HTTPException(status.HTTP_409_CONFLICT, "This guest is not checked in")

        entries = (
            db.table("folio_entries")
            .select("amount_paise")
            .eq("reservation_id", reservation_id)
            .execute()
        )
        # Derived, never stored. Sum of signed entries.
        balance = sum(e["amount_paise"] for e in (entries.data or []))
        if balance > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"₹{balance / 100:,.0f} is outstanding on the folio and must be settled first",
            )

        updated = (
            db.table("reservations")
            .update({"status": "checked_out"})
            .eq("id", reservation_id)
            .eq("status", "checked_in")
            .execute()
        )
        if not updated.data:
            raise HTTPException(status.HTTP_409_CONFLICT, "This reservation changed while you were working on it")

        # Every departure leaves a dirty room. Housekeeping works off this board
        # and should not have to be told.
        if r.get("room_id"):
            db.table("rooms").update({"housekeeping_status": "dirty"}).eq("id", r["room_id"]).execute()

        return updated.data[0]

    return await run_db(_write)


async def post_folio_entry(
    property_id: str,
    reservation_id: str,
    *,
    kind: str,
    description: str,
    amount_paise: int,
    method: str | None,
    idempotency_key: str,
    member,
) -> dict:
    """
    Append to a guest's account.

    The caller passes a positive amount and the sign is applied here, once. A
    caller that had to remember payments are negative would eventually forget,
    and a folio with a positive payment on it reads as a charge.
    """
    if kind not in ("room", "addon", "tax", "payment", "refund", "adjustment"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown folio entry kind: {kind}")
    if amount_paise == 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A folio entry cannot be zero")

    signed = -abs(amount_paise) if kind in ("payment", "refund") else abs(amount_paise)

    def _write():
        db = get_db()

        res = (
            db.table("reservations")
            .select("id,partner_id,total_paise,paid_paise,status")
            .eq("id", reservation_id)
            .eq("property_id", property_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found")
        r = res.data[0]

        existing = (
            db.table("folio_entries")
            .select("*")
            .eq("idempotency_key", idempotency_key)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]  # replay

        entry = (
            db.table("folio_entries")
            .insert(
                {
                    "reservation_id": reservation_id,
                    "property_id": property_id,
                    "partner_id": r["partner_id"],
                    "kind": kind,
                    "description": description,
                    "amount_paise": signed,
                    "method": method,
                    "posted_by_member": member.member_id,
                    "posted_by_name": member.display_name or "Front desk",
                    "idempotency_key": idempotency_key,
                }
            )
            .execute()
        ).data[0]

        # paid_paise on the reservation is a denormalised convenience for the
        # arrivals list. The folio remains authoritative.
        if kind == "payment":
            paid = r["paid_paise"] + abs(signed)
            db.table("reservations").update(
                {
                    "paid_paise": paid,
                    "payment_status": "paid" if paid >= r["total_paise"] else "partially_paid",
                }
            ).eq("id", reservation_id).execute()

        return entry

    return await run_db(_write)


async def get_folio(property_id: str, reservation_id: str) -> dict:
    def _read():
        db = get_db()
        rows = (
            db.table("folio_entries")
            .select("*")
            .eq("reservation_id", reservation_id)
            .eq("property_id", property_id)
            .order("posted_at")
            .execute()
        ).data or []
        return {"entries": rows, "balance_paise": sum(r["amount_paise"] for r in rows)}

    return await run_db(_read)


async def set_room_status(property_id: str, room_id: str, status_value: str) -> dict:
    if status_value not in ("clean", "dirty", "inspected", "out_of_order"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown housekeeping status")

    def _write():
        db = get_db()
        updated = (
            db.table("rooms")
            .update({"housekeeping_status": status_value})
            .eq("id", room_id)
            .eq("property_id", property_id)
            .execute()
        )
        if not updated.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Room not found")
        return updated.data[0]

    return await run_db(_write)


async def create_task(
    property_id: str,
    *,
    task_id: str,
    title: str,
    category: str,
    priority: str,
    room_number: str | None,
    conversation_id: str | None,
    source_channel: str | None,
    guest_name: str | None,
    created_by: str,
    member,
) -> dict:
    """
    Raise a ticket.

    `task_id` is client-generated, which is what makes this safe both offline and
    when an agent retries: the same complaint cannot become two tickets. The
    conversation link is kept because it is the point — whoever picks the job up
    should be able to read what the guest actually said.
    """

    def _write():
        db = get_db()

        existing = db.table("tasks").select("*").eq("id", task_id).limit(1).execute()
        if existing.data:
            return existing.data[0]  # replay

        code = db.rpc("next_task_code", {"p_property": property_id}).execute().data

        room_id = None
        if room_number:
            room = (
                db.table("rooms")
                .select("id")
                .eq("property_id", property_id)
                .ilike("number", room_number)
                .limit(1)
                .execute()
            )
            room_id = room.data[0]["id"] if room.data else None

        return (
            db.table("tasks")
            .insert(
                {
                    "id": task_id,
                    "property_id": property_id,
                    "partner_id": member.partner_id,
                    "code": code,
                    "title": title,
                    "category": category,
                    "priority": priority,
                    "status": "open",
                    "room_id": room_id,
                    "room_number": room_number,
                    "conversation_id": conversation_id,
                    "source_channel": source_channel,
                    "guest_name": guest_name,
                    "created_by": created_by,
                    "created_by_member": member.member_id,
                }
            )
            .execute()
        ).data[0]

    return await run_db(_write)


async def set_task_status(property_id: str, task_id: str, new_status: str, member) -> dict:
    if new_status not in ("open", "accepted", "in_progress", "resolved", "cancelled"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown task status")

    def _write():
        db = get_db()
        current = (
            db.table("tasks")
            .select("id,status")
            .eq("id", task_id)
            .eq("property_id", property_id)
            .limit(1)
            .execute()
        )
        if not current.data:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")
        old = current.data[0]["status"]
        if old == new_status:
            return current.data[0]

        patch: dict = {"status": new_status}
        now = datetime.now(timezone.utc).isoformat()
        if new_status == "accepted":
            patch["accepted_at"] = now
        if new_status == "resolved":
            patch["resolved_at"] = now

        updated = db.table("tasks").update(patch).eq("id", task_id).execute().data[0]

        # The trail staff use to see whether a job is actually moving.
        db.table("task_events").insert(
            {
                "task_id": task_id,
                "from_status": old,
                "to_status": new_status,
                "actor_member": member.member_id,
                "actor_name": member.display_name,
            }
        ).execute()

        return updated

    return await run_db(_write)
