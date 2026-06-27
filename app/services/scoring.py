"""
Weighted ranking + quality scoring for hotels and flights.

Two scores are attached to every result:

- ``rank_score`` (0..100): a *contextual* composite, normalised within the
  current result set (a ₹2000 hotel is "cheap" only relative to its peers). Used
  to order the merged list — replaces the old merge→price→done ordering.

- ``quality_score`` (0..100): an *absolute* signal of how rich/trustworthy the
  listing is (photos, reviews, amenities, provider, availability, description).
  Independent of price/rank, so it stays meaningful when fed to the AI
  recommendation layer later.

All weights live as module-level dicts so they can be tuned without touching the
math. Each component is normalised to 0..1, multiplied by its weight, summed,
then scaled to 0..100. Weights per family sum to 1.0.
"""
from __future__ import annotations

import math

# ── Provider trust (0..1) — confidence we have in a source's data quality ──────
# Booking/Agoda expose the richest structured data + live prices; EMT/Ixigo are
# solid but thinner. Gotogate is a flight meta-search.
_HOTEL_PROVIDER_CONFIDENCE = {
    "booking": 1.0, "agoda": 0.95, "easemytrip": 0.85, "ixigo": 0.8,
}
_FLIGHT_PROVIDER_CONFIDENCE = {
    "booking": 1.0, "easemytrip": 0.9, "gotogate": 0.85,
}
_DEFAULT_CONFIDENCE = 0.7

# ── Weights ────────────────────────────────────────────────────────────────────
HOTEL_RANK_WEIGHTS = {
    "price": 0.25,
    "rating": 0.20,
    "reviews": 0.10,
    "distance": 0.15,
    "provider": 0.10,
    "images": 0.08,
    "amenities": 0.07,
    "freshness": 0.05,
}
HOTEL_QUALITY_WEIGHTS = {
    "photos": 0.22,
    "reviews": 0.22,
    "amenities": 0.18,
    "provider": 0.15,
    "availability": 0.13,
    "description": 0.10,
}
FLIGHT_RANK_WEIGHTS = {
    "price": 0.40,
    "duration": 0.30,
    "stops": 0.15,
    "provider": 0.10,
    "departure": 0.05,
}
FLIGHT_QUALITY_WEIGHTS = {
    "price": 0.30,
    "schedule": 0.25,
    "directness": 0.20,
    "provider": 0.15,
    "seats": 0.10,
}


