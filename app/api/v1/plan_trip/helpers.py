"""
Pure utility functions and constants for trip planning.
No FastAPI / Supabase dependencies — safe to import anywhere.
"""
import asyncio
import logging
import math
import random
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Currency config ────────────────────────────────────────────────────────────
CURRENCY_CONFIG: dict[str, dict] = {
    "usd": {"symbol": "$",   "hotel": {"budget": 60,   "mid-range": 160,   "luxury": 450  }, "level": {0: 30,   1: 60,   2: 150,  3: 320,  4: 550 }, "range": {"low": (40,   90  ), "mid": (90,   220 ), "high": (220,  600 )}, "ceiling": {"low": 200,   "mid": 550,   "high": 1600 }},
    "eur": {"symbol": "€",   "hotel": {"budget": 55,   "mid-range": 140,   "luxury": 400  }, "level": {0: 25,   1: 55,   2: 130,  3: 280,  4: 480 }, "range": {"low": (35,   80  ), "mid": (80,   190 ), "high": (190,  520 )}, "ceiling": {"low": 180,   "mid": 480,   "high": 1400 }},
    "gbp": {"symbol": "£",   "hotel": {"budget": 50,   "mid-range": 120,   "luxury": 350  }, "level": {0: 22,   1: 50,   2: 115,  3: 250,  4: 420 }, "range": {"low": (30,   70  ), "mid": (70,   170 ), "high": (170,  450 )}, "ceiling": {"low": 160,   "mid": 420,   "high": 1200 }},
    "inr": {"symbol": "₹",   "hotel": {"budget": 2000, "mid-range": 5500,  "luxury": 16000}, "level": {0: 800,  1: 1800, 2: 4500, 3: 9000, 4: 18000}, "range": {"low": (1000, 2500), "mid": (2500, 7000), "high": (7000, 20000)}, "ceiling": {"low": 5000,  "mid": 15000, "high": 50000}},
    "jpy": {"symbol": "¥",   "hotel": {"budget": 7000, "mid-range": 20000, "luxury": 60000}, "level": {0: 3000, 1: 7000, 2: 18000, 3: 40000, 4: 75000}, "range": {"low": (4000, 9000), "mid": (9000, 28000), "high": (28000, 80000)}, "ceiling": {"low": 15000, "mid": 55000, "high": 180000}},
    "aud": {"symbol": "A$",  "hotel": {"budget": 90,   "mid-range": 220,   "luxury": 580  }, "level": {0: 45,   1: 90,   2: 200,  3: 430,  4: 720 }, "range": {"low": (60,   110 ), "mid": (110,  280 ), "high": (280,  650 )}, "ceiling": {"low": 280,   "mid": 720,   "high": 2100 }},
    "aed": {"symbol": "AED", "hotel": {"budget": 200,  "mid-range": 600,   "luxury": 1800 }, "level": {0: 100,  1: 200,  2: 550,  3: 1300, 4: 2600}, "range": {"low": (150,  260 ), "mid": (260,  720 ), "high": (720,  2100)}, "ceiling": {"low": 700,   "mid": 2100,  "high": 6500 }},
    "sgd": {"symbol": "S$",  "hotel": {"budget": 80,   "mid-range": 200,   "luxury": 550  }, "level": {0: 40,   1: 80,   2: 190,  3: 400,  4: 680 }, "range": {"low": (55,   100 ), "mid": (100,  260 ), "high": (260,  680 )}, "ceiling": {"low": 250,   "mid": 680,   "high": 2000 }},
}


# ── Deterministic field filling ───────────────────────────────────────────────
_DURATION_MINS: dict[str, int] = {
    "Museum": 90, "Temple": 45, "Landmark": 60, "Park": 75, "Cafe": 60,
    "Restaurant": 75, "Street Food": 30, "Beach": 120, "Market": 60,
    "Viewpoint": 45, "Bar": 90, "Rooftop": 75, "Gallery": 75, "Mall": 90,
    "Trek": 180, "Waterfall": 60, "Hiking": 150, "Camping": 120,
}

