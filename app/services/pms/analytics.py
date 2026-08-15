"""
The hotel's numbers, over a period, computed exactly once.

`workspace.today_stats` answers "what does today look like" and has since M2.
This module answers the same question over a *range*, and everything the Owner
Copilot is allowed to say comes from here. There is no second implementation:
`today_stats` now asks this module for its money, so the Today screen and the
owner's dashboard cannot report different revenue for the same day.

    period ──► read ──► Snapshot ──► Metric[]  ──► the API, the tools, the model
                            │
                            └──► signals()  deterministic, threshold-driven

**Three rules, and they are why an AI may quote these numbers.**

*The model never computes one.* Every figure a tool hands to Gemini is produced
here, carries its own definition, and arrives with the period, the timezone and
the comparison it should be read against. A model given a bare `72.4` will
eventually explain it as something it is not.

*Room revenue comes from `reservation_nights.room_paise`.* That column is
written by `price_stay()` at booking, and migration 6 makes the invariant
arithmetic: Σ room_paise = gross − discount. Deriving revenue from the
reservation total divided by a night count is a second pricing implementation,
and it is the one that was wrong — see `_night_money` below.

*Business dates are the property's.* Nothing here reads a server date. A stay is
"in" a period because the hotel's own calendar says so, which for a Goa property
at 00:15 IST is a different answer from the UTC one.

**What these words mean**, pinned here because a hotelier will argue about them
and they must not drift between two screens:

    room-night sold   one unit occupied for one night
    capacity          Σ room_types.total_units × nights in the period
    occupancy         sold ÷ capacity
    ADR               room revenue ÷ room-nights sold      (net of discount,
                                                            before tax)
    RevPAR            room revenue ÷ capacity              (= ADR × occupancy)
    room revenue      Σ reservation_nights.room_paise for nights in the period
    service revenue   Σ folio 'addon' entries posted in the period
    total revenue     room + service. Tax is neither, and never in ADR.
    booking pace      room-nights *booked* in the period, by creation date —
                      how fast business is arriving, not how much is occupied
    ALOS              average nights per stay, over stays arriving in the period
    cancellation rate the share of stays *created* in the period that are now
                      cancelled or no-show. A cohort, so it cannot exceed 100%.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.db.client import get_db, run_db
from app.services.pms import clock
# Reservations in these states occupy a room and earn money. Imported rather
# than restated, so one edit changes both. The dependency runs one way only:
# `workspace` imports this module inside `today_stats`, not at module level, so
# there is no cycle.
from app.services.pms.workspace import LIVE_STATUSES

logger = logging.getLogger(__name__)

DEAD_STATUSES = ("cancelled", "no_show")

# How many reservations a single period read will look at. A 40-room hotel over
# 90 days is a few hundred; the ceiling exists so a malformed custom range
# cannot pull a year of a large group's bookings into one process.
MAX_RESERVATIONS = 2000
# PostgREST `in.()` goes into the URL. Chunked so a long id list cannot produce
# a request the edge refuses.
_ID_CHUNK = 120


# ── thresholds ───────────────────────────────────────────────────────────────
#
# Configuration, not prompt text. `signals()` reads them from
# `properties.settings.analytics_thresholds`, so a property whose weekends
# always swing 15 points can say so without anybody editing a system prompt —
# and a threshold that lives in a prompt is one a guest's message can argue
# with. Every value is stated in the unit its metric is measured in.

DEFAULT_THRESHOLDS: dict[str, float] = {
    # Percentage *points*, because occupancy is already a percentage.
    "occupancy_drop_pp": 10.0,
    "occupancy_surge_pp": 10.0,
    # Relative, because ADR is money.
    "adr_change_pct": 12.0,
    "revpar_change_pct": 15.0,
    "pace_change_pct": 20.0,
    "cancellation_rate_pct": 20.0,
    # A room type is underperforming when its occupancy sits this far below the
    # property's own.
    "room_type_gap_pp": 15.0,
    # Below these, nothing is said at all. Two bookings are not a trend, and an
    # agent that recommends a price change from two bookings is worse than one
    # that says it cannot tell.
    "min_sample_nights": 10.0,
    "min_sample_bookings": 5.0,
}


def thresholds_for(settings: dict | None) -> dict[str, float]:
    """Property overrides on top of the defaults. Malformed values are ignored."""
    out = dict(DEFAULT_THRESHOLDS)
    raw = (settings or {}).get("analytics_thresholds") or {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if key not in out:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            logger.info("ignoring malformed analytics threshold %s=%r", key, value)
    return out


# ── periods ──────────────────────────────────────────────────────────────────

PERIOD_KEYS = (
    "today", "yesterday", "last_7", "last_30", "this_week", "last_week",
    "this_month", "last_month", "next_7", "next_30", "custom",
)


@dataclass(frozen=True)
class Period:
    """
    A closed range of the property's own business dates, and its comparison.

    Both ends inclusive, because these are the nights a hotelier means. A stay
    occupies the night of its arrival date and does not occupy the night of its
    departure date, which is the half-open rule `reservation_nights` already
    encodes — a night row exists for each night slept.
    """

    key: str
    label: str
    start: date
    end: date
    timezone: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def previous(self) -> "Period":
        """
        The equivalent stretch immediately before this one.

        Same number of days, ending the day before this one starts. Not "the
        same dates last month" and not "last year": a like-for-like of equal
        length is the only comparison that is honest for every period key
        without a calendar argument, and it is the one a hotelier means by
        "compared with before".
        """
        length = self.days
        end = self.start - timedelta(days=1)
        return Period(
            key=f"previous_{self.key}",
            label=f"previous {length} day{'s' if length != 1 else ''}",
            start=end - timedelta(days=length - 1),
            end=end,
            timezone=self.timezone,
        )

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "days": self.days,
            "timezone": self.timezone,
        }


def resolve_period(
    *,
    today: date,
    timezone_name: str,
    key: str = "today",
    date_from: date | None = None,
    date_to: date | None = None,
) -> Period:
    """
    Turn a word into two dates on the property's calendar.

    `today` is the caller's — always `clock.business_date(property_id)`, never
    `date.today()`. That is the whole reason this takes it as an argument rather
    than reading a clock: a function that can reach for the server's date is a
    function that will, on the one path nobody tested at 00:30.

    Weeks start on Monday, which is what an Indian hotel's week does.
    """
    key = (key or "today").lower()

    if key == "custom":
        if not date_from or not date_to:
            raise ValueError("a custom period needs both from and to")
        start, end = min(date_from, date_to), max(date_from, date_to)
        # A range nobody meant. Refused rather than clamped: an owner who asked
        # for four years of data should be told, not quietly given three months.
        if (end - start).days > 400:
            raise ValueError("a custom period may not exceed 400 days")
        label = f"{start.isoformat()} → {end.isoformat()}"
        return Period(key="custom", label=label, start=start, end=end,
                      timezone=timezone_name)

    if key == "today":
        start = end = today
        label = "today"
    elif key == "yesterday":
        start = end = today - timedelta(days=1)
        label = "yesterday"
    elif key == "last_7":
        end, start, label = today, today - timedelta(days=6), "the last 7 days"
    elif key == "last_30":
        end, start, label = today, today - timedelta(days=29), "the last 30 days"
    elif key == "this_week":
        start = today - timedelta(days=today.weekday())
        end, label = today, "this week so far"
    elif key == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        end, label = this_monday - timedelta(days=1), "last week"
    elif key == "this_month":
        start = today.replace(day=1)
        end, label = today, "this month so far"
    elif key == "last_month":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start, label = end.replace(day=1), "last month"
    elif key == "next_7":
        start, end, label = today, today + timedelta(days=6), "the next 7 days"
    elif key == "next_30":
        start, end, label = today, today + timedelta(days=29), "the next 30 days"
    else:
        raise ValueError(f"unknown period {key!r}; one of {', '.join(PERIOD_KEYS)}")

    return Period(key=key, label=label, start=start, end=end, timezone=timezone_name)


def _utc_window(period: Period) -> tuple[str, str]:
    """
    The period as a half-open UTC instant range, for the timestamptz columns.

    `created_at`, `cancelled_at` and `posted_at` are instants, not dates. A
    booking taken at 23:40 IST on the 14th belongs to the 14th at this hotel and
    to the 14th's UTC *18:10*, and filtering those columns on a bare date is how
    the last five hours of every Indian day land in the wrong bucket.
    """
    tz = ZoneInfo(period.timezone) if period.timezone else ZoneInfo(clock.DEFAULT_TIMEZONE)
    start = datetime.combine(period.start, time.min, tzinfo=tz)
    end = datetime.combine(period.end + timedelta(days=1), time.min, tzinfo=tz)
    return start.isoformat(), end.isoformat()


# ── the shapes ───────────────────────────────────────────────────────────────

@dataclass
class Metric:
    """
    One number, and everything needed to read it correctly.

    `change` is always in the metric's own unit — percentage points for a
    percentage, paise for money, whole units for a count. `change_pct` is the
    relative move and is **None for a metric that is already a percentage**,
    because "occupancy is up 8%" and "occupancy is up 8 points" are different
    claims and only one of them is usually true.
    """

    key: str
    label: str
    value: float | int | None
    unit: str                       # percent · paise · count · nights · days
    basis: str
    currency: str | None = None
    previous: float | int | None = None
    change: float | None = None
    change_pct: float | None = None
    sample: int = 0
    sufficient: bool = True

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "currency": self.currency,
            "previous": self.previous,
            "change": self.change,
            "changePct": self.change_pct,
            "basis": self.basis,
            "sample": self.sample,
            "sufficient": self.sufficient,
        }


@dataclass
class Snapshot:
    """Everything one period's read produced. Raw, before it becomes Metrics."""

    period: Period
    property_id: str
    currency: str

    room_nights_sold: int = 0
    capacity_room_nights: int = 0
    room_revenue_paise: int = 0
    service_revenue_paise: int = 0
    tax_paise: int = 0

    # Pace: what was *booked* during the period, whenever it is for.
    bookings_made: int = 0
    room_nights_booked: int = 0
    booked_revenue_paise: int = 0
    bookings_cancelled_from_cohort: int = 0

    # Cancellations by the date they happened, for spike detection.
    cancellations_in_period: int = 0
    cancelled_value_paise: int = 0

    arrivals: int = 0
    departures: int = 0
    arrival_nights: int = 0

    by_room_type: dict[str, dict] = field(default_factory=dict)
    by_rate_plan: dict[str, dict] = field(default_factory=dict)
    by_source: dict[str, dict] = field(default_factory=dict)
    by_date: dict[str, dict] = field(default_factory=dict)
    services: dict[str, dict] = field(default_factory=dict)

    @property
    def total_revenue_paise(self) -> int:
        return self.room_revenue_paise + self.service_revenue_paise

    @property
    def occupancy_pct(self) -> float | None:
        if not self.capacity_room_nights:
            return None
        return self.room_nights_sold / self.capacity_room_nights * 100

    @property
    def adr_paise(self) -> int | None:
        if not self.room_nights_sold:
            return None
        return round(self.room_revenue_paise / self.room_nights_sold)

    @property
    def revpar_paise(self) -> int | None:
        if not self.capacity_room_nights:
            return None
        return round(self.room_revenue_paise / self.capacity_room_nights)

    @property
    def alos(self) -> float | None:
        if not self.arrivals:
            return None
        return self.arrival_nights / self.arrivals

    @property
    def cancellation_rate_pct(self) -> float | None:
        """
        Of the stays booked in this period, the share now dead.

        A cohort rather than "cancellations ÷ bookings", which mixes two
        populations and can exceed 100% on a quiet week — a number that
        embarrasses whoever shows it to an owner.
        """
        cohort = self.bookings_made
        if not cohort:
            return None
        return self.bookings_cancelled_from_cohort / cohort * 100


