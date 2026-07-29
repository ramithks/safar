"""
Stay availability — reading it, and moving it.

Until the 20260729_stay_availability migration a stay could be booked for any
dates, any number of times: nothing counted rooms and nothing decremented them.
This module is the backend half of fixing that.

Two rules the rest of the code depends on:

  * **Stock moves in exactly one place.** `claim_stay_nights` is called from
    `finalise_booking` and nowhere else, on the transition to `paid`. Checking
    availability at quote or checkout time is advisory — it makes the UI honest
    and refuses the obviously-doomed order early, but between that check and the
    payment landing another traveller can take the room. Losing that race is a
    handled outcome (refund), not a bug to design away.

  * **Never read-then-write.** The claim is one guarded statement in Postgres
    that either books every night in the range or none of them, returning -1
    when it cannot. Same contract as `claim_departure_seats`; see
    supabase/migrations/20260729_stay_availability.sql §7.

A listing with no room types configured has no inventory to check, and is left
bookable exactly as it was before this existed. Refusing those instead would
take every stay that has not yet been migrated offline.
"""
import logging
from datetime import date, datetime

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# Matches the guard inside claim_stay_nights: one call must not walk the year.
MAX_NIGHTS = 90


def _as_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


async def room_types(listing_id: str) -> list[dict]:
    """
    Active room types for a listing, cheapest first.

    Swallows read failures for the same reason `availability` does, and for one
    more: this is the first thing `check_stay` touches, so an exception here
    would propagate out through /quote and /checkout and take stay booking down
    entirely. Returning [] instead means "nothing is tracked", which is the
    pre-migration behaviour — bookable, unguarded, exactly as it was. That also
    makes the deploy order not matter: shipping this before the migration lands
    degrades to the old behaviour rather than 500ing every stay.
    """
    try:
        res = await run_db(
            lambda: get_db()
            .table("listing_room_types")
            .select("id, name, default_units, occupancy, base_price, sort_order")
            .eq("listing_id", listing_id)
            .eq("is_active", True)
            .order("sort_order")
            .execute()
        )
        return (res.data if res else None) or []
    except Exception:  # noqa: BLE001
        logger.exception("[availability] room_types failed for %s", listing_id)
        return []


async def availability(listing_id: str, check_in, check_out) -> list[dict]:
    """
    Per-room-type, per-night availability over [check_in, check_out).

    Reads through `stay_availability`, which materialises the sparse-override
    inheritance so callers never reimplement the coalesce.
    """
    ci, co = _as_date(check_in), _as_date(check_out)
    if not ci or not co or co <= ci:
        return []

    try:
        res = await run_db(
            lambda: get_db()
            .rpc(
                "stay_availability",
                {
                    "p_listing": listing_id,
                    "p_from": ci.isoformat(),
                    "p_to": co.isoformat(),
                },
            )
            .execute()
        )
        return (res.data if res else None) or []
    except Exception:  # noqa: BLE001
        # A read failing must not take checkout down with it — the authoritative
        # guard is the claim at finalise time, and this is the advisory pass.
        logger.exception("[availability] stay_availability failed for %s", listing_id)
        return []


def sold_out_nights(rows: list[dict], room_type_id: str | None, rooms: int) -> list[str]:
    """
    Which nights cannot take `rooms` more.

    With no room type named, a night counts as available when *any* room type
    can take it — the traveller has not chosen yet, so refusing on the strictest
    room would hide inventory that is genuinely for sale.
    """
    if not rows:
        return []

    by_night: dict[str, bool] = {}
    for row in rows:
        if room_type_id and row.get("room_type_id") != room_type_id:
            continue
        night = str(row.get("date"))[:10]
        fits = (
            not row.get("stop_sell")
            and int(row.get("units_left") or 0) >= rooms
        )
        by_night[night] = by_night.get(night, False) or fits

    return sorted(night for night, ok in by_night.items() if not ok)