# Category + best_time → ideal start minute from midnight.
# best_time is Gemini's field: "Morning" | "Afternoon" | "Evening" | "Anytime"
_CATEGORY_IDEAL_START: dict[str, dict[str, int]] = {
    "Viewpoint":   {"Morning": 6*60,    "Afternoon": 14*60, "Evening": 17*60+30, "Anytime": 8*60},
    "Trek":        {"Morning": 6*60,    "Afternoon": 11*60, "Evening": 6*60,     "Anytime": 7*60},
    "Hiking":      {"Morning": 6*60,    "Afternoon": 11*60, "Evening": 6*60,     "Anytime": 7*60},
    "Waterfall":   {"Morning": 8*60,    "Afternoon": 11*60, "Evening": 8*60,     "Anytime": 9*60},
    "Beach":       {"Morning": 7*60,    "Afternoon": 14*60, "Evening": 17*60,    "Anytime": 9*60},
    "Park":        {"Morning": 7*60,    "Afternoon": 15*60, "Evening": 17*60,    "Anytime": 9*60},
    "Temple":      {"Morning": 6*60,    "Afternoon": 11*60, "Evening": 17*60,    "Anytime": 8*60},
    "Museum":      {"Morning": 10*60,   "Afternoon": 14*60, "Evening": 10*60,    "Anytime": 10*60},
    "Gallery":     {"Morning": 10*60,   "Afternoon": 14*60, "Evening": 10*60,    "Anytime": 11*60},
    "Landmark":    {"Morning": 9*60,    "Afternoon": 14*60, "Evening": 16*60,    "Anytime": 10*60},
    "Cafe":        {"Morning": 8*60,    "Afternoon": 13*60, "Evening": 18*60,    "Anytime": 12*60},
    "Restaurant":  {"Morning": 8*60,    "Afternoon": 13*60, "Evening": 19*60,    "Anytime": 13*60},
    "Street Food": {"Morning": 8*60,    "Afternoon": 12*60, "Evening": 18*60,    "Anytime": 12*60},
    "Market":      {"Morning": 9*60,    "Afternoon": 14*60, "Evening": 17*60,    "Anytime": 10*60},
    "Mall":        {"Morning": 11*60,   "Afternoon": 14*60, "Evening": 17*60,    "Anytime": 12*60},
    "Rooftop":     {"Morning": 10*60,   "Afternoon": 16*60, "Evening": 18*60,    "Anytime": 18*60},
    "Bar":         {"Morning": 20*60,   "Afternoon": 20*60, "Evening": 20*60,    "Anytime": 20*60},
    "Camping":     {"Morning": 7*60,    "Afternoon": 15*60, "Evening": 18*60,    "Anytime": 10*60},
}

_DEFAULT_IDEAL_MIN: dict[str, int] = {
    "Morning": 9*60, "Afternoon": 13*60, "Evening": 17*60, "Anytime": 10*60,
}


def _ideal_start_min(act: dict) -> int:
    cat = act.get("category", "")
    best = (act.get("best_time") or "Anytime").strip().title()
    lookup = _CATEGORY_IDEAL_START.get(cat)
    if lookup:
        return lookup.get(best) or lookup.get("Anytime") or _DEFAULT_IDEAL_MIN.get(best, 10*60)
    return _DEFAULT_IDEAL_MIN.get(best, 10*60)


VALID_ACTIVITY_TYPES = {"attraction", "food", "experience", "shopping", "nightlife", "transport"}


def normalise_activity_type(t: str | None) -> str:
    if not t:
        return "attraction"
    t = t.lower().strip()
    return t if t in VALID_ACTIVITY_TYPES else "attraction"


def fill_deterministic_fields(days: list[dict], currency: str, budget: str) -> None:  # noqa: ARG001
    """Fill duration_min and type. Time is NOT assigned here — call assign_activity_times() after route sort."""
    for day in days:
        for act in (day.get("activities") or []):
            if not act.get("duration_min"):
                act["duration_min"] = _DURATION_MINS.get(act.get("category", ""), 60)
            act["type"] = normalise_activity_type(act.get("type"))


def assign_activity_times(days: list[dict]) -> None:
    """
    Assign deterministic HH:MM start_time to every activity.
    Uses category + best_time lookup — viewpoints get dawn/dusk, cafes get lunch, treks get early morning.
    Sorts activities within each day by ideal time (time-correctness > route order).
    Must be called AFTER nearest_neighbor_sort.
    """
    BUFFER_MIN = 30

    for day in days:
        activities = day.get("activities") or []
        if not activities:
            continue

        # Sort by ideal start time so schedule flows naturally through the day
        activities = sorted(activities, key=_ideal_start_min)

        prev_end_min = 0
        for act in activities:
            ideal = _ideal_start_min(act)
            start_min = ideal if prev_end_min == 0 else max(ideal, prev_end_min + BUFFER_MIN)
            start_min = max(start_min, 5 * 60)   # never before 05:00
            start_min = min(start_min, 22 * 60)  # never after 22:00
            h, m = divmod(start_min, 60)
            act["start_time"] = f"{h:02d}:{m:02d}"
            duration = act.get("duration_min") or _DURATION_MINS.get(act.get("category", ""), 60)
            prev_end_min = start_min + duration

        day["activities"] = activities