# ── reading ──────────────────────────────────────────────────────────────────

def _night_money(reservation: dict, nights: list[dict]) -> dict[str, int]:
    """
    What each night of this stay contributes, keyed by ISO date.

    `reservation_nights.room_paise` when it exists, which is every booking made
    through `create_reservation` since migration 6. The fallback is an even
    allocation of `gross − discount` across the stay — the same thing migration
    6's backfill did, and the same thing it is: an allocation of a number that
    already exists on the reservation, never a second pricing implementation.
    It exists for imported and hand-inserted stays, which otherwise appear as
    occupancy with no revenue.

    **`units` is not applied here.** `gross_paise` is Σ(tariff × units) — see
    `pricing.price_stay` — so it already covers every room on the booking, and
    `room_paise` is defined as "all units, after discount, before tax".
    Multiplying by units again is the bug this function exists to not have: a
    three-room booking reported three times its revenue and an ADR three times
    too high, on the screen a property prices next season from.
    """
    if nights:
        return {str(n["date"]): int(n.get("room_paise") or 0) for n in nights}

    span = int(reservation.get("nights") or 0)
    if span <= 0:
        return {}
    net = int(reservation.get("gross_paise") or 0) - int(reservation.get("discount_paise") or 0)
    check_in = _as_date(reservation["check_in"])
    per = net // span
    out: dict[str, int] = {}
    for i in range(span):
        # The last night absorbs the remainder, so the allocation sums exactly
        # to the stay rather than to the stay minus a few paise.
        amount = per if i < span - 1 else net - per * (span - 1)
        out[(check_in + timedelta(days=i)).isoformat()] = amount
    return out


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _chunks(values: list[str]):
    for i in range(0, len(values), _ID_CHUNK):
        yield values[i:i + _ID_CHUNK]


