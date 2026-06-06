"""
Constraint Solver — validates the merged trip plan for budget, timing, and
logistics conflicts before the response is returned to the client.

Three checks:
  1. Budget: total cost vs ceiling for the requested tier
  2. Arrival timing: recommended transport arrival vs Day 1 itinerary start
  3. Hotel check-in: arrival time vs standard 2 PM check-in
  4. Return journey: earliest return departure vs last-day activities

Result is attached as merged["trip_validation"] — non-blocking (never raises,
never strips data). Issues are things the frontend should highlight in red;
warnings are amber; adjustments are suggested copy to show the user.
"""
import logging
import re
from datetime import datetime, timedelta

from app.api.v1.plan_trip.helpers import CURRENCY_CONFIG

logger = logging.getLogger(__name__)

_HOTEL_CHECKIN_HOUR = 14   # 2:00 PM
_LATE_ARRIVAL_HOUR  = 22   # 10:00 PM — Day 1 itinerary is wasted
_EVE_ARRIVAL_HOUR   = 20   # 8:00 PM  — Day 1 evening is tight


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_arrival(item: dict | None) -> datetime | None:
    """Extract arrival datetime from a recommended bus, flight, or train."""
    if not item:
        return None

    # Bus: "YYYY-MM-DD HH:MM:SS"
    s = item.get("arrival") or ""
    if s:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                pass

    # Flight: departure_time + duration_min
    dep_str = item.get("departure_time") or ""
    dur_min = item.get("duration_min")
    if dep_str and dur_min:
        try:
            dep = datetime.fromisoformat(dep_str.replace("Z", ""))
            return dep + timedelta(minutes=int(dur_min))
        except Exception:
            pass

    # Train: departure + duration_min (same structure as flight via ConfirmTKT)
    dep_str = item.get("departure") or ""
    dur_min = item.get("duration_min")
    if dep_str and dur_min:
        try:
            dep = datetime.strptime(dep_str, "%Y-%m-%d %H:%M:%S")
            arr = dep + timedelta(minutes=int(dur_min))
            return arr
        except Exception:
            pass

    return None


def _recommended_going_item(merged: dict) -> tuple[str | None, dict | None]:
    """Return (mode, item) for the recommended going transport."""
    going = merged.get("going") or {}

    rec_bus = (going.get("buses") or {}).get("recommended")
    if rec_bus:
        return "bus", rec_bus

    rec_flight = (going.get("flights") or {}).get("recommended")
    if rec_flight:
        return "flight", rec_flight

    rec_train = (going.get("trains") or {}).get("recommended")
    if rec_train:
        return "train", rec_train

    return None, None