# ── Geo helpers ───────────────────────────────────────────────────────────────
def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def nearest_neighbor_sort(activities: list[dict]) -> list[dict]:
    if len(activities) <= 2:
        return activities
    hotel = next((a for a in activities if a.get("type") == "hotel"), None)
    rest = [a for a in activities if a.get("type") != "hotel"]
    with_coords = [a for a in rest if a.get("lat") and a.get("lng")]
    without_coords = [a for a in rest if not (a.get("lat") and a.get("lng"))]
    if len(with_coords) <= 1:
        return activities

    start_lat = hotel["lat"] if hotel and hotel.get("lat") else with_coords[0]["lat"]
    start_lng = hotel["lng"] if hotel and hotel.get("lng") else with_coords[0]["lng"]
    remaining = list(with_coords)
    sorted_acts: list[dict] = []
    while remaining:
        idx = min(range(len(remaining)), key=lambda i: haversine_km(start_lat, start_lng, remaining[i]["lat"], remaining[i]["lng"]))
        nxt = remaining.pop(idx)
        sorted_acts.append(nxt)
        start_lat, start_lng = nxt["lat"], nxt["lng"]

    all_sorted = sorted_acts + without_coords
    return ([hotel] + all_sorted) if hotel else all_sorted


# ── Retry utility ─────────────────────────────────────────────────────────────
async def with_retry(coro_fn, *args, max_retries: int = 2, base_delay: float = 1.5, fallback=None, **kwargs):
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries:
                logger.warning("[retry] %s failed after %dx: %s", getattr(coro_fn, "__name__", "?"), max_retries + 1, e)
                return fallback
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
    return fallback



# ── Misc ──────────────────────────────────────────────────────────────────────
def clean_city(name: str) -> str:
    return name.split(",")[0].strip() if name else name


def stable_seed(*parts) -> int:
    """
    Deterministic 31-bit seed from request parameters, so identical filters
    produce the identical Gemini output across processes and restarts
    (Python's built-in hash() is salted per process — unusable for this).
    """
    import hashlib
    key = "|".join(str(p).lower().strip() for p in parts)
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) & 0x7FFFFFFF


def build_date_context(start_date: str | None, end_date: str | None, destination: str | None) -> str:
    if not start_date:
        return ""
    try:
        start = datetime.fromisoformat(start_date)
        m = start.month
        season = "Spring" if 3 <= m <= 5 else "Summer" if 6 <= m <= 8 else "Autumn/Fall" if 9 <= m <= 11 else "Winter"
        ctx = f"\nTRAVEL DATES: {start.strftime('%A')} {start.day} {start.strftime('%B')} {start.year}"
        if end_date:
            end = datetime.fromisoformat(end_date)
            ctx += f" → {end.day} {end.strftime('%B')} {end.year}"
        ctx += f" ({season} season in the Northern Hemisphere)."
        ctx += (
            f"\nDATE-AWARE PLANNING NOTES:"
            f"\n- Consider {destination}'s typical weather in {start.strftime('%B')} ({season})"
            f"\n- Note major local festivals or events in this period"
            f"\n- Include 1-2 date-specific tips per day"
        )
        return ctx
    except Exception:
        return ""