def _read_period(property_id: str, period: Period) -> dict:
    """One period's worth of rows. Blocking; called inside `run_db`."""
    db = get_db()
    start_s, end_s = period.start.isoformat(), period.end.isoformat()
    since, until = _utc_window(period)

    # Stays that touch the window. Half-open on the stay: a departure on the
    # first day of the period slept its last night before it.
    overlapping = (
        db.table("reservations")
        .select("id,status,check_in,check_out,nights,units,source,room_type_id,"
                "rate_plan_id,gross_paise,discount_paise,tax_paise,total_paise,"
                "created_at,cancelled_at,guest_name,ref")
        .eq("property_id", property_id)
        .gt("check_out", start_s)
        .lte("check_in", end_s)
        .limit(MAX_RESERVATIONS)
        .execute()
        .data
        or []
    )

    live_ids = [r["id"] for r in overlapping if r["status"] in LIVE_STATUSES]
    nights: list[dict] = []
    for chunk in _chunks(live_ids):
        nights += (
            db.table("reservation_nights")
            .select("reservation_id,date,room_paise,tax_paise,rate_paise")
            .in_("reservation_id", chunk)
            .gte("date", start_s)
            .lte("date", end_s)
            .limit(MAX_RESERVATIONS * 40)
            .execute()
            .data
            or []
        )

    # The pace cohort: booked during the window, whenever the stay is for.
    booked = (
        db.table("reservations")
        .select("id,status,nights,units,source,room_type_id,rate_plan_id,"
                "gross_paise,discount_paise,total_paise,created_at")
        .eq("property_id", property_id)
        .gte("created_at", since)
        .lt("created_at", until)
        .limit(MAX_RESERVATIONS)
        .execute()
        .data
        or []
    )

    cancelled = (
        db.table("reservations")
        .select("id,status,nights,units,total_paise,cancelled_at,source")
        .eq("property_id", property_id)
        .gte("cancelled_at", since)
        .lt("cancelled_at", until)
        .limit(MAX_RESERVATIONS)
        .execute()
        .data
        or []
    )

    addons = (
        db.table("folio_entries")
        .select("description,amount_paise,posted_at")
        .eq("property_id", property_id)
        .eq("kind", "addon")
        .gte("posted_at", since)
        .lt("posted_at", until)
        .limit(MAX_RESERVATIONS * 4)
        .execute()
        .data
        or []
    )

    room_types = (
        db.table("room_types")
        .select("id,code,name,total_units")
        .eq("property_id", property_id)
        .eq("is_active", True)
        .order("sort_order")
        .execute()
        .data
        or []
    )
    rate_plans = (
        db.table("rate_plans")
        .select("id,code,name")
        .eq("property_id", property_id)
        .execute()
        .data
        or []
    )
    prop = (
        db.table("properties")
        .select("name,currency,timezone,settings")
        .eq("id", property_id)
        .limit(1)
        .execute()
        .data
        or []
    )

    return {
        "overlapping": overlapping,
        "nights": nights,
        "booked": booked,
        "cancelled": cancelled,
        "addons": addons,
        "room_types": room_types,
        "rate_plans": rate_plans,
        "property": prop[0] if prop else {},
    }