def unmet_min_nights(rows: list[dict], room_type_id: str | None, nights: int) -> int | None:
    """
    The largest min-nights rule the requested stay breaks, if any.

    A partner sets these to stop one-night bookings eating a long weekend. Only
    the check-in night's rule applies — that is the convention every channel
    manager uses, and applying every night's rule would make a stay that spans a
    restricted night impossible rather than merely constrained.
    """
    if not rows:
        return None

    first = min((str(r.get("date"))[:10] for r in rows), default=None)
    if not first:
        return None

    worst = None
    for row in rows:
        if room_type_id and row.get("room_type_id") != room_type_id:
            continue
        if str(row.get("date"))[:10] != first:
            continue
        req = row.get("min_nights")
        if req and int(req) > nights:
            worst = max(worst or 0, int(req))
    return worst


async def check_stay(
    listing_id: str,
    check_in,
    check_out,
    rooms: int,
    room_type_id: str | None = None,
) -> dict:
    """
    The advisory pre-flight used by /quote and /checkout.

    Returns {"ok", "sold_out_nights", "units_left", "min_nights", "tracked"}.
    `tracked` is False for a listing with no room types — nothing is being
    counted, so nothing can be refused.
    """
    types = await room_types(listing_id)
    if not types:
        return {"ok": True, "sold_out_nights": [], "units_left": None, "tracked": False}

    ci, co = _as_date(check_in), _as_date(check_out)
    if not ci or not co or co <= ci:
        return {"ok": True, "sold_out_nights": [], "units_left": None, "tracked": True}

    nights = (co - ci).days
    rows = await availability(listing_id, ci, co)
    if not rows:
        # The read failed or the listing is invisible; do not block on it.
        return {"ok": True, "sold_out_nights": [], "units_left": None, "tracked": True}

    blocked = sold_out_nights(rows, room_type_id, rooms)
    min_req = unmet_min_nights(rows, room_type_id, nights)

    relevant = [
        r for r in rows if not room_type_id or r.get("room_type_id") == room_type_id
    ]
    units_left = min((int(r.get("units_left") or 0) for r in relevant), default=None)

    return {
        "ok": not blocked and not min_req,
        "sold_out_nights": blocked,
        "units_left": units_left,
        "min_nights": min_req,
        "tracked": True,
    }


async def claim(booking: dict) -> bool:
    """
    Take the rooms for a paid booking. True when the stay is held.

    True is also returned when there is nothing to claim — no room type on the
    booking means the listing was not tracking inventory when it was made, and
    failing the payment over that would punish the traveller for our migration.
    """
    room_type_id = booking.get("room_type_id")
    rooms = booking.get("rooms") or 1
    ci, co = _as_date(booking.get("check_in")), _as_date(booking.get("check_out"))

    if not room_type_id or not ci or not co or co <= ci:
        return True
    if (co - ci).days > MAX_NIGHTS:
        logger.warning(
            "[availability] booking %s spans %s nights, over the claim cap",
            booking.get("id"),
            (co - ci).days,
        )
        return False

    try:
        res = await run_db(
            lambda: get_db()
            .rpc(
                "claim_stay_nights",
                {
                    "p_room_type": room_type_id,
                    "p_check_in": ci.isoformat(),
                    "p_check_out": co.isoformat(),
                    "p_units": rooms,
                },
            )
            .execute()
        )
        remaining = res.data if res else None
        # -1 is the documented "guard lost" return; anything non-negative held.
        return not (isinstance(remaining, (int, float)) and remaining < 0)
    except Exception:  # noqa: BLE001
        # Treated as a loss on purpose. Refunding a traveller we cannot confirm
        # is recoverable; handing out a room we may not have is not.
        logger.exception("[availability] claim_stay_nights failed for %s", booking.get("id"))
        return False


async def release(booking: dict) -> None:
    """
    Give the rooms back after a refund or a cancellation.

    Best-effort and silent: a release is cleanup on a path that has already
    decided its outcome, and must never be the thing that fails it.
    """
    room_type_id = booking.get("room_type_id")
    rooms = booking.get("rooms") or 1
    ci, co = _as_date(booking.get("check_in")), _as_date(booking.get("check_out"))

    if not room_type_id or not ci or not co or co <= ci:
        return

    try:
        await run_db(
            lambda: get_db()
            .rpc(
                "release_stay_nights",
                {
                    "p_room_type": room_type_id,
                    "p_check_in": ci.isoformat(),
                    "p_check_out": co.isoformat(),
                    "p_units": rooms,
                },
            )
            .execute()
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[availability] release_stay_nights failed for %s", booking.get("id")
        )
