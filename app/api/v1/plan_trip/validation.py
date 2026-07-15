"""
Anti-hallucination layer for Gemini-generated routes and itineraries.

Route validation (validate_leg / validate_route):
  Checks each transport leg against resolved identifiers and live data.
  Every leg gets a validation block: {valid, confidence, issues, warnings}.

Trip health checks (trip_health_check / auto_fix_trip):
  Audits the day-by-day itinerary for duplicates, overload, and closed
  attractions, then auto-fixes what it can.

Confidence scoring (compute_confidence_score / annotate_confidence / filter_low_confidence):
  Score = 0.0–1.0 weighted across 4 signals for Gemini-generated activities.
"""
import math
import re
from difflib import SequenceMatcher

# Per-Gemini-category cap is now RELATIVE to trip length (see
# _category_cap) — a 6-day trip can legitimately hit more temples/cafes than a
# 2-day one without feeling repetitive. This floor applies to very short trips.
_MIN_CATEGORY_CAP = 3

# Soft ceiling on activities per day. Days are no longer squeezed down to 2-3 —
# a full, travel-agent-grade day can hold more. We only trim genuinely
# unrealistic days (see _MAX_ACTIVITIES_PER_DAY) and let the copilot add more
# spots on demand. Prompt asks for 3-5; this is the hard upper bound.
_MAX_ACTIVITIES_PER_DAY = 6


def _category_cap(days: list[dict]) -> int:
    """How many activities may share one Gemini category across the whole trip.
    Scales with trip length so longer trips aren't force-diversified into thin,
    repetitive days."""
    return max(_MIN_CATEGORY_CAP, len(days))

# Suffixes that indicate a "revisit" — normalized away before dup comparison
_REVISIT_TOKENS = frozenset({
    "revisit", "again", "repeat", "return", "part 2", "second visit",
    "2nd visit", "second time", "2nd time",
})

# Realistic speed ranges per mode (km/h)
_SPEED_RANGE: dict[str, tuple[float, float]] = {
    "bus":        (30,  80),
    "train":      (50, 160),
    "flight":    (400, 900),
    "cab":        (20,  80),
    "shared_cab": (20,  60),
    "auto":       (15,  40),
    "metro":      (25,  80),
    "walk":        (3,   6),
    "ferry":      (15,  50),
}

_MAX_HOURS: dict[str, float] = {
    "bus": 18, "train": 36, "flight": 16, "cab": 10,
    "shared_cab": 6, "auto": 2, "metro": 2, "walk": 4, "ferry": 24,
}


def _parse_hours(duration_approx: str) -> float | None:
    h = re.search(r'(\d+)\s*h', duration_approx or "")
    m = re.search(r'(\d+)\s*m(?:in)?', duration_approx or "")
    if not h and not m:
        return None
    return int(h.group(1) if h else 0) + int(m.group(1) if m else 0) / 60.0


# ── Per-leg validation ────────────────────────────────────────────────────────

