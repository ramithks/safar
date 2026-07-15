"""
Feature functions — pure, 0..1 (or ``None`` when a signal is absent).

Every ranking feature is a small deterministic function so it's testable, cheap, and
debuggable. They reuse the primitives already proven in ``app.services.scoring``
(``_minmax``, ``_log_scale``, ``_f``) rather than reinventing them. ``None`` means
"we don't know this signal for this item" and is dropped + renormalised by the
ranker — NOT scored 0 (that would bury a thin-provider listing). A real ``0.0``
(e.g. no free cancellation) is a known-low value and counts.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.scoring import _f, _minmax, _log_scale


# ── Set-level stats (a feature like "cheap" is only meaningful vs the peer set) ──
@dataclass
class SetStats:
    lo_price: float = 0.0
    hi_price: float = 0.0
    cheapest: float | None = None
    hi_dist: float = 0.0
    lo_dur: float = 0.0
    hi_dur: float = 0.0

    @classmethod
    def for_hotels(cls, hotels: list[dict]) -> "SetStats":
        prices = [p for p in (_hotel_price(h) for h in hotels) if p and p > 0]
        dists = [d for d in (_f(h.get("distance_km")) for h in hotels) if d is not None]
        return cls(
            lo_price=min(prices) if prices else 0.0,
            hi_price=max(prices) if prices else 0.0,
            cheapest=min(prices) if prices else None,
            hi_dist=max(dists) if dists else 0.0,
        )

    @classmethod
    def for_transport(cls, items: list[dict]) -> "SetStats":
        prices = [p for p in (_transport_price(t) for t in items) if p and p > 0]
        durs = [d for d in (_f(t.get("duration_min")) for t in items) if d and d > 0]
        return cls(
            lo_price=min(prices) if prices else 0.0,
            hi_price=max(prices) if prices else 0.0,
            cheapest=min(prices) if prices else None,
            lo_dur=min(durs) if durs else 0.0,
            hi_dur=max(durs) if durs else 0.0,
        )


def _hotel_price(h: dict) -> float | None:
    return _f(h.get("price_per_night")) or _f(h.get("estimated_price_per_night"))


def _transport_price(t: dict) -> float | None:
    return _f(t.get("price_total")) or _f(t.get("price")) or _f(t.get("fare"))


def _hotel_rating(h: dict) -> float | None:
    score = _f(h.get("review_score")) or _f(h.get("ta_rating")) or _f(h.get("google_rating"))
    if score is not None:
        return score
    star = _f(h.get("star_rating"))
    return star * 2 if star is not None else None


def _hour(ts: str | None) -> int | None:
    raw = (ts or "").strip()
    if not raw:
        return None
    if "T" in raw:
        raw = raw.split("T", 1)[1]
    elif " " in raw:
        raw = raw.split(" ", 1)[1]
    try:
        h = int(raw.split(":", 1)[0])
        return h if 0 <= h <= 23 else None
    except ValueError:
        return None


# ── Semantic + personality (shared) ─────────────────────────────────────────────
def cosine01(a, b) -> float | None:
    """Cosine of two equal-length vectors rescaled to 0..1. None if unavailable."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return None
    return max(0.0, min(1.0, (dot / (na * nb) + 1.0) / 2.0))


def _tag_fit(item_tags, wanted) -> float | None:
    """Jaccard-ish overlap of an item's personality tags with the traveller's
    wanted vibes/purpose tags. None when the item has no tags (unknown)."""
    it = {str(t).lower() for t in (item_tags or [])}
    if not it:
        return None
    wt = {str(t).lower() for t in (wanted or [])}
    if not wt:
        return 0.5  # item has personality but we have no target → neutral
    inter = len(it & wt)
    return min(1.0, inter / max(1, len(wt)))


# ── Hotel components ────────────────────────────────────────────────────────────
def hotel_components(h: dict, stats: SetStats, ctx: dict) -> dict[str, float | None]:
    price = _hotel_price(h)
    rating = _hotel_rating(h)
    dist = _f(h.get("distance_km"))
    sub = h.get("subscores") or {}
    budget = (ctx or {}).get("budget")

    return {
        "price": _budget_fit(price, stats, budget),
        "rating": _review_confidence(rating, _f(h.get("review_count"))),
        "location": _minmax(dist, 0.0, stats.hi_dist, invert=True) if dist is not None else None,
        "personality": _tag_fit(h.get("personality_tags"), (ctx or {}).get("vibes")),
        "flexibility": _flexibility(h, ctx),
        "comfort": _hotel_comfort(h, sub),
        "deal": _deal_quality(h),
        "semantic": cosine01(h.get("_embedding"), (ctx or {}).get("profile_embedding")),
    }