def _earliest_return_departure(merged: dict) -> datetime | None:
    ret = merged.get("return") or {}
    flights = (ret.get("flights") or {}).get("items") or []
    buses   = (ret.get("buses")   or {}).get("items") or []

    candidates: list[datetime] = []
    for f in flights:
        s = f.get("departure_time") or ""
        try:
            candidates.append(datetime.fromisoformat(s.replace("Z", "")))
        except Exception:
            pass
    for b in buses:
        s = b.get("departure") or ""
        try:
            candidates.append(datetime.strptime(s, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass

    return min(candidates) if candidates else None


# ── Main validator ─────────────────────────────────────────────────────────────

def validate_trip(
    merged: dict,
    budget: str,
    currency_code: str,
    travelers: int,
) -> dict:
    """
    Run all constraint checks and attach trip_validation to merged.
    Always returns merged (never raises).
    """
    issues: list[str]      = []
    warnings: list[str]    = []
    adjustments: list[str] = []

    ci = CURRENCY_CONFIG.get(currency_code) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]
    safe_budget = budget if budget in ("low", "mid", "high") else "mid"
    ceiling_pp = ci["ceiling"].get(safe_budget, ci["ceiling"]["mid"])
    total_ceiling = ceiling_pp * travelers

    # ── 1. Budget ─────────────────────────────────────────────────────────────
    total = (merged.get("budget_breakdown") or {}).get("total") or 0
    if total > total_ceiling * 1.5:
        issues.append(
            f"Trip cost {symbol}{total:,} is {int((total/total_ceiling-1)*100)}% over "
            f"{safe_budget} budget ceiling ({symbol}{total_ceiling:,} for {travelers} traveler"
            f"{'s' if travelers > 1 else ''}). "
            f"Consider switching to a cheaper transport mode or reducing hotel tier."
        )
    elif total > total_ceiling:
        warnings.append(
            f"Trip cost {symbol}{total:,} slightly exceeds {safe_budget} budget ceiling "
            f"({symbol}{total_ceiling:,}). Budget status block has suggestions."
        )

    # ── 2. Arrival timing vs Day 1 itinerary ─────────────────────────────────
    mode, rec_item = _recommended_going_item(merged)
    arrival_dt = _parse_arrival(rec_item)

    days = merged.get("days") or []
    day1_acts = (days[0] if days else {}).get("activities") or []
    # Activities use start_time (HH:MM 24h) set by the time engine
    day1_start_str = (day1_acts[0].get("start_time") or day1_acts[0].get("time")) if day1_acts else None
    day1_first_time = day1_start_str or "09:00"

    if arrival_dt:
        h = arrival_dt.hour

        if h >= _LATE_ARRIVAL_HOUR:
            issues.append(
                f"Transport arrives at {arrival_dt.strftime('%H:%M')} — "
                f"Day 1 itinerary (starting {day1_first_time}) cannot happen on arrival day."
            )
            adjustments.append(
                "Day 1 activities should be moved to Day 2. "
                "Book accommodation near arrival point for the first night."
            )

        elif h >= _EVE_ARRIVAL_HOUR:
            warnings.append(
                f"Late arrival at {arrival_dt.strftime('%H:%M')} — "
                f"Day 1 evening activities may be rushed."
            )
            adjustments.append(
                "Consider starting the Day 1 itinerary next morning. "
                "A dinner near your accommodation is a good first-night plan."
            )

        # ── 2a. First activity must be >= arrival + 120 min ───────────────────
        if day1_start_str and h < _LATE_ARRIVAL_HOUR:
            try:
                parts = day1_start_str.split(":")
                act_h, act_m = int(parts[0]), int(parts[1])
                first_act_dt = arrival_dt.replace(hour=act_h, minute=act_m, second=0, microsecond=0)
                buffer_dt = arrival_dt + timedelta(minutes=120)
                if first_act_dt < arrival_dt:
                    issues.append(
                        f"Impossible schedule: Day 1 first activity at {day1_start_str} is before "
                        f"transport arrives at {arrival_dt.strftime('%H:%M')}."
                    )
                    adjustments.append(
                        f"Reschedule Day 1 to start after {buffer_dt.strftime('%H:%M')} "
                        f"(arrival + 2 h check-in buffer)."
                    )
                elif first_act_dt < buffer_dt:
                    gap_min = int((first_act_dt - arrival_dt).total_seconds() // 60)
                    warnings.append(
                        f"Day 1 starts at {day1_start_str}, only {gap_min} min after arrival at "
                        f"{arrival_dt.strftime('%H:%M')} — very tight for check-in."
                    )
                    adjustments.append(
                        f"Consider starting Day 1 at {buffer_dt.strftime('%H:%M')} or later "
                        f"to allow time to check in and settle in."
                    )
            except Exception:
                pass

        # ── 3. Hotel check-in timing ──────────────────────────────────────────
        if h < _HOTEL_CHECKIN_HOUR:
            if h < 8:
                warnings.append(
                    f"Very early arrival at {arrival_dt.strftime('%H:%M')} — "
                    f"hotel check-in is typically at 14:00."
                )
            adjustments.append(
                f"Arrive at {arrival_dt.strftime('%H:%M')}: request early check-in, "
                f"or drop luggage at the hotel and start Day 1 activities immediately."
            )

    # ── 4. Return departure vs last-day activities ─────────────────────────────
    ret_dep = _earliest_return_departure(merged)
    if ret_dep and days:
        last_day_acts = (days[-1] if days else {}).get("activities") or []
        if last_day_acts and ret_dep.hour < 10:
            warnings.append(
                f"Earliest return departure at {ret_dep.strftime('%I:%M %p')} — "
                f"last-day activities starting at "
                f"{last_day_acts[0].get('time', '09:00 AM')} may not be feasible."
            )
            adjustments.append(
                "Plan a light last day: breakfast near accommodation, then head to departure point."
            )

    result = {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "adjustments": adjustments,
        "checks_run": ["budget", "arrival_timing", "hotel_checkin", "return_timing"],
    }

    if issues:
        logger.warning(
            "[constraints] %s — %d issue(s): %s",
            merged.get("destination", "?"), len(issues), "; ".join(issues[:2]),
        )

    merged["trip_validation"] = result
    return merged
