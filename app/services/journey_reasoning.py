"""
Journey reasoning — the "Reason" step between Score and Recommend.

Turns the scored fastest/cheapest/balanced options into human trade-off
sentences a competitor can't trivially copy, e.g.:

    "The flight is ₹1,200 cheaper but lands at 02:10 — you'd likely pay for an
     extra hotel night. The faster option costs ₹800 more and arrives by noon,
     saving that night."

Deterministic (no extra LLM call → no added latency or cost). Reads only the
option blocks produced by ``_build_journey_options`` plus the live hotel nightly
rate, and emits a ``reasoning`` block: a headline, an ordered list of insights,
and a per-option ``why`` one-liner.
"""
from __future__ import annotations

# A trip that lands in this window usually can't make a same-day check-in /
# onward connection and effectively burns a hotel night.
_LATE_NIGHT_START = 22   # 22:00
_LATE_NIGHT_END = 6      # 06:00
_DAYTIME_START = 8
_DAYTIME_END = 16


def _arrival_hour(option: dict) -> int | None:
    """Best-effort hour-of-day (0..23) from an option's arrival timestamp."""
    raw = (option.get("arrival_time") or "").strip()
    if not raw:
        return None
    # ISO 'YYYY-MM-DDTHH:MM[:SS]'  or  'YYYY-MM-DD HH:MM:SS'
    if "T" in raw:
        raw = raw.split("T", 1)[1]
    elif " " in raw and ":" in raw.split(" ", 1)[1]:
        raw = raw.split(" ", 1)[1]
    # now expect 'HH:MM...'
    head = raw.split(":", 1)[0]
    try:
        hour = int(head)
    except ValueError:
        return None
    return hour if 0 <= hour <= 23 else None


def _is_late_arrival(hour: int | None) -> bool:
    return hour is not None and (hour >= _LATE_NIGHT_START or hour < _LATE_NIGHT_END)


def _is_daytime_arrival(hour: int | None) -> bool:
    return hour is not None and _DAYTIME_START <= hour <= _DAYTIME_END


def _money(symbol: str, amount: float | int | None) -> str:
    if not amount:
        return f"{symbol}0"
    return f"{symbol}{int(round(amount)):,}"


def _fmt_hour(hour: int | None) -> str:
    if hour is None:
        return "an unknown time"
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12} {suffix}"


def _dur_display(minutes: int | None) -> str:
    if not minutes or minutes >= 999_999:
        return "unknown duration"
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if m else f"{h}h"


def build_journey_reasoning(
    options: dict,
    *,
    symbol: str = "₹",
    hotel_per_night: int | None = None,
) -> dict | None:
    """
    Build a reasoning block from the fastest/cheapest/balanced options.

    Returns None when there is nothing to compare (fewer than the option set, or
    cheapest and fastest are the same option).
    """
    cheapest = options.get("cheapest")
    fastest = options.get("fastest")
    balanced = options.get("balanced")
    if not cheapest or not fastest:
        return None

    insights: list[str] = []

    cheap_cost = cheapest.get("cost_per_person") or 0
    fast_cost = fastest.get("cost_per_person") or 0
    cheap_dur = cheapest.get("duration_min") or 0
    fast_dur = fastest.get("duration_min") or 0
    price_delta = fast_cost - cheap_cost          # extra you pay to go fast
    time_saved = cheap_dur - fast_dur             # minutes the fast option saves

    cheap_arr = _arrival_hour(cheapest)
    fast_arr = _arrival_hour(fastest)

    same_option = (
        cheapest.get("mode") == fastest.get("mode")
        and cheapest.get("description") == fastest.get("description")
    )

    if same_option:
        # One option is both cheapest and fastest — no trade-off to weigh.
        insights.append(
            f"The {cheapest.get('mode', 'best')} option is both the cheapest "
            f"({_money(symbol, cheap_cost)}/person) and the fastest "
            f"({_dur_display(cheap_dur)}) — no trade-off to make."
        )
        headline = "One clear winner on this route."
    else:
        # Core price-vs-time sentence.
        if price_delta > 0 and time_saved > 0:
            insights.append(
                f"The {fastest.get('mode', 'fast')} option costs "
                f"{_money(symbol, price_delta)} more per person but saves "
                f"{_dur_display(time_saved)} over the cheapest "
                f"{cheapest.get('mode', '')} option."
            )
        elif price_delta > 0:
            insights.append(
                f"The {fastest.get('mode', 'fastest')} option is "
                f"{_money(symbol, price_delta)} more per person; the "
                f"{cheapest.get('mode', 'cheapest')} option is the better value."
            )

        # Hotel-night insight — the differentiator.
        if _is_late_arrival(cheap_arr) and _is_daytime_arrival(fast_arr):
            extra = (
                f" — likely an extra {_money(symbol, hotel_per_night)} hotel night"
                if hotel_per_night else ""
            )
            insights.append(
                f"The cheapest option lands around {_fmt_hour(cheap_arr)}{extra}. "
                f"The faster option arrives by {_fmt_hour(fast_arr)}, saving that night."
            )
            if hotel_per_night and price_delta < hotel_per_night:
                insights.append(
                    f"Paying {_money(symbol, price_delta)} more to arrive in daylight "
                    f"is cheaper than the {_money(symbol, hotel_per_night)} night the "
                    f"late arrival would cost — the faster option likely wins overall."
                )
        elif _is_late_arrival(fast_arr):
            insights.append(
                f"Note: even the faster option arrives around {_fmt_hour(fast_arr)} — "
                f"plan a late check-in."
            )

        headline = (
            "Cheapest saves money; fastest saves a hotel night — weigh both."
            if _is_late_arrival(cheap_arr) and _is_daytime_arrival(fast_arr)
            else "Trade money for time on this route."
        )

    # Per-option one-liners.
    def _why(opt: dict | None, kind: str) -> str | None:
        if not opt:
            return None
        arr = _arrival_hour(opt)
        arr_note = f", arrives ~{_fmt_hour(arr)}" if arr is not None else ""
        base = (
            f"{_money(symbol, opt.get('cost_per_person'))}/person, "
            f"{_dur_display(opt.get('duration_min'))}{arr_note}"
        )
        tag = {
            "cheapest": "Lowest out-of-pocket cost",
            "fastest": "Least time on the road",
            "balanced": "Best overall value",
        }[kind]
        return f"{tag} — {base}."

    return {
        "headline": headline,
        "insights": insights,
        "why": {
            "cheapest": _why(cheapest, "cheapest"),
            "fastest": _why(fastest, "fastest"),
            "balanced": _why(balanced, "balanced"),
        },
    }