# ── Generic helpers ─────────────────────────────────────────────────────────────
def _f(v) -> float | None:
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _minmax(value: float | None, lo: float, hi: float, *, invert: bool = False) -> float:
    """Scale value into 0..1 against [lo, hi]. invert=True → lower is better."""
    if value is None or hi <= lo:
        return 0.5  # neutral when we can't tell
    frac = (value - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    return 1.0 - frac if invert else frac


def _log_scale(count: float | None, cap: float) -> float:
    """0..1 saturating curve — good for review counts / photo counts."""
    if not count or count <= 0:
        return 0.0
    return min(1.0, math.log1p(count) / math.log1p(cap))


def _weighted(components: dict[str, float], weights: dict[str, float]) -> float:
    """Sum component*weight → scale to 0..100. Missing components count as 0."""
    total = sum(components.get(k, 0.0) * w for k, w in weights.items())
    return round(100.0 * total, 1)


# ── Hotels ───────────────────────────────────────────────────────────────────
def _hotel_price(h: dict) -> float | None:
    return _f(h.get("price_per_night")) or _f(h.get("estimated_price_per_night"))


def _hotel_rating(h: dict) -> float | None:
    """Normalise any available rating to a 0..10 scale."""
    score = _f(h.get("review_score")) or _f(h.get("google_rating"))
    if score is not None:
        return score
    star = _f(h.get("star_rating"))
    return star * 2 if star is not None else None  # 5★ → 10


def _hotel_available(h: dict) -> bool:
    return not h.get("is_sold_out", False)


def score_hotels(hotels: list[dict]) -> list[dict]:
    """
    Attach ``quality_score`` + ``rank_score`` to each hotel and return the list
    ordered by rank_score (best first). Mutates the dicts in place.
    """
    if not hotels:
        return hotels

    prices = [p for p in (_hotel_price(h) for h in hotels) if p and p > 0]
    lo_p, hi_p = (min(prices), max(prices)) if prices else (0.0, 0.0)
    dists = [d for d in (_f(h.get("distance_km")) for h in hotels) if d is not None]
    hi_d = max(dists) if dists else 0.0

    for h in hotels:
        price = _hotel_price(h)
        rating = _hotel_rating(h)
        reviews = _f(h.get("review_count")) or 0
        photos = len(h.get("photos") or [])
        amenities = len(h.get("amenities") or [])
        dist = _f(h.get("distance_km"))
        confidence = _HOTEL_PROVIDER_CONFIDENCE.get(h.get("source"), _DEFAULT_CONFIDENCE)
        has_desc = bool(h.get("description") or h.get("review_label"))

        # Freshness proxy: a live price + flexible cancellation + in-stock signals
        # a current, bookable listing (we have no scrape timestamp per hotel).
        freshness = (
            0.5 * (price is not None)
            + 0.3 * bool(h.get("free_cancellation"))
            + 0.2 * _hotel_available(h)
        )

        rank = _weighted(
            {
                "price": _minmax(price, lo_p, hi_p, invert=True) if price else 0.3,
                "rating": (rating / 10.0) if rating is not None else 0.4,
                "reviews": _log_scale(reviews, cap=2000),
                "distance": _minmax(dist, 0.0, hi_d, invert=True) if dist is not None else 0.5,
                "provider": confidence,
                "images": _log_scale(photos, cap=6),
                "amenities": _log_scale(amenities, cap=15),
                "freshness": freshness,
            },
            HOTEL_RANK_WEIGHTS,
        )
        quality = _weighted(
            {
                "photos": _log_scale(photos, cap=6),
                "reviews": 0.6 * _log_scale(reviews, cap=2000) + 0.4 * ((rating / 10.0) if rating else 0.0),
                "amenities": _log_scale(amenities, cap=15),
                "provider": confidence,
                "availability": (0.6 * _hotel_available(h)) + (0.4 * (price is not None)),
                "description": 1.0 if has_desc else 0.0,
            },
            HOTEL_QUALITY_WEIGHTS,
        )
        h["quality_score"] = quality
        h["rank_score"] = rank

    hotels.sort(key=lambda x: x.get("rank_score", 0.0), reverse=True)
    return hotels


# ── Flights ──────────────────────────────────────────────────────────────────
def _dep_convenience(flight: dict) -> float:
    """0..1 — favours a civilised departure (peaks ~9 AM, sinks for red-eyes)."""
    raw = (flight.get("departure_time") or "").strip()
    hour: int | None = None
    if "T" in raw:  # ISO 2026-06-15T08:30:00
        try:
            hour = int(raw.split("T")[1][:2])
        except (ValueError, IndexError):
            hour = None
    elif ":" in raw:  # HH:MM
        try:
            hour = int(raw.split(":")[0])
        except ValueError:
            hour = None
    if hour is None:
        return 0.5
    ideal = 9.0
    return max(0.0, 1.0 - abs(hour - ideal) / 12.0)


def score_flights(flights: list[dict]) -> list[dict]:
    """
    Attach ``quality_score`` + ``rank_score`` to each flight. Does NOT reorder —
    the caller decides whether to sort by rank_score or by an explicit user sort
    (CHEAPEST/FASTEST). Mutates the dicts in place.
    """
    if not flights:
        return flights

    prices = [p for p in (_f(f.get("price_total")) for f in flights) if p and p > 0]
    lo_p, hi_p = (min(prices), max(prices)) if prices else (0.0, 0.0)
    durs = [d for d in (_f(f.get("duration_min")) for f in flights) if d and d > 0]
    lo_d, hi_d = (min(durs), max(durs)) if durs else (0.0, 0.0)

    for f in flights:
        price = _f(f.get("price_total"))
        dur = _f(f.get("duration_min"))
        stops = int(f.get("stops") or 0)
        confidence = _FLIGHT_PROVIDER_CONFIDENCE.get(f.get("source"), _DEFAULT_CONFIDENCE)
        seats = _f(f.get("seats_available"))
        directness = 1.0 if stops == 0 else max(0.0, 1.0 - 0.35 * stops)
        dep = _dep_convenience(f)

        rank = _weighted(
            {
                "price": _minmax(price, lo_p, hi_p, invert=True) if price else 0.2,
                "duration": _minmax(dur, lo_d, hi_d, invert=True) if dur else 0.4,
                "stops": directness,
                "provider": confidence,
                "departure": dep,
            },
            FLIGHT_RANK_WEIGHTS,
        )
        quality = _weighted(
            {
                "price": 1.0 if price else 0.0,
                "schedule": 0.5 * bool(f.get("departure_time")) + 0.5 * bool(f.get("arrival_time")),
                "directness": directness,
                "provider": confidence,
                "seats": _log_scale(seats, cap=9) if seats else 0.3,
            },
            FLIGHT_QUALITY_WEIGHTS,
        )
        f["quality_score"] = quality
        f["rank_score"] = rank

    return flights