def _build(property_id: str, period: Period, rows: dict) -> Snapshot:
    """Turn rows into a Snapshot. Pure arithmetic — no I/O, so it is testable."""
    prop = rows["property"]
    snap = Snapshot(
        period=period,
        property_id=property_id,
        currency=prop.get("currency") or "INR",
    )

    room_types = {rt["id"]: rt for rt in rows["room_types"]}
    rate_plans = {rp["id"]: rp for rp in rows["rate_plans"]}

    snap.capacity_room_nights = sum(
        int(rt.get("total_units") or 0) for rt in rows["room_types"]
    ) * period.days

    for rt in rows["room_types"]:
        snap.by_room_type[rt["id"]] = {
            "roomTypeId": rt["id"],
            "code": rt.get("code"),
            "name": rt.get("name"),
            "units": int(rt.get("total_units") or 0),
            "capacityRoomNights": int(rt.get("total_units") or 0) * period.days,
            "roomNightsSold": 0,
            "roomRevenuePaise": 0,
        }

    nights_by_reservation: dict[str, list[dict]] = {}
    for n in rows["nights"]:
        nights_by_reservation.setdefault(n["reservation_id"], []).append(n)

    for r in rows["overlapping"]:
        check_in, check_out = _as_date(r["check_in"]), _as_date(r["check_out"])
        if r["status"] in LIVE_STATUSES:
            if period.contains(check_in):
                snap.arrivals += 1
                snap.arrival_nights += int(r.get("nights") or 0)
            if period.contains(check_out):
                snap.departures += 1

        if r["status"] not in LIVE_STATUSES:
            continue

        units = int(r.get("units") or 1)
        money = _night_money(r, nights_by_reservation.get(r["id"], []))
        tax_by_date = {
            str(n["date"]): int(n.get("tax_paise") or 0)
            for n in nights_by_reservation.get(r["id"], [])
        }

        day = check_in
        while day < check_out:
            if period.contains(day):
                iso = day.isoformat()
                amount = money.get(iso, 0)

                snap.room_nights_sold += units
                snap.room_revenue_paise += amount
                snap.tax_paise += tax_by_date.get(iso, 0)

                slot = snap.by_date.setdefault(iso, {
                    "date": iso, "roomNightsSold": 0, "roomRevenuePaise": 0,
                })
                slot["roomNightsSold"] += units
                slot["roomRevenuePaise"] += amount

                rt = snap.by_room_type.get(r["room_type_id"])
                if rt is None:
                    # A stay on a room type that has since been deactivated. It
                    # still happened and still earned; it simply has no capacity
                    # line, which is stated rather than hidden.
                    rt = snap.by_room_type.setdefault(r["room_type_id"], {
                        "roomTypeId": r["room_type_id"],
                        "code": (room_types.get(r["room_type_id"]) or {}).get("code"),
                        "name": (room_types.get(r["room_type_id"]) or {}).get("name")
                                or "Retired room type",
                        "units": 0,
                        "capacityRoomNights": 0,
                        "roomNightsSold": 0,
                        "roomRevenuePaise": 0,
                    })
                rt["roomNightsSold"] += units
                rt["roomRevenuePaise"] += amount

                plan = snap.by_rate_plan.setdefault(r["rate_plan_id"], {
                    "ratePlanId": r["rate_plan_id"],
                    "code": (rate_plans.get(r["rate_plan_id"]) or {}).get("code"),
                    "name": (rate_plans.get(r["rate_plan_id"]) or {}).get("name")
                            or "Unknown rate plan",
                    "roomNightsSold": 0,
                    "roomRevenuePaise": 0,
                })
                plan["roomNightsSold"] += units
                plan["roomRevenuePaise"] += amount

                src = snap.by_source.setdefault(r["source"], {
                    "source": r["source"],
                    "roomNightsSold": 0,
                    "roomRevenuePaise": 0,
                    "bookings": 0,
                })
                src["roomNightsSold"] += units
                src["roomRevenuePaise"] += amount
            day += timedelta(days=1)

    # Bookings are counted on the stays that *touch* the period, so "12 bookings
    # from Booking.com" means twelve distinct reservations rather than twelve
    # nights.
    for r in rows["overlapping"]:
        if r["status"] in LIVE_STATUSES and r["source"] in snap.by_source:
            snap.by_source[r["source"]]["bookings"] += 1

    for r in rows["booked"]:
        snap.bookings_made += 1
        snap.room_nights_booked += int(r.get("nights") or 0) * int(r.get("units") or 1)
        snap.booked_revenue_paise += int(r.get("total_paise") or 0)
        if r["status"] in DEAD_STATUSES:
            snap.bookings_cancelled_from_cohort += 1

    for r in rows["cancelled"]:
        snap.cancellations_in_period += 1
        snap.cancelled_value_paise += int(r.get("total_paise") or 0)

    for e in rows["addons"]:
        amount = int(e.get("amount_paise") or 0)
        snap.service_revenue_paise += amount
        name = (e.get("description") or "Other").strip()[:60]
        slot = snap.services.setdefault(name, {"name": name, "count": 0, "revenuePaise": 0})
        slot["count"] += 1
        slot["revenuePaise"] += amount

    return snap


async def snapshot(property_id: str, period: Period) -> Snapshot:
    rows = await run_db(_read_period, property_id, period)
    return _build(property_id, period, rows)


