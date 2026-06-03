"""
Pure utility functions and constants for trip planning.
No FastAPI / Supabase dependencies — safe to import anywhere.
"""
import asyncio
import math
import random
from datetime import datetime

from app.config import settings

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
_COST_DEFAULTS: dict[str, dict] = {
    "usd": {"food": 15,  "attraction": 10,  "nightlife": 40,   "experience": 25,  "hotel": 120},
    "eur": {"food": 13,  "attraction": 8,   "nightlife": 35,   "experience": 22,  "hotel": 100},
    "gbp": {"food": 12,  "attraction": 7,   "nightlife": 30,   "experience": 20,  "hotel": 90 },
    "inr": {"food": 600, "attraction": 300, "nightlife": 1200, "experience": 900, "hotel": 4000},
    "jpy": {"food": 1500,"attraction": 800, "nightlife": 4000, "experience": 2500,"hotel": 15000},
    "aud": {"food": 22,  "attraction": 15,  "nightlife": 55,   "experience": 35,  "hotel": 160},
    "aed": {"food": 55,  "attraction": 30,  "nightlife": 120,  "experience": 80,  "hotel": 400},
    "sgd": {"food": 20,  "attraction": 12,  "nightlife": 50,   "experience": 30,  "hotel": 150},
}
_DURATION_MINS: dict[str, int] = {
    "Museum": 90, "Temple": 45, "Landmark": 60, "Park": 75, "Cafe": 60,
    "Restaurant": 75, "Street Food": 30, "Beach": 120, "Market": 60,
    "Viewpoint": 30, "Bar": 90, "Rooftop": 60, "Gallery": 75, "Mall": 90,
}
_TIME_SLOTS = ["09:00 AM", "11:00 AM", "01:00 PM", "03:30 PM", "06:00 PM", "08:00 PM"]


def fill_deterministic_fields(days: list[dict], currency: str, budget: str) -> None:
    """
    Fill time, duration, and estimated_cost deterministically.
    Gemini no longer outputs these — system controls them.
    """
    costs = _COST_DEFAULTS.get(currency.lower(), _COST_DEFAULTS["usd"])
    mult = {"low": 0.6, "mid": 1.0, "high": 2.2}.get(budget, 1.0)

    for day in days:
        slot = 0
        for act in (day.get("activities") or []):
            if not act.get("time"):
                act["time"] = _TIME_SLOTS[min(slot, len(_TIME_SLOTS) - 1)]
            slot += 1

            if not act.get("duration"):
                mins = _DURATION_MINS.get(act.get("category", ""), 60)
                h, m = divmod(mins, 60)
                act["duration"] = f"{h}h {m}m" if h and m else (f"{h}h" if h else f"{m} mins")

            if not act.get("estimated_cost"):
                base = costs.get(act.get("type", ""), costs["attraction"])
                act["estimated_cost"] = round(base * mult)


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
                print(f"[retry] {getattr(coro_fn, '__name__', '?')} failed after {max_retries + 1}x: {e}")
                return fallback
            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)
    return fallback


# ── Misc ──────────────────────────────────────────────────────────────────────
def clean_city(name: str) -> str:
    return name.split(",")[0].strip() if name else name


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
    pace = "Relaxed" if avg_acts <= 2.5 else "Moderate" if avg_acts <= 4.5 else "Packed"

    total_cost = sum(a.get("estimated_cost") or 0 for d in day_plans for a in (d.get("activities") or []))
    ceiling = (ceiling_per_day.get(budget) or ceiling_per_day.get("mid") or 500) * days
    # Under budget = 100% (good). Over budget = penalised proportionally.
    if total_cost <= ceiling:
        budget_fit = 100
    else:
        overshoot = (total_cost - ceiling) / max(ceiling, 1)
        budget_fit = max(0, round((1 - overshoot) * 100))

    total_dist, dist_count = 0.0, 0
    for d in day_plans:
        acts = [a for a in (d.get("activities") or []) if a.get("lat") and a.get("lng")]
        for i in range(1, len(acts)):
            total_dist += haversine_km(acts[i-1]["lat"], acts[i-1]["lng"], acts[i]["lat"], acts[i]["lng"])
            dist_count += 1
    avg_walking = round(total_dist / max(dist_count, 1) * 10) / 10
    travel_eff = max(0, min(100, round(100 - (avg_walking - 1) * 10)))

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
        "budget_fit_score": budget_fit,
        "travel_efficiency_score": travel_eff,
        "avg_walking_distance_km": avg_walking,
        "top_category": top_cat,
        "total_estimated_cost": total_cost,
        "ai_summary": summary_map.get(pace, "A perfectly curated journey awaits"),
    }


def rank_hotels(hotels: list[dict]) -> list[dict]:
    def _score(h: dict) -> float:
        review = float(h.get("review_score") or h.get("ta_rating") or h.get("star_rating") or 0)
        price = float(h.get("price_per_night") or h.get("estimated_price_per_night") or 0)
        price_eff = min(1.0, 10000 / price) if price > 0 else 0.5
        return review * 0.6 + price_eff * 10 * 0.4
    return sorted(hotels, key=_score, reverse=True)


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
        graph_r = db.table("user_preference_graph").select("*").eq("user_id", user_id).maybe_single().execute()
        past = list({t["destination"] for t in (trips_r.data or []) if t.get("destination")})[:3]
        graph = graph_r.data
        if not graph and not past:
            return ""
        parts = []
        if past:
            parts.append(f"Previously visited: {', '.join(past)}")
        if graph:
            vw = graph.get("vibe_weights") or {}
            top_vibe = max(vw, key=lambda k: vw[k]) if vw else "balanced"
            parts.append(f"Dominant travel vibe: {top_vibe}")
            if graph.get("budget_behavior"):
                parts.append(f"Budget behavior: {graph['budget_behavior']}")
            if graph.get("pace_preference"):
                parts.append(f"Pace preference: {graph['pace_preference']}")
            fp = graph.get("food_preference") or {}
            top_food = sorted(fp, key=lambda k: -fp[k])[:2]
            if top_food:
                parts.append(f"Prefers: {' and '.join(top_food)} food experiences")
            if destination and graph.get("destination_affinity", {}).get(destination):
                parts.append(f"Affinity for {destination}: {graph['destination_affinity'][destination]}/100")
        return f"\n\nUSER MEMORY & PREFERENCES:\n{'. '.join(parts)}.\nPersonalize accordingly."
    except Exception:
        return ""


def pick_city_id(cities: list[dict]) -> int | None:
    if not cities:
        return None
    for c in cities:
        if c.get("type") == "CITY":
            return c["id"]
    for c in cities:
        if c.get("type") == "AREA":
            return c["id"]
    return cities[0]["id"] if cities else None