def validate_leg(leg: dict, resolved: dict) -> dict:
    """
    Returns {valid, confidence, issues, warnings}.
    `resolved` is the pre-resolved identifier map from journey.py Phase 2a.
    """
    issues: list[str] = []
    warnings: list[str] = []
    confidence = 1.0
    mode = leg.get("mode", "").lower()
    sp = leg.get("search_params") or {}

    if mode == "flight":
        fi = sp.get("from_iata") or resolved.get(f"flight|{leg.get('from', '').lower()}")
        ti = sp.get("to_iata") or resolved.get(f"flight|{leg.get('to', '').lower()}")
        if not fi:
            issues.append(f"No airport found for origin: {leg.get('from')}")
            confidence -= 0.40
        if not ti:
            issues.append(f"No airport found for destination: {leg.get('to')}")
            confidence -= 0.40
        if leg.get("needs_live_search") and leg.get("has_live_data") is False:
            warnings.append("No live flights found — route may not have direct service")
            confidence -= 0.20

    elif mode in ("train", "railway"):
        fc = sp.get("from_station_code") or resolved.get(f"train|{leg.get('from', '').lower()}")
        tc = sp.get("to_station_code") or resolved.get(f"train|{leg.get('to', '').lower()}")
        if not fc:
            issues.append(f"No railway station for: {leg.get('from')}")
            confidence -= 0.40
        if not tc:
            issues.append(f"No railway station for: {leg.get('to')}")
            confidence -= 0.40
        if leg.get("needs_live_search") and leg.get("has_live_data") is False:
            warnings.append("No live trains found — check IRCTC for availability")
            confidence -= 0.15

    elif mode == "bus":
        fo = (sp.get("origin") or leg.get("from", "")).lower()
        fd = (sp.get("destination") or leg.get("to", "")).lower()
        if not resolved.get(f"bus|{fo}"):
            issues.append(f"No RedBus stop for: {sp.get('origin') or leg.get('from')}")
            confidence -= 0.30
        if not resolved.get(f"bus|{fd}"):
            issues.append(f"No RedBus stop for: {sp.get('destination') or leg.get('to')}")
            confidence -= 0.30
        if leg.get("needs_live_search") and leg.get("has_live_data") is False:
            warnings.append("No live buses found — try RedBus directly")
            confidence -= 0.15

    # Duration realism check (only when distance is known)
    dist_km = float(leg.get("distance_km") or 0)
    dur_h = _parse_hours(leg.get("duration_approx") or "")
    if dist_km > 0 and dur_h is not None and mode in _SPEED_RANGE:
        min_spd, max_spd = _SPEED_RANGE[mode]
        if dur_h > 0 and dist_km / dur_h > max_spd * 1.2:
            warnings.append(
                f"Duration {leg.get('duration_approx')} seems too fast for "
                f"{dist_km:.0f} km by {mode}"
            )
            confidence -= 0.10
        max_h = _MAX_HOURS.get(mode, 24)
        if dur_h > max_h:
            warnings.append(f"Duration {leg.get('duration_approx')} is unusually long for {mode}")
            confidence -= 0.05

    return {
        "valid": len(issues) == 0,
        "confidence": round(max(0.0, confidence), 2),
        "issues": issues,
        "warnings": warnings,
    }


def validate_route(legs: list[dict], resolved: dict) -> list[dict]:
    """Annotate all legs with validation metadata in-place. Returns legs."""
    for leg in legs:
        leg["validation"] = validate_leg(leg, resolved)
    return legs


# ── Trip health checks ────────────────────────────────────────────────────────

def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _normalize_for_dup(title: str) -> str:
    """Strip revisit/bracket suffixes so 'Shangarh Meadow (Revisit)' == 'Shangarh Meadow'."""
    t = title.lower().strip()
    t = re.sub(r'\s*\([^)]*\)', '', t).strip()  # remove any (...) suffix
    for tok in _REVISIT_TOKENS:
        if t.endswith(tok):
            t = t[: -len(tok)].strip(" -–—,")
    return t


def trip_health_check(days: list[dict]) -> dict:
    """
    Audit itinerary for common AI mistakes.
    Returns {score, issues, warnings, total_activities}.
    Flags duplicate activities on the dict itself (_duplicate=True) for auto_fix_trip.
    """
    issues: list[str] = []
    warnings: list[str] = []
    seen: list[str] = []

    for day in days:
        acts = day.get("activities") or []

        # Duplicate check across all days — normalize titles first
        for act in acts:
            title = act.get("title", "")
            norm = _normalize_for_dup(title)
            for s in seen:
                if _sim(norm, _normalize_for_dup(s)) > 0.75:
                    issues.append(f"Duplicate: '{title}' ≈ '{s}'")
                    act["_duplicate"] = True
                    break
            else:
                seen.append(title)

        # Activity overload
        non_dup = [a for a in acts if not a.get("_duplicate")]
        if len(non_dup) > _MAX_ACTIVITIES_PER_DAY:
            issues.append(
                f"Day {day.get('day')}: {len(non_dup)} activities — max {_MAX_ACTIVITIES_PER_DAY} recommended"
            )

        # Closed attractions
        for act in acts:
            if act.get("open_now") is False:
                warnings.append(f"'{act.get('title')}' appears to be closed right now")

    total = sum(len(d.get("activities") or []) for d in days)
    score = max(0, 100 - len(issues) * 15 - len(warnings) * 5)

    return {
        "score": score,
        "issues": issues,
        "warnings": warnings,
        "total_activities": total,
    }