def _budget_fit(price: float | None, stats: SetStats, budget: str | None) -> float | None:
    if price is None or price <= 0:
        return None
    if stats.hi_price <= stats.lo_price:
        return 0.6
    if budget == "low":
        return _minmax(price, stats.lo_price, stats.hi_price, invert=True)
    if budget == "high":
        return _minmax(price, stats.lo_price, stats.hi_price)  # pricier = better fit
    # mid / unknown → closeness to the set median band, cheaper-leaning
    span = stats.hi_price - stats.lo_price
    mid = stats.lo_price + span * 0.4
    return max(0.0, 1.0 - abs(price - mid) / (span or 1.0))


def _review_confidence(rating: float | None, count: float | None) -> float | None:
    if rating is None:
        return None
    conf = _log_scale(count, cap=500) if count else 0.35  # thin evidence → shrink
    return (rating / 10.0) * (0.55 + 0.45 * conf)


def _flexibility(h: dict, ctx: dict) -> float:
    flex = bool(h.get("free_cancellation"))
    noprep = bool(h.get("no_prepayment") or h.get("pay_at_hotel"))
    base = 0.7 * flex + 0.3 * noprep
    risk = (ctx or {}).get("risk_tolerance")
    if risk is not None:
        try:  # risk-averse traveller (low tolerance) values flexibility more
            base = min(1.0, base * (1.0 + 0.5 * (0.5 - float(risk))))
        except (TypeError, ValueError):
            pass
    return base


def _hotel_comfort(h: dict, sub: dict) -> float | None:
    for k in ("facilities", "cleanliness", "comfort"):
        v = _f(sub.get(k))
        if v is not None:
            return min(1.0, v / 10.0)
    star = _f(h.get("star_rating"))
    return min(1.0, star / 5.0) if star is not None else None


def _deal_quality(h: dict) -> float | None:
    pct = _f(h.get("discount_pct"))
    save = _f(h.get("savings"))
    if pct is None and save is None:
        return None
    p = min(1.0, (pct or 0) / 40.0)
    s = min(1.0, (save or 0) / 2000.0)
    return round(0.6 * p + 0.4 * s, 4)


# ── Transport components ─────────────────────────────────────────────────────────
def transport_components(t: dict, stats: SetStats, ctx: dict) -> dict[str, float | None]:
    price = _transport_price(t)
    dur = _f(t.get("duration_min"))
    stops = int(t.get("stops") or 0)
    baggage = _f(t.get("baggage_kg"))
    dep_h = _hour(t.get("departure_time"))
    arr_h = _hour(t.get("arrival_time"))
    return {
        "price": _budget_fit(price, stats, (ctx or {}).get("budget")),
        "duration": _minmax(dur, stats.lo_dur, stats.hi_dur, invert=True) if dur else None,
        "stops": 1.0 if stops == 0 else max(0.0, 1.0 - 0.35 * stops),
        "arrival": _arrival_quality(arr_h),
        "departure": _daypart_fit(dep_h, ctx),
        "baggage": _minmax(baggage, 0.0, 20.0) if baggage else None,
        "flexibility": _transport_flex(t),
        "comfort": _transport_comfort(t),
        "personality": _tag_fit(t.get("personality_tags"), (ctx or {}).get("vibes")),
    }


def _arrival_quality(hour: int | None) -> float | None:
    if hour is None:
        return None
    if 8 <= hour <= 18:
        return 1.0          # arrives in daylight — usable day, easy transfer
    if 6 <= hour < 8 or 18 < hour <= 21:
        return 0.7
    return 0.25             # red-eye: burns a hotel night / hard transfer


def _daypart_fit(hour: int | None, ctx: dict) -> float | None:
    if hour is None:
        return None
    pref = ((ctx or {}).get("time_prefs") or {}).get("depart_daypart")
    ideal = {"early": 7, "morning": 9, "afternoon": 14, "evening": 19, "night": 22}.get(pref, 9)
    return max(0.0, 1.0 - abs(hour - ideal) / 12.0)


def _transport_flex(t: dict) -> float | None:
    if t.get("is_refundable") is True or (t.get("fare_type") or "").lower() in ("flexi", "flex", "refundable"):
        return 1.0
    if t.get("is_refundable") is False:
        return 0.0
    return None


def _transport_comfort(t: dict) -> float | None:
    mode = t.get("mode") or t.get("type")
    cabin = (t.get("cabin_class") or t.get("class") or "").upper()
    if mode == "flight" or t.get("airline"):
        return {"ECONOMY": 0.4, "PREMIUM_ECONOMY": 0.6, "BUSINESS": 0.85, "FIRST": 1.0}.get(cabin, 0.4)
    if mode == "train":
        return {"1A": 1.0, "2A": 0.8, "3A": 0.6, "CC": 0.6, "SL": 0.4, "2S": 0.25}.get(cabin)
    if mode == "bus":
        s = (t.get("class") or "").lower()
        if "sleeper" in s:
            return 0.7
        if "seater" in s or "semi" in s:
            return 0.45
        return None
    return None
