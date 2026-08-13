"""
The property clock.

Every business date in the PMS is the *hotel's* date, not the server's. A folio
posted at 00:30 IST belongs to the day that has just started; a UTC server
files it under yesterday, and the arrivals list, the night audit and the
sync window all inherit that mistake.

`properties.timezone` is the authority. This module is the only place a UTC
instant becomes a calendar date, mirroring `property_business_date()` in
20260814_hotel_os_9_property_clock.sql — the SQL copy is the one the night-audit
sweep uses, and this one exists so an API handler can answer without a round
trip on every request.

**Cached, deliberately.** A property's timezone changes approximately never, and
the alternative is a database read on the path of every arrivals query. The TTL
is what bounds how long a correction takes to be believed; it is short enough
that nobody notices and long enough that the read is free.
"""
import logging
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.db.client import get_db, run_db

logger = logging.getLogger(__name__)

# The fallback when a property cannot be read or carries a timezone this
# machine's tz database does not know. India, because that is where every
# property is; stated here rather than defaulted to UTC so the failure mode is
# "an hour or two off for a hotel abroad" rather than "five and a half hours off
# for all of them".
DEFAULT_TIMEZONE = "Asia/Kolkata"

_TTL_SECONDS = 300
_cache: dict[str, tuple[float, str]] = {}


def _load_timezone(property_id: str) -> str:
    db = get_db()
    row = (
        db.table("properties")
        .select("timezone")
        .eq("id", property_id)
        .limit(1)
        .execute()
    )
    if not row.data:
        return DEFAULT_TIMEZONE
    return row.data[0].get("timezone") or DEFAULT_TIMEZONE


def timezone_for(property_id: str) -> str:
    """
    The property's IANA timezone. Blocking; call inside a `run_db` closure or
    use `atimezone_for` from async code.
    """
    hit = _cache.get(property_id)
    now = time.monotonic()
    if hit and now - hit[0] < _TTL_SECONDS:
        return hit[1]

    try:
        tz = _load_timezone(property_id)
    except Exception:
        # A hotel does not stop being able to check guests in because the
        # timezone lookup failed. Serve the stale value if there is one.
        logger.warning("timezone lookup failed for property %s", property_id, exc_info=True)
        return hit[1] if hit else DEFAULT_TIMEZONE

    _cache[property_id] = (now, tz)
    return tz


async def atimezone_for(property_id: str) -> str:
    return await run_db(timezone_for, property_id)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A migration-9 trigger refuses invalid names at write time, so this is
        # reachable only when Postgres knows a zone that this machine's tzdata
        # does not. Degrade rather than 500 the front desk.
        logger.warning("unknown timezone %r, falling back to %s", name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def business_date_in(tz_name: str, at: datetime | None = None) -> date:
    """The calendar date on that timezone's wall clock. The whole conversion."""
    moment = at or datetime.now(tz=ZoneInfo("UTC"))
    return moment.astimezone(zone(tz_name)).date()


def business_date(property_id: str) -> date:
    """Today, at this hotel. Blocking — see `timezone_for`."""
    return business_date_in(timezone_for(property_id))


async def abusiness_date(property_id: str) -> date:
    return await run_db(business_date, property_id)


def business_date_iso(property_id: str) -> str:
    return business_date(property_id).isoformat()


async def abusiness_date_iso(property_id: str) -> str:
    return (await abusiness_date(property_id)).isoformat()


def audit_date_for(property_id: str) -> date:
    """
    The business day the night audit closes when run now.

    The night that has just ended, which is *yesterday* on the property's clock:
    at 01:00 on the 15th the audit is closing the 14th. Same arithmetic as
    `run_night_audit_due()`, and the reason both exist is that a manager can
    press the button at 15:00 and must get the same answer as the 01:05 sweep.
    """
    return business_date(property_id) - timedelta(days=1)


def clear_cache() -> None:
    """For tests, and for whatever eventually edits a property's timezone."""
    _cache.clear()