def enforce_category_diversity(days: list[dict]) -> int:
    """
    Allow up to _category_cap() activities per Gemini category across the whole
    trip (scales with trip length). Removes the weakest (lowest confidence_score)
    excess activities per category. Guarantees each day retains at least 2
    activities — restores the best cut activity if needed. Returns count removed.
    """
    cap = _category_cap(days)
    by_cat: dict[str, list[tuple[int, dict]]] = {}
    for d_idx, day in enumerate(days):
        for act in (day.get("activities") or []):
            cat = act.get("category") or "Other"
            by_cat.setdefault(cat, []).append((d_idx, act))

    for cat, pairs in by_cat.items():
        if len(pairs) <= cap:
            continue
        # Weakest first — those are candidates for removal
        pairs.sort(key=lambda x: x[1].get("confidence_score", 0.5))
        for _d_idx, act in pairs[: len(pairs) - cap]:
            act["_diversity_cut"] = True

    removed = 0
    for day in days:
        acts = day.get("activities") or []
        keep = [a for a in acts if not a.get("_diversity_cut")]
        cut  = [a for a in acts if a.get("_diversity_cut")]

        # Restore best cut activity if day would drop below 2
        cut.sort(key=lambda a: a.get("confidence_score", 0.0), reverse=True)
        while len(keep) < 2 and cut:
            restored = cut.pop(0)
            restored.pop("_diversity_cut", None)
            keep.append(restored)

        for a in cut:
            a.pop("_diversity_cut", None)
            removed += 1

        day["activities"] = keep

    return removed


def auto_fix_trip(days: list[dict]) -> int:
    """
    Remove flagged duplicates, cap days at _MAX_ACTIVITIES_PER_DAY activities,
    then enforce category diversity. Returns total count of activities removed.
    """
    removed = 0
    for day in days:
        acts = day.get("activities") or []
        deduped = [a for a in acts if not a.pop("_duplicate", False)]
        removed += len(acts) - len(deduped)

        if len(deduped) > _MAX_ACTIVITIES_PER_DAY:
            deduped.sort(key=lambda a: a.get("confidence_score", 0.5), reverse=True)
            removed += len(deduped) - _MAX_ACTIVITIES_PER_DAY
            deduped = deduped[:_MAX_ACTIVITIES_PER_DAY]

        day["activities"] = deduped

    removed += enforce_category_diversity(days)
    return removed


# ── Confidence scoring ────────────────────────────────────────────────────────
# Score = 0.0–1.0 weighted across 4 signals:
#   Google Places verified (place_id present):  0.40
#   Rating quality (0-5 → 0-0.30):              0.30
#   Review count (log scale → 0-0.20):          0.20
#   Opening hours data available:               0.10
#
# Activities below FILTER_THRESHOLD are dropped unless a day would
# be left with fewer than 2 activities, in which case the best low-
# confidence activity is kept.

FILTER_THRESHOLD = 0.30


def compute_confidence_score(act: dict) -> float:
    score = 0.0

    if act.get("place_id") and act.get("source") == "google_places":
        score += 0.40
    elif act.get("place_id"):
        score += 0.20

    rating = act.get("rating") or 0
    if rating > 0:
        score += (min(float(rating), 5.0) / 5.0) * 0.30

    reviews = act.get("user_ratings_total") or 0
    if reviews > 0:
        score += min(math.log10(float(reviews)) / 4.0, 1.0) * 0.20

    if act.get("opening_hours"):
        score += 0.10

    return round(min(score, 1.0), 2)


def annotate_confidence(days: list[dict]) -> None:
    """Add confidence_score to every activity in-place."""
    for day in days:
        for act in (day.get("activities") or []):
            act["confidence_score"] = compute_confidence_score(act)


def filter_low_confidence(days: list[dict], threshold: float = FILTER_THRESHOLD) -> list[str]:
    """
    Remove activities below threshold.
    Guarantees each day keeps at least 2 activities.
    Returns list of dropped activity titles for logging.
    """
    removed: list[str] = []
    for day in days:
        acts = day.get("activities") or []
        kept = [a for a in acts if a.get("confidence_score", 1.0) >= threshold]
        dropped = [a for a in acts if a.get("confidence_score", 1.0) < threshold]

        if len(kept) < 2 and dropped:
            dropped.sort(key=lambda a: a.get("confidence_score", 0.0), reverse=True)
            kept.append(dropped.pop(0))

        removed.extend(a.get("title", "unknown") for a in dropped)
        day["activities"] = kept

    return removed