async def period_for(
    property_id: str,
    *,
    key: str = "today",
    date_from: date | None = None,
    date_to: date | None = None,
) -> Period:
    """The named period on this property's calendar. The only way one is made."""
    today = await clock.abusiness_date(property_id)
    tz = await clock.atimezone_for(property_id)
    return resolve_period(
        today=today, timezone_name=tz, key=key, date_from=date_from, date_to=date_to
    )


# ── metrics ──────────────────────────────────────────────────────────────────

def _metric(
    key: str, label: str, unit: str, basis: str,
    value, previous, *, currency: str | None = None,
    sample: int = 0, min_sample: float = 0.0,
) -> Metric:
    """
    One metric, with its comparison worked out in the right unit.

    A percentage metric reports `change` in points and no `change_pct` — the
    difference between "occupancy is up 8 points" and "occupancy is up 8%",
    which are different claims about the same pair of numbers and are confused
    in every hotel dashboard that reports only one of them.
    """
    change = change_pct = None
    if value is not None and previous is not None:
        change = round(float(value) - float(previous), 4)
        if unit != "percent" and previous:
            change_pct = round((float(value) - float(previous)) / abs(float(previous)) * 100, 2)
    return Metric(
        key=key, label=label, value=value, unit=unit, basis=basis,
        currency=currency, previous=previous, change=change, change_pct=change_pct,
        sample=sample, sufficient=sample >= min_sample,
    )


def metrics_from(current: Snapshot, previous: Snapshot | None,
                 thresholds: dict[str, float] | None = None) -> list[Metric]:
    """The authoritative metric list, in the order an owner reads them."""
    th = thresholds or DEFAULT_THRESHOLDS
    prev = previous
    cur_currency = current.currency
    min_nights = th.get("min_sample_nights", 0)
    min_bookings = th.get("min_sample_bookings", 0)

    def p(attr: str):
        return getattr(prev, attr) if prev is not None else None

    occ = current.occupancy_pct
    return [
        _metric("occupancy", "Occupancy", "percent",
                "room-nights sold ÷ (active units × nights in the period)",
                None if occ is None else round(occ, 1),
                None if p("occupancy_pct") is None else round(p("occupancy_pct"), 1),
                sample=current.room_nights_sold, min_sample=min_nights),
        _metric("adr", "ADR", "paise",
                "room revenue ÷ room-nights sold, net of discount and before tax",
                current.adr_paise, p("adr_paise"), currency=cur_currency,
                sample=current.room_nights_sold, min_sample=min_nights),
        _metric("revpar", "RevPAR", "paise",
                "room revenue ÷ capacity room-nights, before tax",
                current.revpar_paise, p("revpar_paise"), currency=cur_currency,
                sample=current.room_nights_sold, min_sample=min_nights),
        _metric("room_revenue", "Room revenue", "paise",
                "Σ reservation_nights.room_paise for nights in the period",
                current.room_revenue_paise, p("room_revenue_paise"),
                currency=cur_currency,
                sample=current.room_nights_sold, min_sample=min_nights),
        _metric("service_revenue", "Service revenue", "paise",
                "folio add-on entries posted in the period",
                current.service_revenue_paise, p("service_revenue_paise"),
                currency=cur_currency,
                sample=len(current.services), min_sample=0),
        _metric("total_revenue", "Total revenue", "paise",
                "room revenue + service revenue, excluding tax",
                current.total_revenue_paise,
                None if prev is None else prev.total_revenue_paise,
                currency=cur_currency,
                sample=current.room_nights_sold, min_sample=min_nights),
        _metric("rooms_sold", "Room-nights sold", "nights",
                "one unit occupied for one night",
                current.room_nights_sold, p("room_nights_sold"),
                sample=current.room_nights_sold, min_sample=0),
        _metric("capacity", "Capacity", "nights",
                "active units × nights in the period",
                current.capacity_room_nights, p("capacity_room_nights"),
                sample=current.capacity_room_nights, min_sample=0),
        _metric("booking_pace", "Booking pace", "nights",
                "room-nights booked during the period, by the date the booking "
                "was taken — arriving business, not occupied business",
                current.room_nights_booked, p("room_nights_booked"),
                sample=current.bookings_made, min_sample=min_bookings),
        _metric("bookings_made", "Bookings taken", "count",
                "reservations created during the period, on the property's clock",
                current.bookings_made, p("bookings_made"),
                sample=current.bookings_made, min_sample=0),
        _metric("cancellation_rate", "Cancellation rate", "percent",
                "share of the bookings taken in this period that are now "
                "cancelled or no-show",
                None if current.cancellation_rate_pct is None
                else round(current.cancellation_rate_pct, 1),
                None if p("cancellation_rate_pct") is None
                else round(p("cancellation_rate_pct"), 1),
                sample=current.bookings_made, min_sample=min_bookings),
        _metric("cancellations", "Cancellations", "count",
                "reservations cancelled during the period, by the date they "
                "were cancelled",
                current.cancellations_in_period, p("cancellations_in_period"),
                sample=current.cancellations_in_period, min_sample=0),
        _metric("alos", "Average length of stay", "days",
                "nights ÷ stays, over stays arriving in the period",
                None if current.alos is None else round(current.alos, 2),
                None if p("alos") is None else round(p("alos"), 2),
                sample=current.arrivals, min_sample=min_bookings),
        _metric("arrivals", "Arrivals", "count",
                "stays checking in during the period",
                current.arrivals, p("arrivals"), sample=current.arrivals),
        _metric("departures", "Departures", "count",
                "stays checking out during the period",
                current.departures, p("departures"), sample=current.departures),
    ]