def compute_trip_intelligence(itinerary: dict, budget: str, days: int, ceiling_per_day: dict) -> dict:
    day_plans = itinerary.get("days") or []
    acts_per_day = [len([a for a in (d.get("activities") or []) if a.get("type") != "hotel"]) for d in day_plans]
    avg_acts = sum(acts_per_day) / max(len(day_plans), 1)
    # Days are capped at 3 activities, so calibrate: ≤2.2 avg is relaxed,
    # up to the cap is moderate, anything that slipped past the cap is packed.
    pace = "Relaxed" if avg_acts <= 2.2 else "Moderate" if avg_acts <= 3.0 else "Packed"

    total_dist, dist_count = 0.0, 0
    for d in day_plans:
        acts = [a for a in (d.get("activities") or []) if a.get("lat") and a.get("lng")]
        for i in range(1, len(acts)):
            total_dist += haversine_km(acts[i-1]["lat"], acts[i-1]["lng"], acts[i]["lat"], acts[i]["lng"])
            dist_count += 1
    avg_walking = round(total_dist / max(dist_count, 1) * 10) / 10
    travel_eff = max(0, min(100, round(100 - (avg_walking - 1) * 10)))
    if travel_eff >= 90:
        eff_reason = "Activities clustered in the same neighborhood — minimal transit"
    elif travel_eff >= 70:
        eff_reason = "Activities spread across 2–3 nearby areas — light commuting"
    elif travel_eff >= 50:
        eff_reason = "Activities span multiple neighborhoods — plan for transit time"
    else:
        eff_reason = "Activities are spread across the city — expect significant travel between spots"

    cat_counts: dict[str, int] = {}
    for d in day_plans:
        for a in (d.get("activities") or []):
            if a.get("type") and a["type"] != "hotel":
                cat_counts[a["type"]] = cat_counts.get(a["type"], 0) + 1
    top_cat = max(cat_counts, key=lambda k: cat_counts[k]) if cat_counts else "attraction"

    summary_map = {
        "Relaxed": f"A relaxed {top_cat}-focused journey with room to breathe",
        "Moderate": f"A well-balanced trip mixing {top_cat} and exploration",
        "Packed": "An action-packed adventure — every moment is maximized",
    }
    return {
        "pace": pace,
        "travel_efficiency_score": travel_eff,
        "travel_efficiency_reason": eff_reason,
        "avg_walking_distance_km": avg_walking,
        "top_category": top_cat,
        "ai_summary": summary_map.get(pace, "A perfectly curated journey awaits"),
    }


def _hotel_price(h: dict) -> float:
    return float(h.get("price_per_night") or 0)


def rank_hotels(hotels: list[dict], budget: str = "mid") -> list[dict]:
    """Order hotels for the pick + the shortlist the recommender sees.

    Budget-aware: a *low*-budget traveller wants the cheapest acceptable stay,
    so price leads and quality only breaks ties — otherwise a ₹4,000 hotel with
    nicer reviews outranks an ₹850 one for someone who asked to go cheap. mid/
    high stay quality-led. NOTE the old blend used `min(1, 10000/price)`, which
    saturates at 1.0 for anything under ₹10k — so price never differentiated two
    cheap hotels. We normalise against the cheapest in THIS result set instead.

    Partner stays are pinned above the ranked list instead of being scored into
    it — they are the stays Safar can actually take a booking for, and the
    card tags them as such.
    """
    from app.services.partner_stays import pin_partners

    if budget == "low":
        prices = [p for p in (_hotel_price(h) for h in hotels) if p > 0]
        cheapest = min(prices) if prices else 0.0

        def _score_low(h: dict) -> float:
            review = float(h.get("review_score") or h.get("ta_rating") or h.get("star_rating") or 0)
            price = _hotel_price(h)
            # cheapest → 1.0, a 2× price → 0.5; unknown price sinks (budget users
            # want a real, priced-and-cheap room, not a mystery rate).
            price_n = (cheapest / price) if price > 0 and cheapest > 0 else 0.0
            return price_n * 0.8 + min(1.0, review / 10.0) * 0.2

        return pin_partners(sorted(hotels, key=_score_low, reverse=True))

    def _score(h: dict) -> float:
        review = float(h.get("review_score") or h.get("ta_rating") or h.get("star_rating") or 0)
        price = _hotel_price(h)
        price_eff = min(1.0, 10000 / price) if price > 0 else 0.5
        return review * 0.6 + price_eff * 10 * 0.4
    return pin_partners(sorted(hotels, key=_score, reverse=True))




# ── DB helpers ────────────────────────────────────────────────────────────────
def fetch_user_profile(db, user_id: str) -> dict:
    try:
        r = db.table("profiles").select("country, age, language, currency").eq("id", user_id).maybe_single().execute()
        return r.data or {}
    except Exception:
        return {}


def fetch_memory_context(db, user_id: str, destination: str | None) -> str:
    try:
        trips_r = db.table("trips").select("destination").eq("user_id", user_id).order("created_at", desc=True).limit(5).execute()
        past = list({t["destination"] for t in (trips_r.data or []) if t.get("destination")})[:3]
        if not past:
            return ""
        return f"\n\nUSER MEMORY & PREFERENCES:\nPreviously visited: {', '.join(past)}.\nPersonalize accordingly."
    except Exception:
        return ""
