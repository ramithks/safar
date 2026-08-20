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
from app.services.pms import clock

logger = logging.getLogger(__name__)


def _business_date(property_id: str) -> str:
    # The property's business date, not the server's. A folio posted at 00:30
    # IST belongs to the day that has just started, and a server thinking in UTC
    # would file it under yesterday. See services/pms/clock.py.
    return clock.business_date_iso(property_id)


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

        # Post the nights due so far, so the folio is never empty while a guest
        # is resident and a late check-in does not owe two nights it has not
        # been billed for. Everything after today is the night audit's job.
        #
        # `post_room_nights` is idempotent on the folio's unique key, so a
        # replayed check-in — an outbox draining after an outage, two
        # receptionists pressing at once — posts nothing the second time.
        db.rpc(
            "post_room_nights",
            {
                "p_reservation": reservation_id,
                "p_through": _business_date(property_id),
                "p_posted_by": member.display_name or "Front desk",
            },
        ).execute()

        return updated.data[0]

    return await run_db(_write)


def _count_the_stay(db, property_id: str, reservation: dict) -> None:
    """
    A departure is what makes somebody a returning guest.

    `guests.stays_count`, `total_spend_paise` and `last_stay_at` existed and
    nothing ever wrote them, so every guest in the system had stayed zero times
    and spent nothing. The consequences were quiet and total: the `repeat_guest`
    marketing segment reads `stays_count >= 2` and could never match anybody,
    the guest list showed 0 stays beside a guest on their fourth visit, and a
    VIP was whoever somebody had ticked by hand.

    **Counted at check-out, not at booking.** A booking is an intention; a
    check-out is a stay that happened. Counting at booking would make a guest
    who cancelled twice look like a regular.

    Guarded on the status transition above: `check_out` only reaches here when
    it won the `status='checked_in'` update, so a second call returns early and
    this cannot run twice for one stay. A guest who is walked out and back in
    by the desk is the one case that double-counts, and a stay counted twice is
    a far smaller wrong than every stay counted never.

    Never fatal. A guest who has left has left; failing their check-out because
    a statistic could not be written would strand a room.
    """
    guest_id = reservation.get("guest_id")
    if not guest_id:
        return
    try:
        rows = (db.table("guests").select("stays_count,total_spend_paise")
                .eq("id", guest_id).eq("property_id", property_id)
                .limit(1).execute().data or [])
        if not rows:
            return
        db.table("guests").update({
            "stays_count": int(rows[0].get("stays_count") or 0) + 1,
            # What they actually paid, not what the stay was priced at: a
            # comped night is not spend.
            "total_spend_paise": int(rows[0].get("total_spend_paise") or 0)
                                 + int(reservation.get("paid_paise") or 0),
            "last_stay_at": reservation.get("check_out"),
        }).eq("id", guest_id).execute()
    except Exception:  # noqa: BLE001 — a statistic must not strand a room
        logger.warning("could not count the stay for guest %s", guest_id, exc_info=True)


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
            .select("id,status,room_id,check_in,check_out,total_paise,paid_paise,partner_id")
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

        # Bill the whole stay before deciding whether it is settled. Without
        # this, a guest departing before the night audit has caught up walks out
        # on a folio that reads zero because the nights were never posted — the
        # balance check would pass on an incomplete bill, which is a worse
        # failure than refusing a legitimate check-out.
        #
        # Through the departure date, which posts every night of the stay:
        # nights are half-open, so no night row is dated check_out. A guest
        # leaving early still owes the nights the reservation sold — the room
        # was held and could not be re-let, and shortening a stay is an
        # amendment somebody makes deliberately, not a side effect of walking to
        # the desk.
        db.rpc(
            "post_room_nights",
            {
                "p_reservation": reservation_id,
                "p_through": r["check_out"],
                "p_posted_by": member.display_name or "Front desk",
            },
        ).execute()

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

        _count_the_stay(db, property_id, updated.data[0])

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

    # Only a payment is negative. A refund hands money back, so it *undoes* a
    # payment and moves the balance the same way a charge does — storing it
    # negative, as this did until the night-audit work, made refunding a guest
    # look like collecting from them twice.
    signed = -abs(amount_paise) if kind == "payment" else abs(amount_paise)

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

        # Keep the reservation's two running totals in step with the folio.
        #
        # They answer a different question from the folio — what the stay is
        # contracted to cost and what has been collected, versus what has been
        # billed so far — but they must not be able to *contradict* it. The rule
        # is one line: a charge raises the total, a payment raises paid, a refund
        # lowers it. Miss any of those and the drawer and the folio disagree,
        # which is the bug this milestone exists to close.
        #
        # Room nights are not here on purpose: post_room_nights writes them
        # straight to the folio and they were already in total_paise the moment
        # the booking was taken.
        patch: dict = {}
        if kind in ("room", "addon", "tax", "adjustment"):
            patch["total_paise"] = r["total_paise"] + signed
        elif kind == "payment":
            patch["paid_paise"] = r["paid_paise"] + abs(signed)
        elif kind == "refund":
            patch["paid_paise"] = max(0, r["paid_paise"] - abs(signed))

        if patch:
            total = patch.get("total_paise", r["total_paise"])
            paid = patch.get("paid_paise", r["paid_paise"])
            patch["payment_status"] = (
                "paid" if paid >= total else "partially_paid" if paid > 0 else "unpaid"
            )
            db.table("reservations").update(patch).eq("id", reservation_id).execute()

        return entry

    return await run_db(_write)


async def run_night_audit(property_id: str, business_date: str | None = None) -> dict:
    """
    Post the room nights for everyone in house.

    pg_cron sweeps this hourly, closing each property's day once its own local
    midnight has passed. It is exposed to the desk as well because the
    interesting case is the property that was closed, offline, or simply not
    looked at for two days: a manager needs to be able to bring the folios up to
    date without waiting for the sweep, and it is safe to press because
    `post_room_nights` is idempotent — running it four times posts each night
    once.

    With no date, this closes the night that has just ended on the *property's*
    clock — `clock.audit_date_for`, the same arithmetic `run_night_audit_due()`
    uses, so pressing the button at 15:00 and the 01:05 sweep agree.
    """

    def _run():
        target = business_date or clock.audit_date_for(property_id).isoformat()
        rows = get_db().rpc(
            "run_night_audit",
            {"p_property": property_id, "p_business_date": target},
        ).execute().data or []
        summary = rows[0] if rows else {"reservations": 0, "entries": 0}

        # Record the close, so "did last night run" is a query rather than a
        # hunch — and so the hourly sweep does not re-close a day a human
        # already closed by hand.
        try:
            get_db().table("night_audit_runs").upsert(
                {
                    "property_id": property_id,
                    "business_date": target,
                    "reservations": summary.get("reservations", 0),
                    "entries": summary.get("entries", 0),
                    "trigger": "manual",
                },
                on_conflict="property_id,business_date",
            ).execute()
        except Exception:
            # The charges are posted; failing to write the diary entry is not a
            # reason to tell the manager the audit failed.
            logger.warning("could not record night audit run", exc_info=True)

        return {
            "businessDate": target,
            "reservations": summary.get("reservations", 0),
            "entries": summary.get("entries", 0),
        }

    return await run_db(_run)


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