def breakdowns_from(current: Snapshot, previous: Snapshot | None = None) -> dict:
    """
    The three cuts an owner asks for, each ranked by revenue.

    Room-type occupancy is computed against that type's own capacity, which is
    the only way "the deluxe rooms are the problem" is a statement rather than
    an impression — a type with four units and a type with twenty produce very
    different night counts at identical occupancy.
    """
    def with_derived(row: dict) -> dict:
        cap = row.get("capacityRoomNights") or 0
        sold = row.get("roomNightsSold") or 0
        revenue = row.get("roomRevenuePaise") or 0
        return {
            **row,
            "occupancyPct": round(sold / cap * 100, 1) if cap else None,
            "adrPaise": round(revenue / sold) if sold else None,
            "revparPaise": round(revenue / cap) if cap else None,
        }

    prev_types = (previous.by_room_type if previous else {}) or {}
    room_types = []
    for rid, row in current.by_room_type.items():
        enriched = with_derived(row)
        was = prev_types.get(rid)
        if was:
            prev_sold = was.get("roomNightsSold") or 0
            prev_cap = was.get("capacityRoomNights") or 0
            enriched["previousOccupancyPct"] = (
                round(prev_sold / prev_cap * 100, 1) if prev_cap else None
            )
            enriched["previousRoomRevenuePaise"] = was.get("roomRevenuePaise")
        room_types.append(enriched)

    total_revenue = current.room_revenue_paise or 0
    sources = []
    for row in current.by_source.values():
        sources.append({
            **row,
            "sharePct": (round(row["roomRevenuePaise"] / total_revenue * 100, 1)
                         if total_revenue else None),
            "adrPaise": (round(row["roomRevenuePaise"] / row["roomNightsSold"])
                         if row["roomNightsSold"] else None),
        })

    rate_plans = [
        {
            **row,
            "adrPaise": (round(row["roomRevenuePaise"] / row["roomNightsSold"])
                         if row["roomNightsSold"] else None),
        }
        for row in current.by_rate_plan.values()
    ]

    return {
        "roomTypes": sorted(room_types, key=lambda r: -(r.get("roomRevenuePaise") or 0)),
        "ratePlans": sorted(rate_plans, key=lambda r: -(r.get("roomRevenuePaise") or 0)),
        "sources": sorted(sources, key=lambda r: -(r.get("roomRevenuePaise") or 0)),
        "services": sorted(current.services.values(),
                           key=lambda r: -(r.get("revenuePaise") or 0)),
        "daily": [current.by_date[k] for k in sorted(current.by_date)],
    }


# ── signals ──────────────────────────────────────────────────────────────────
#
# Deterministic, threshold-driven, and computed here rather than noticed by a
# model. Every signal names the metric, the observed value, what it is being
# compared with, and the threshold it crossed — which is what makes a
# recommendation built on it checkable rather than merely plausible.

@dataclass
class Signal:
    kind: str
    severity: str        # info · watch · act
    headline: str
    metric: str
    observed: float | int | None
    comparison: float | int | None
    threshold: float
    unit: str
    detail: str = ""
    subject: dict | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "headline": self.headline,
            "metric": self.metric,
            "observed": self.observed,
            "comparison": self.comparison,
            "threshold": self.threshold,
            "unit": self.unit,
            "detail": self.detail,
            "subject": self.subject,
        }


SIGNAL_KINDS = (
    "pricing_opportunity", "low_occupancy", "high_demand",
    "room_type_underperformance", "cancellation_spike", "booking_pace_change",
    "channel_performance", "revenue_anomaly", "insufficient_data",
)


