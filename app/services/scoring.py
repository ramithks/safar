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
    "price": 0.22,
    "rating": 0.20,
    "reviews": 0.10,
    "distance": 0.15,
    "provider": 0.10,
    "images": 0.08,
    "amenities": 0.07,
    "freshness": 0.03,
    "flexibility": 0.05,   # free cancellation / no prepayment — de-risks the booking
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
    "price": 0.37,
    "duration": 0.30,
    "stops": 0.15,
    "provider": 0.10,
    "departure": 0.03,
    "baggage": 0.05,   # checked-baggage allowance — a real cost signal for many
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
    """Sum component*weight → scale to 0..100. Missing components count as 0.

    Used for ``quality_score`` (absolute richness) where a MISSING signal SHOULD
    lower the score — a listing with no photos is genuinely less rich.
    """
    total = sum(components.get(k, 0.0) * w for k, w in weights.items())
    return round(100.0 * total, 1)


def _weighted_present(components: dict[str, float | None], weights: dict[str, float]) -> float:
    """Sum component*weight → 0..100, but RENORMALISE over only the components that
    are present (non-None). A listing is scored on what it HAS, not penalised to 0
    for a signal a thin provider never emitted.

    Used for ``rank_score`` (ordering): a Booking hotel with subscores + reviews +
    distance and an EaseMyTrip hotel with only price + rating are each scored on
    their own present signals, reweighted to sum 1 — so the thin listing isn't
    unfairly buried, and the rich one isn't diluted. `None` = "unknown/absent";
    a real `0.0` (e.g. "no free cancellation") still counts.
    """
    present = {k: w for k, w in weights.items() if components.get(k) is not None}
    z = sum(present.values())
    if z <= 0.0:
        return 50.0  # nothing to go on → neutral
    return round(100.0 * sum(components[k] * (w / z) for k, w in present.items()), 1)


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


# ── Cross-provider rating normalisation ──────────────────────────────────────
# Each source speaks a different dialect: Booking/Agoda/Ixigo give a /10 guest
# score, EaseMyTrip gives only a TripAdvisor /5 rating, some carry a text label
# ("Very Good"), some carry stars, some nothing. This unifies them so EVERY hotel
# reads the same way in the UI + to the copilot.
_RATING_BUCKETS = (
    (9.0, "Exceptional"),
    (8.0, "Very Good"),
    (7.0, "Good"),
    (6.0, "Pleasant"),
    (0.0, "Okay"),
)
# Placeholder/garbage "labels" some providers emit in the label slot — not real.
_BAD_RATING_LABELS = {"", "review score", "score", "rating", "n/a", "na", "-"}


def _rating_label_for(score: float) -> str:
    for threshold, label in _RATING_BUCKETS:
        if score >= threshold:
            return label
    return "Okay"


def normalize_rating(h: dict) -> dict:
    """Attach a unified rating view to a hotel dict (mutates + returns it):

      rating_score  — guest score on a 0..10 scale, or None if genuinely unrated.
      rating_label  — 'Very Good' etc.; the provider's own if real, else derived.
      rating_basis  — 'Guest reviews' | 'TripAdvisor' | None (honesty about source).
      star_rating   — kept as the property CLASS (0..5); 0 normalised to None
                      (0 stars means 'unrated', not a zero-star hotel).

    Also backfills the legacy ``review_score``/``review_label`` fields when a
    provider left them empty, so existing consumers render consistently without
    needing to know about the new fields.
    """
    score = _f(h.get("review_score")) or _f(h.get("google_rating"))
    basis = "Guest reviews" if score and score > 0 else None
    if not score or score <= 0:
        ta = _f(h.get("ta_rating"))              # EaseMyTrip: TripAdvisor, /5
        if ta and ta > 0:
            score = round(ta * 2, 1)             # → /10
            basis = "TripAdvisor"
        else:
            score = None
    h["rating_score"] = score
    h["rating_basis"] = basis

    raw = str(h.get("review_label") or "").strip()
    if raw and raw.lower() not in _BAD_RATING_LABELS:
        label = raw
    elif score is not None:
        label = _rating_label_for(score)
    else:
        label = None
    h["rating_label"] = label

    star = _f(h.get("star_rating"))
    h["star_rating"] = star if star and star > 0 else None

    # Backfill legacy fields so current UI/copilot show a consistent value.
    if h.get("review_score") in (None, "", 0, 0.0) and score is not None:
        h["review_score"] = score
    if not h.get("review_label") or str(h.get("review_label")).strip().lower() in _BAD_RATING_LABELS:
        if label:
            h["review_label"] = label
    return h


