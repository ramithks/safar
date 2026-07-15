"""Budget-aware hotel ranking — a LOW-budget traveller must see the genuinely
cheapest stay first, not a pricier one with nicer reviews.

Regression for the live report where an ₹849/night room lost to a ₹4,435 one
that the agent then mislabelled 'the most budget-friendly option'.
"""
from app.api.v1.plan_trip.helpers import rank_hotels


CHEAP = {"name": "Beyond Rishikesh - Cozy Stay", "price_per_night": 849, "review_score": 7.4}
PRICEY = {"name": "Hotel The Shivaay Delight Inn", "price_per_night": 4435, "review_score": 8.9}


def test_low_budget_ranks_cheapest_first():
    ranked = rank_hotels([PRICEY, CHEAP], budget="low")
    assert ranked[0]["name"] == CHEAP["name"]


def test_low_budget_price_beats_reviews_below_10k():
    # Both under ₹10k — the old min(1, 10000/price) saturated to 1.0 for both,
    # so reviews decided and the pricier one won. Price must differentiate now.
    hotels = [
        {"name": "nice-but-pricey", "price_per_night": 3800, "review_score": 9.2},
        {"name": "cheapest", "price_per_night": 900, "review_score": 6.8},
        {"name": "mid", "price_per_night": 1800, "review_score": 8.0},
    ]
    ranked = rank_hotels(hotels, budget="low")
    assert [h["name"] for h in ranked] == ["cheapest", "mid", "nice-but-pricey"]


def test_mid_budget_keeps_quality_led_order():
    # mid/high behaviour is unchanged: quality can outweigh a modest premium.
    ranked = rank_hotels([CHEAP, PRICEY], budget="mid")
    assert ranked[0]["name"] == PRICEY["name"]


def test_unknown_price_sinks_for_low_budget():
    mystery = {"name": "no-price", "review_score": 9.5}
    ranked = rank_hotels([mystery, CHEAP], budget="low")
    assert ranked[0]["name"] == CHEAP["name"]