def signals_from(
    current: Snapshot,
    previous: Snapshot | None,
    thresholds: dict[str, float],
) -> list[Signal]:
    """
    What deterministically stands out. No model, no ML, no heuristics that
    cannot be printed as a sentence with a number in it.

    Returns a single `insufficient_data` signal when the period is too thin to
    say anything, which is the honest answer far more often than a dashboard
    admits: two bookings is not a demand pattern.
    """
    out: list[Signal] = []
    occ = current.occupancy_pct
    min_nights = thresholds["min_sample_nights"]

    if current.room_nights_sold < min_nights:
        return [Signal(
            kind="insufficient_data", severity="info",
            headline=f"Only {current.room_nights_sold} room-nights in this period",
            metric="rooms_sold", observed=current.room_nights_sold,
            comparison=None, threshold=min_nights, unit="nights",
            detail=(
                f"Nothing can be concluded about pricing or demand from "
                f"{current.room_nights_sold} room-nights. The threshold is "
                f"{min_nights:.0f}."
            ),
        )]

    prev_occ = previous.occupancy_pct if previous else None
    if occ is not None and prev_occ is not None:
        move = occ - prev_occ
        if move <= -thresholds["occupancy_drop_pp"]:
            out.append(Signal(
                kind="low_occupancy", severity="act",
                headline=f"Occupancy is {abs(move):.1f} points below the comparable period",
                metric="occupancy", observed=round(occ, 1),
                comparison=round(prev_occ, 1),
                threshold=thresholds["occupancy_drop_pp"], unit="percentage points",
                detail=(
                    f"{current.room_nights_sold} of {current.capacity_room_nights} "
                    f"room-nights sold, against {previous.room_nights_sold} of "
                    f"{previous.capacity_room_nights} in {previous.period.label}."
                ),
            ))
        elif move >= thresholds["occupancy_surge_pp"]:
            out.append(Signal(
                kind="high_demand", severity="watch",
                headline=f"Occupancy is {move:.1f} points above the comparable period",
                metric="occupancy", observed=round(occ, 1),
                comparison=round(prev_occ, 1),
                threshold=thresholds["occupancy_surge_pp"], unit="percentage points",
                detail=(
                    f"{current.room_nights_sold} of {current.capacity_room_nights} "
                    f"room-nights sold, against {previous.room_nights_sold} of "
                    f"{previous.capacity_room_nights} in {previous.period.label}."
                ),
            ))

    # High occupancy on its own is a pricing question whether or not it moved.
    if occ is not None and occ >= 85:
        out.append(Signal(
            kind="pricing_opportunity", severity="watch",
            headline=f"Occupancy is running at {occ:.1f}%",
            metric="occupancy", observed=round(occ, 1),
            comparison=None if prev_occ is None else round(prev_occ, 1),
            threshold=85.0, unit="percent",
            detail=(
                f"{current.capacity_room_nights - current.room_nights_sold} "
                f"room-nights remain unsold out of {current.capacity_room_nights}. "
                f"ADR over the period is {current.adr_paise} paise."
            ),
        ))

    if previous and current.adr_paise and previous.adr_paise:
        move_pct = (current.adr_paise - previous.adr_paise) / previous.adr_paise * 100
        if abs(move_pct) >= thresholds["adr_change_pct"]:
            out.append(Signal(
                kind="revenue_anomaly", severity="watch",
                headline=(
                    f"ADR is {abs(move_pct):.1f}% "
                    f"{'above' if move_pct > 0 else 'below'} the comparable period"
                ),
                metric="adr", observed=current.adr_paise,
                comparison=previous.adr_paise,
                threshold=thresholds["adr_change_pct"], unit="percent",
                detail=(
                    f"{current.adr_paise} paise against {previous.adr_paise} paise "
                    f"in {previous.period.label}."
                ),
            ))

    if previous and previous.room_nights_booked and \
            current.bookings_made >= thresholds["min_sample_bookings"]:
        move_pct = ((current.room_nights_booked - previous.room_nights_booked)
                    / previous.room_nights_booked * 100)
        if abs(move_pct) >= thresholds["pace_change_pct"]:
            out.append(Signal(
                kind="booking_pace_change", severity="act" if move_pct < 0 else "watch",
                headline=(
                    f"Booking pace is {abs(move_pct):.1f}% "
                    f"{'ahead of' if move_pct > 0 else 'behind'} the comparable period"
                ),
                metric="booking_pace", observed=current.room_nights_booked,
                comparison=previous.room_nights_booked,
                threshold=thresholds["pace_change_pct"], unit="percent",
                detail=(
                    f"{current.room_nights_booked} room-nights booked in "
                    f"{current.period.label} against {previous.room_nights_booked} "
                    f"in {previous.period.label}."
                ),
            ))

    rate = current.cancellation_rate_pct
    if rate is not None and current.bookings_made >= thresholds["min_sample_bookings"] \
            and rate >= thresholds["cancellation_rate_pct"]:
        out.append(Signal(
            kind="cancellation_spike", severity="act",
            headline=f"{rate:.1f}% of bookings taken in this period have cancelled",
            metric="cancellation_rate", observed=round(rate, 1),
            comparison=(None if not previous or previous.cancellation_rate_pct is None
                        else round(previous.cancellation_rate_pct, 1)),
            threshold=thresholds["cancellation_rate_pct"], unit="percent",
            detail=(
                f"{current.bookings_cancelled_from_cohort} of "
                f"{current.bookings_made} bookings taken in {current.period.label}."
            ),
        ))

    if occ is not None:
        for row in current.by_room_type.values():
            cap = row.get("capacityRoomNights") or 0
            if not cap:
                continue
            rt_occ = (row.get("roomNightsSold") or 0) / cap * 100
            gap = occ - rt_occ
            if gap >= thresholds["room_type_gap_pp"]:
                out.append(Signal(
                    kind="room_type_underperformance", severity="watch",
                    headline=(
                        f"{row.get('name')} is {gap:.1f} points below the "
                        f"property's occupancy"
                    ),
                    metric="occupancy", observed=round(rt_occ, 1),
                    comparison=round(occ, 1),
                    threshold=thresholds["room_type_gap_pp"], unit="percentage points",
                    detail=(
                        f"{row.get('roomNightsSold')} of {cap} room-nights sold "
                        f"across {row.get('units')} units."
                    ),
                    subject={"roomTypeId": row.get("roomTypeId"), "name": row.get("name")},
                ))

    # Channel concentration. Not a threshold anybody set — it is arithmetic
    # about where the business comes from, and it is 'info' because a hotel that
    # sells 70% through one OTA may have chosen to.
    total = current.room_revenue_paise
    if total:
        top = max(current.by_source.values(), key=lambda r: r["roomRevenuePaise"], default=None)
        if top and top["roomRevenuePaise"] / total >= 0.6:
            share = top["roomRevenuePaise"] / total * 100
            out.append(Signal(
                kind="channel_performance", severity="info",
                headline=f"{share:.0f}% of room revenue came through {top['source']}",
                metric="room_revenue", observed=top["roomRevenuePaise"],
                comparison=total, threshold=60.0, unit="percent",
                detail=(
                    f"{top['roomNightsSold']} room-nights across "
                    f"{top['bookings']} bookings."
                ),
                subject={"source": top["source"]},
            ))

    return out


# ── the one call everything else makes ───────────────────────────────────────