def score_hotels(hotels: list[dict]) -> list[dict]:
    """
    Attach ``quality_score`` + ``rank_score`` to each hotel and return the list
    ordered by rank_score (best first). Mutates the dicts in place.
    """
    if not hotels:
        return hotels

    prices = [p for p in (_hotel_price(h) for h in hotels) if p and p > 0]
    lo_p, hi_p = (min(prices), max(prices)) if prices else (0.0, 0.0)
    cheapest = lo_p if prices else None
    dists = [d for d in (_f(h.get("distance_km")) for h in hotels) if d is not None]
    hi_d = max(dists) if dists else 0.0
    lo_d = min(dists) if dists else 0.0

    for h in hotels:
        normalize_rating(h)   # unify review_score/label/stars across providers
        price = _hotel_price(h)
        rating = _hotel_rating(h)
        reviews = _f(h.get("review_count")) or 0
        photos = len(h.get("photos") or [])
        amenities = len(h.get("amenities") or [])
        dist = _f(h.get("distance_km"))
        confidence = _HOTEL_PROVIDER_CONFIDENCE.get(h.get("source"), _DEFAULT_CONFIDENCE)
        has_desc = bool(h.get("description") or h.get("review_label"))
        flexible = bool(h.get("free_cancellation"))
        no_prepay = bool(h.get("no_prepayment"))
        breakfast = bool(h.get("is_breakfast_included") or h.get("meal_plan"))

        # Freshness proxy: a live price + flexible cancellation + in-stock signals
        # a current, bookable listing (we have no scrape timestamp per hotel).
        freshness = (
            0.5 * (price is not None)
            + 0.3 * flexible
            + 0.2 * _hotel_available(h)
        )

        # rank_score is absent-field-aware: a signal a provider never emitted is
        # None (dropped + renormalised), NOT a neutral guess that dilutes the score.
        # Real zeros (flexibility with no free-cancellation) still count.
        rank = _weighted_present(
            {
                "price": _minmax(price, lo_p, hi_p, invert=True) if price else None,
                "rating": (rating / 10.0) if rating is not None else None,
                "reviews": _log_scale(reviews, cap=2000) if reviews else None,
                "distance": _minmax(dist, 0.0, hi_d, invert=True) if dist is not None else None,
                "provider": confidence,
                "images": _log_scale(photos, cap=6) if photos else None,
                "amenities": _log_scale(amenities, cap=15) if amenities else None,
                "freshness": freshness,
                "flexibility": (0.7 * flexible) + (0.3 * no_prepay),
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

        # Human-readable "why" chips derived from the same signals — surfaced in
        # the UI + fed to the copilot so a pick reads as concretely justified.
        tags: list[str] = []
        if price and cheapest is not None:
            if price <= cheapest * 1.02:
                tags.append("Lowest price")
            elif price <= cheapest * 1.15:
                tags.append(f"₹{round(price - cheapest)} over cheapest")
        if dist is not None and dist <= max(lo_d + 0.3, 1.0):
            tags.append("Closest to centre")
        if rating is not None and rating >= 9 and reviews >= 50:
            tags.append("Top rated")
        elif rating is not None and rating >= 8:
            tags.append("Highly rated")
        if flexible:
            tags.append("Free cancellation")
        if breakfast:
            tags.append("Breakfast included")
        h["why_tags"] = tags[:4]

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
    cheapest = lo_p if prices else None
    durs = [d for d in (_f(f.get("duration_min")) for f in flights) if d and d > 0]
    lo_d, hi_d = (min(durs), max(durs)) if durs else (0.0, 0.0)

    for f in flights:
        price = _f(f.get("price_total"))
        dur = _f(f.get("duration_min"))
        stops = int(f.get("stops") or 0)
        confidence = _FLIGHT_PROVIDER_CONFIDENCE.get(f.get("source"), _DEFAULT_CONFIDENCE)
        seats = _f(f.get("seats_available"))
        baggage = _f(f.get("baggage_kg"))
        directness = 1.0 if stops == 0 else max(0.0, 1.0 - 0.35 * stops)
        dep = _dep_convenience(f)

        # Absent-field-aware (see score_hotels): unknown price/duration/baggage are
        # None (renormalised out), not a neutral guess. stops→directness is always
        # known (0 stops = non-stop), so it always counts.
        rank = _weighted_present(
            {
                "price": _minmax(price, lo_p, hi_p, invert=True) if price else None,
                "duration": _minmax(dur, lo_d, hi_d, invert=True) if dur else None,
                "stops": directness,
                "provider": confidence,
                "departure": dep,
                # 15kg+ saturates to 1.0; unknown allowance → None (not scored).
                "baggage": _minmax(baggage, 0.0, 20.0) if baggage else None,
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

        tags: list[str] = []
        if price and cheapest is not None:
            if price <= cheapest * 1.02:
                tags.append("Lowest fare")
            elif price <= cheapest * 1.15:
                tags.append(f"₹{round(price - cheapest)} over cheapest")
        if stops == 0:
            tags.append("Non-stop")
        if dur is not None and durs and dur <= lo_d * 1.05:
            tags.append("Fastest")
        if baggage and baggage >= 15:
            tags.append(f"{baggage:g}kg baggage")
        if seats is not None and 0 < seats <= 5:
            tags.append(f"Only {int(seats)} seats left")
        f["why_tags"] = tags[:4]

    return flights
