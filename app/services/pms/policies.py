"""
The rules a guest is owed an answer to, resolved once.

Three questions arrive constantly on Instagram and WhatsApp and none of them had
a structured answer before this module:

    "cancel karne pe paisa wapas milega?"     cancellation
    "10 baje check-in kar sakta hoon?"        early arrival / late departure
    "breakfast included hai?"                 meals

Each was answerable only from prose — `rate_plans.cancellation_policy` was an
empty `jsonb` on all twenty-four plans in production, `properties.check_in_time`
is a single time with no notion of arriving before it, and `meal_plan` is a
two-letter code the guest has never heard of. So the agent guessed, hedged, or
handed the conversation to a person who was asleep.

**The same resolver answers the guest and the website.** §15 and §45 both ask
for that, and the reason is not tidiness: a hotel page that says "free
cancellation up to 7 days" while the AI says 48 hours is a dispute at the front
desk, and whichever one is wrong, the hotel loses.

Everything here is a pure function over rows. Nothing reads a database, so every
branch is testable without one, and the callers — an agent tool, a public site
projection, a refund calculation — cannot each grow their own interpretation.
"""
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)


# ── cancellation ─────────────────────────────────────────────────────────────
#
# The stored shape, on `rate_plans.cancellation_policy`, `group_policies.
# cancellation` and `experiences.cancellation` alike:
#
#     {
#       "tiers": [{"hoursBefore": 168, "refundPct": 100},
#                 {"hoursBefore": 48,  "refundPct": 50},
#                 {"hoursBefore": 0,   "refundPct": 0}],
#       "noShowPct": 0,
#       "modification": "allowed" | "once" | "not_allowed",
#       "note": "…"
#     }
#
# One shape for all three so one resolver reads them. `refundPct` is what the
# guest gets back, not what the hotel keeps — the two are trivially related and
# choosing the wrong one silently inverts a policy, so it is stated here and
# named in the field.

# What a plan gets when nothing has been configured: refundable until the plan's
# own cutoff, nothing after. This is what `refundable` and
# `cancellation_cutoff_hours` have always meant, expressed as a ladder so there
# is one code path rather than two.
DEFAULT_CUTOFF_HOURS = 24


@dataclass(frozen=True)
class RefundTier:
    hours_before: int
    refund_pct: int


@dataclass(frozen=True)
class CancellationTerms:
    """
    What happens if this booking is called off, and when.

    `tiers` is always sorted from the most generous (furthest out) to the least,
    which is the order a human reads them in and the order `refund_for` walks.
    """

    tiers: tuple[RefundTier, ...]
    no_show_pct: int
    modification: str
    note: str | None
    # True when nothing was configured and the plan's own two columns were used.
    # Surfaced so a readiness check can say "you have not set this" rather than
    # showing a hotelier a policy they never wrote.
    derived: bool

    @property
    def non_refundable(self) -> bool:
        return all(t.refund_pct == 0 for t in self.tiers)

    @property
    def free_until_hours(self) -> int | None:
        """The last point at which a full refund is still given, if there is one."""
        for tier in self.tiers:
            if tier.refund_pct >= 100:
                return tier.hours_before
        return None

    def refund_pct_at(self, hours_remaining: float) -> int:
        """
        The percentage refunded when cancelling this far ahead.

        Walks from the most generous tier down and takes the first one the guest
        still qualifies for. A cancellation *further* out than the most generous
        tier gets that tier, which is the only sane reading — a policy saying
        "100% up to 7 days" does not mean 8 days out is worse.
        """
        for tier in self.tiers:
            if hours_remaining >= tier.hours_before:
                return tier.refund_pct
        return self.tiers[-1].refund_pct if self.tiers else 0

    def as_dict(self) -> dict:
        return {
            "tiers": [
                {"hoursBefore": t.hours_before, "refundPct": t.refund_pct} for t in self.tiers
            ],
            "noShowPct": self.no_show_pct,
            "modification": self.modification,
            "note": self.note,
            "nonRefundable": self.non_refundable,
            "freeUntilHours": self.free_until_hours,
            "configured": not self.derived,
        }