async def report(
    property_id: str,
    *,
    key: str = "today",
    date_from: date | None = None,
    date_to: date | None = None,
    compare: bool = True,
) -> dict:
    """
    One period, its comparison, its metrics, its breakdowns and its signals.

    This is what the API returns, what the tools return, and therefore what the
    model is shown — one shape, so a number on the Reports page and a number in
    an owner's answer are the same number read from the same place.
    """
    period = await period_for(property_id, key=key, date_from=date_from, date_to=date_to)
    current = await snapshot(property_id, period)
    previous = await snapshot(property_id, period.previous) if compare else None

    settings = await run_db(_property_settings, property_id)
    th = thresholds_for(settings)

    return {
        "propertyId": property_id,
        "period": period.as_dict(),
        "comparisonPeriod": period.previous.as_dict() if previous else None,
        "currency": current.currency,
        "metrics": [m.as_dict() for m in metrics_from(current, previous, th)],
        "breakdowns": breakdowns_from(current, previous),
        "signals": [s.as_dict() for s in signals_from(current, previous, th)],
        "thresholds": th,
        "generatedAt": datetime.now(ZoneInfo("UTC")).isoformat(),
    }


def _property_settings(property_id: str) -> dict:
    rows = (
        get_db().table("properties").select("settings")
        .eq("id", property_id).limit(1).execute().data or []
    )
    return (rows[0].get("settings") if rows else None) or {}


async def day_series(property_id: str, days: int = 30) -> list[dict]:
    """
    Occupancy, ADR and RevPAR per night for the last N nights, ending today.

    What the Reports chart draws. Derived from the same snapshot as everything
    else rather than from its own query, so the chart and the headline figures
    cannot disagree — which they did for as long as the chart came from the
    mock seed.
    """
    today = await clock.abusiness_date(property_id)
    tz = await clock.atimezone_for(property_id)
    span = max(1, min(int(days), 365))
    period = resolve_period(
        today=today, timezone_name=tz, key="custom",
        date_from=today - timedelta(days=span - 1), date_to=today,
    )
    snap = await snapshot(property_id, period)

    units = sum(
        int(r.get("units") or 0) for r in snap.by_room_type.values()
    )
    out = []
    day = period.start
    while day <= period.end:
        iso = day.isoformat()
        row = snap.by_date.get(iso, {"roomNightsSold": 0, "roomRevenuePaise": 0})
        sold = row["roomNightsSold"]
        revenue = row["roomRevenuePaise"]
        out.append({
            "date": iso,
            "roomsSold": sold,
            "roomsAvailable": units,
            "occupancyPct": round(sold / units * 100, 1) if units else 0.0,
            "adrPaise": round(revenue / sold) if sold else 0,
            "revparPaise": round(revenue / units) if units else 0,
            "revenuePaise": revenue,
        })
        day += timedelta(days=1)
    return out


def _read_day(property_id: str, period: Period) -> dict:
    """
    The three queries a single day's room money needs, in `_read_period`'s shape.

    `today_stats` is the screen a receptionist reloads all shift, so it does not
    pay for the pace cohort, the cancellation cohort, the add-on ledger or the
    rate-plan names — none of which it displays. The arithmetic is still
    `_build`'s, unchanged, which is the part that had to stay shared: a second
    revenue calculation for the fast path is exactly the bug this refactor
    removed.
    """
    db = get_db()
    day_s = period.start.isoformat()

    overlapping = (
        db.table("reservations")
        .select("id,status,check_in,check_out,nights,units,source,room_type_id,"
                "rate_plan_id,gross_paise,discount_paise,tax_paise,total_paise,"
                "created_at,cancelled_at,guest_name,ref")
        .eq("property_id", property_id)
        .gt("check_out", day_s)
        .lte("check_in", day_s)
        .in_("status", list(LIVE_STATUSES))
        .limit(MAX_RESERVATIONS)
        .execute()
        .data
        or []
    )

    nights: list[dict] = []
    for chunk in _chunks([r["id"] for r in overlapping]):
        nights += (
            db.table("reservation_nights")
            .select("reservation_id,date,room_paise,tax_paise,rate_paise")
            .in_("reservation_id", chunk)
            .eq("date", day_s)
            .limit(MAX_RESERVATIONS)
            .execute()
            .data
            or []
        )

    room_types = (
        db.table("room_types")
        .select("id,code,name,total_units")
        .eq("property_id", property_id)
        .eq("is_active", True)
        .execute()
        .data
        or []
    )

    return {
        "overlapping": overlapping,
        "nights": nights,
        "booked": [],
        "cancelled": [],
        "addons": [],
        "room_types": room_types,
        "rate_plans": [],
        "property": {},
    }


async def day_money(property_id: str, business_date: date) -> dict:
    """
    One business date's room money, for `workspace.today_stats`.

    Exists so the Today screen and the owner's dashboard cannot report different
    revenue for the same day. `today_stats` used to compute this itself, with
    `units` applied twice — `gross_paise` is already Σ(tariff × units), so a
    three-room booking reported three times its revenue and an ADR three times
    too high. That number is what a property prices its next season on.
    """
    tz = await clock.atimezone_for(property_id)
    period = Period(key="today", label="today", start=business_date,
                    end=business_date, timezone=tz)
    rows = await run_db(_read_day, property_id, period)
    snap = _build(property_id, period, rows)
    return {
        "roomsSold": snap.room_nights_sold,
        "roomsAvailable": sum(
            int(r.get("units") or 0) for r in snap.by_room_type.values()
        ),
        "roomRevenuePaise": snap.room_revenue_paise,
        "adrPaise": snap.adr_paise or 0,
        "revparPaise": snap.revpar_paise or 0,
        "occupancyPct": snap.occupancy_pct or 0.0,
    }
