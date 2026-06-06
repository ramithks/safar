"""
Detailed budget engine — replaces vague low/mid/high labels with an
itemized breakdown users can actually trust.

Five buckets:
  transport  — live fares from journey (or tier estimate as fallback)
  stay       — hotel nightly rate × nights (live or estimated)
  food       — realistic daily food spend per person × days × travelers
  activities — sum of estimated_cost from itinerary; fallback to tier estimate
  misc       — 10% buffer (tips, local transport, sim card, etc.)
"""
from .helpers import CURRENCY_CONFIG

# Daily food budget per person (per currency, per budget tier)
_FOOD_DAILY: dict[str, dict[str, int]] = {
    "low":  {"inr": 500,  "usd": 20,  "eur": 18,  "gbp": 15,  "jpy": 2000,  "aud": 30,  "aed": 70,  "sgd": 25},
    "mid":  {"inr": 1200, "usd": 45,  "eur": 40,  "gbp": 35,  "jpy": 5000,  "aud": 70,  "aed": 160, "sgd": 60},
    "high": {"inr": 3000, "usd": 100, "eur": 90,  "gbp": 80,  "jpy": 12000, "aud": 150, "aed": 350, "sgd": 130},
}

# Daily activity entry fees per person (museums, parks, experiences)
_ACTIVITY_DAILY: dict[str, dict[str, int]] = {
    "low":  {"inr": 300,  "usd": 10,  "eur": 8,   "gbp": 7,   "jpy": 1000,  "aud": 15,  "aed": 35,  "sgd": 12},
    "mid":  {"inr": 800,  "usd": 25,  "eur": 22,  "gbp": 20,  "jpy": 2500,  "aud": 35,  "aed": 90,  "sgd": 30},
    "high": {"inr": 2000, "usd": 60,  "eur": 55,  "gbp": 50,  "jpy": 7000,  "aud": 90,  "aed": 220, "sgd": 80},
}

_HOTEL_TIER = {"low": "budget", "mid": "mid-range", "high": "luxury"}


def compute_detailed_budget(
    journey_result: dict | None,
    itinerary_result: dict | None,
    budget: str,
    travelers: int,
    stay_nights: int,
    currency_code: str,
) -> dict:
    safe_budget = budget if budget in ("low", "mid", "high") else "mid"
    c = currency_code.lower()
    ci = CURRENCY_CONFIG.get(c) or CURRENCY_CONFIG["inr"]
    symbol = ci["symbol"]

    # ── Transport ─────────────────────────────────────────────────────────────
    transport_per_person = 0
    if journey_result and not isinstance(journey_result, Exception):
        cb = journey_result.get("cost_breakdown") or {}
        transport_per_person = (
            cb.get("transport_per_person")
            or journey_result.get("cost_per_person")
            or 0
        )
    transport_total = transport_per_person * travelers

    # ── Stay ─────────────────────────────────────────────────────────────────
    hotel_per_night: int | None = None
    if journey_result and not isinstance(journey_result, Exception):
        cb = journey_result.get("cost_breakdown") or {}
        hotel_per_night = cb.get("hotel_per_night")

    if not hotel_per_night:
        hotel_per_night = ci["hotel"].get(_HOTEL_TIER.get(safe_budget, "mid-range"), 0)

    stay_total = (hotel_per_night or 0) * stay_nights

    # ── Days count ────────────────────────────────────────────────────────────
    num_days = stay_nights + 1
    if itinerary_result and not isinstance(itinerary_result, Exception):
        days_list = itinerary_result.get("days") or []
        if days_list:
            num_days = len(days_list)

    # ── Food ─────────────────────────────────────────────────────────────────
    food_daily = (_FOOD_DAILY.get(safe_budget) or {}).get(c, 0)
    food_total = food_daily * num_days * travelers

    # ── Activities ────────────────────────────────────────────────────────────
    activity_total = 0
    if itinerary_result and not isinstance(itinerary_result, Exception):
        activity_total = sum(
            a.get("estimated_cost") or 0
            for d in (itinerary_result.get("days") or [])
            for a in (d.get("activities") or [])
        )
    if not activity_total:
        acts_daily = (_ACTIVITY_DAILY.get(safe_budget) or {}).get(c, 0)
        activity_total = acts_daily * num_days * travelers

    # ── Misc (10% buffer) ────────────────────────────────────────────────────
    misc = round((transport_total + stay_total) * 0.10)

    grand_total = transport_total + stay_total + food_total + activity_total + misc

    return {
        "currency": symbol,
        "currency_code": c,
        "transport": transport_total,
        "stay": stay_total,
        "food": food_total,
        "activities": activity_total,
        "misc": misc,
        "total": grand_total,
        "per_person": round(grand_total / max(travelers, 1)),
        "hotel_per_night": hotel_per_night,
        "travelers": travelers,
        "nights": stay_nights,
        "source": "live+estimated",
    }