def _int_in_range(value, low: int, high: int, fallback: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return fallback


def cancellation_terms(policy: dict | None, *, refundable: bool = True,
                       cutoff_hours: int | None = None) -> CancellationTerms:
    """
    Read a stored policy, falling back to the plan's own two columns.

    **Malformed input degrades to the fallback rather than raising.** A hotelier
    whose JSON somebody hand-edited should get their simple policy back, not a
    500 in the middle of a guest asking whether they can cancel. Every unreadable
    tier is dropped individually, so one bad row does not discard the good ones
    beside it.
    """
    raw_tiers = (policy or {}).get("tiers")
    tiers: list[RefundTier] = []
    if isinstance(raw_tiers, list):
        for entry in raw_tiers:
            if not isinstance(entry, dict):
                continue
            hours = entry.get("hoursBefore", entry.get("hours_before"))
            pct = entry.get("refundPct", entry.get("refund_pct"))
            if hours is None or pct is None:
                continue
            try:
                tiers.append(RefundTier(max(0, int(hours)), _int_in_range(pct, 0, 100, 0)))
            except (TypeError, ValueError):
                continue

    derived = not tiers
    if derived:
        # The two columns that have always existed, read as a ladder. A
        # non-refundable plan is one tier of zero; a refundable one is full
        # refund until the cutoff and nothing after.
        cutoff = cutoff_hours if cutoff_hours is not None else DEFAULT_CUTOFF_HOURS
        tiers = (
            [RefundTier(max(0, int(cutoff)), 100), RefundTier(0, 0)]
            if refundable else [RefundTier(0, 0)]
        )

    # Sorted here rather than trusted from storage. The resolution walk depends
    # on the order, and a hotelier entering their tiers out of order would
    # otherwise silently get a policy that is not the one on their screen.
    tiers.sort(key=lambda t: t.hours_before, reverse=True)
    # A ladder that does not reach zero leaves the last few hours undefined; a
    # guest cancelling then would fall off the end. Closed here rather than
    # required of the hotelier, whose mental model stops at "and after that,
    # nothing".
    if tiers[-1].hours_before > 0:
        tiers.append(RefundTier(0, 0))

    modification = (policy or {}).get("modification") or "allowed"
    if modification not in {"allowed", "once", "not_allowed"}:
        modification = "allowed"

    return CancellationTerms(
        tiers=tuple(tiers),
        no_show_pct=_int_in_range((policy or {}).get("noShowPct", (policy or {}).get("no_show_pct")), 0, 100, 0),
        modification=modification,
        note=(policy or {}).get("note") or None,
        derived=derived,
    )


def refund_for(
    terms: CancellationTerms,
    *,
    total_paise: int,
    check_in: date,
    check_in_time: time | None = None,
    at: datetime | None = None,
    no_show: bool = False,
) -> dict:
    """
    What this booking is worth back, cancelled now.

    Integer paise and one rounding, the same discipline as `price_stay`. The
    hours remaining are measured to the *arrival moment* — check-in date plus the
    hotel's check-in time — rather than to midnight, because a "24 hours before"
    policy on a 2pm check-in means 2pm the day before, and measuring to midnight
    would give away ten hours of it on every booking.

    Timezone: the arrival is built in UTC when the caller gives no better, which
    is wrong by five and a half hours for an Indian hotel and is *deliberately*
    wrong in the guest's favour — it makes the deadline slightly later, so nobody
    is refused a refund they believed they were entitled to because of a
    conversion this function was not given the information to do properly. The
    caller that has the property's timezone should pass an aware `at`.
    """
    now = at or datetime.now(timezone.utc)
    arrival = datetime.combine(check_in, check_in_time or time(14, 0), tzinfo=timezone.utc)
    hours_remaining = (arrival - now).total_seconds() / 3600

    pct = terms.no_show_pct if no_show else terms.refund_pct_at(hours_remaining)
    total = max(0, int(total_paise or 0))
    refund = round(total * pct / 100)
    return {
        "refundPct": pct,
        "refundPaise": refund,
        "retainedPaise": total - refund,
        "hoursRemaining": round(hours_remaining, 1),
        "noShow": no_show,
    }


# ── arriving early, leaving late ─────────────────────────────────────────────


@dataclass(frozen=True)
class TimeBand:
    kind: str
    until: time
    charge_kind: str
    amount: int
    subject_to_availability: bool
    note: str | None


@dataclass(frozen=True)
class TimeVerdict:
    """One answer to "can I arrive at 10?"."""

    kind: str
    available: bool
    free: bool
    charge_paise: int
    charge_kind: str
    subject_to_availability: bool
    note: str | None
    # The hotel's standard time, so the agent can say "our check-in is 2pm" in
    # the same breath rather than needing a second lookup.
    standard: time

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "available": self.available,
            "free": self.free,
            "chargePaise": self.charge_paise,
            "chargeKind": self.charge_kind,
            "subjectToAvailability": self.subject_to_availability,
            "note": self.note,
            "standardTime": self.standard.strftime("%H:%M"),
        }


def bands_from_rows(rows: list[dict]) -> list[TimeBand]:
    """`stay_time_rules` rows, cleaned and sorted earliest-first."""
    bands: list[TimeBand] = []
    for row in rows or []:
        if not row.get("is_active", True):
            continue
        parsed = _as_time(row.get("until_time"))
        if parsed is None:
            continue
        bands.append(
            TimeBand(
                kind=str(row.get("kind") or ""),
                until=parsed,
                charge_kind=str(row.get("charge_kind") or "free"),
                amount=max(0, int(row.get("amount") or 0)),
                subject_to_availability=bool(row.get("subject_to_availability", True)),
                note=row.get("note") or None,
            )
        )
    bands.sort(key=lambda b: b.until)
    return bands


def _as_time(value) -> time | None:
    if isinstance(value, time):
        return value
    if value is None:
        return None
    text = str(value)
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text[:8] if len(text) >= 8 else text, fmt).time()
        except ValueError:
            continue
    return None


def resolve_time_request(
    bands: list[TimeBand],
    *,
    kind: str,
    wanted: time,
    standard: time,
    nightly_paise: int,
) -> TimeVerdict:
    """
    What arriving at `wanted` costs, or leaving at it.

    **The standard time is always free and is never a band.** A hotel does not
    configure "2pm → free"; that is what check-in *means*. So arriving at or
    after the standard time — or leaving at or before it — short-circuits here,
    which also means a hotel with no bands configured at all still answers the
    ordinary question correctly instead of falling through to "unavailable".

    Bands are read in time order and the first one that still covers the request
    wins. For an early arrival that is the first band whose `until` is at or
    after the wanted time: an 8am band and a 10am band mean 9am is charged at the
    10am rate, because 9am is "before 10" and the 8am band has already passed.
    Late checkout reads the other way — the first band whose `until` is at or
    after the departure — which is the same sentence with the inequality flipped
    and is why both live in one function rather than two that drift.

    A request beyond every configured band is `available: False`. That is a real
    answer a hotel means: "we cannot hold your room until 11pm."
    """
    ours = [b for b in bands if b.kind == kind]

    if kind == "early_in" and wanted >= standard:
        return TimeVerdict(kind, True, True, 0, "free", False, None, standard)
    if kind == "late_out" and wanted <= standard:
        return TimeVerdict(kind, True, True, 0, "free", False, None, standard)

    for band in ours:
        if wanted <= band.until:
            if band.charge_kind == "unavailable":
                return TimeVerdict(
                    kind, False, False, 0, "unavailable", band.subject_to_availability,
                    band.note, standard,
                )
            charge = (
                0 if band.charge_kind == "free"
                else band.amount if band.charge_kind == "fixed"
                else round(max(0, nightly_paise) * band.amount / 100)
            )
            return TimeVerdict(
                kind=kind,
                available=True,
                free=charge == 0,
                charge_paise=charge,
                charge_kind=band.charge_kind,
                subject_to_availability=band.subject_to_availability,
                note=band.note,
                standard=standard,
            )

    # No band covers it. A hotel that has configured nothing gets `available:
    # False` too, and that is the honest answer — it has not said it can, and an
    # AI inventing "sure, 10am is fine" is exactly what this module exists to
    # stop.
    return TimeVerdict(kind, False, False, 0, "unavailable", False, None, standard)


# ── meals ────────────────────────────────────────────────────────────────────

# What each rate plan code includes. The vocabulary is the database's —
# `rate_plans.meal_plan` has enumerated these four since the schema was written —
# and this is the one place the codes are translated into meals.
#
# MAP is breakfast and dinner rather than breakfast and lunch. That is the Indian
# hotel convention and it is what every hotelier on this platform means by it;
# getting it wrong would have the AI confidently tell a guest their dinner is
# paid for when it is not.
MEAL_PLAN_INCLUDES: dict[str, frozenset[str]] = {
    "EP": frozenset(),
    "CP": frozenset({"breakfast"}),
    "MAP": frozenset({"breakfast", "dinner"}),
    "AP": frozenset({"breakfast", "lunch", "dinner"}),
}

MEALS = ("breakfast", "lunch", "dinner")


def meals_for_plan(meal_plan: str | None, options: list[dict]) -> list[dict]:
    """
    What each meal is, and whether this rate already covers it.

    **Inclusion is derived, never stored twice.** `meal_options` says what
    breakfast *is*; `meal_plan` says whether this particular rate includes it.
    Storing "included" on the meal row as well would let a CP plan and its
    breakfast row disagree about a booking somebody has already paid for.

    A meal a hotel has not described at all still appears, with
    `configured: false`. The agent needs to be able to say "I don't have our
    lunch timings written down" — which is true, useful, and a fixable gap on
    the readiness screen — rather than silently omitting lunch and leaving the
    guest to assume there is none.
    """
    included = MEAL_PLAN_INCLUDES.get((meal_plan or "EP").upper(), frozenset())
    by_meal = {str(r.get("meal")): r for r in options or []}

    out = []
    for meal in MEALS:
        row = by_meal.get(meal)
        availability = str(row.get("availability")) if row else None
        out.append(
            {
                "meal": meal,
                "configured": row is not None,
                "availability": availability or "unknown",
                # A meal the hotel does not serve cannot be included by a rate
                # plan, whatever the code says. The code is about money; this is
                # about whether there is food.
                "included": meal in included and availability != "not_available",
                "pricePaise": row.get("price_paise") if row else None,
                "priceUnit": row.get("price_unit") if row else None,
                "startsAt": str(row.get("starts_at"))[:5] if row and row.get("starts_at") else None,
                "endsAt": str(row.get("ends_at"))[:5] if row and row.get("ends_at") else None,
                "diet": row.get("diet") if row else None,
                "serviceStyle": row.get("service_style") if row else None,
                "cuisine": row.get("cuisine") if row else None,
                "note": row.get("note") if row else None,
            }
        )
    return out


# ── deposits ─────────────────────────────────────────────────────────────────


def deposit_for(
    total_paise: int,
    *,
    token_pct: int | None,
    group_deposit_pct: int | None = None,
    is_group: bool = False,
    rule: str = "percentage",
    fixed_paise: int | None = None,
) -> dict:
    """
    What a guest pays now to confirm, and what is left for check-in.

    §12 asks for four rules — full payment, a percentage, a fixed token, and pay
    at hotel — and StaySuite already had exactly one of them, `settings.token_pct`,
    which `price_stay` applies. So this adds the other three around it rather
    than replacing the one that works.

    A group booking uses the group deposit where the hotel has set one, because a
    twenty-room block is the case a hotel most wants a bigger commitment on and
    the reason `group_policies.deposit_pct` exists.
    """
    total = max(0, int(total_paise or 0))
    if rule == "pay_at_hotel":
        return {"rule": rule, "depositPaise": 0, "balancePaise": total}
    if rule == "full":
        return {"rule": rule, "depositPaise": total, "balancePaise": 0}
    if rule == "fixed":
        deposit = min(max(0, int(fixed_paise or 0)), total)
        return {"rule": rule, "depositPaise": deposit, "balancePaise": total - deposit}

    pct = group_deposit_pct if (is_group and group_deposit_pct is not None) else token_pct
    pct = _int_in_range(pct, 0, 100, 20)
    deposit = min(round(total * pct / 100), total)
    return {"rule": "percentage", "pct": pct, "depositPaise": deposit, "balancePaise": total - deposit}
