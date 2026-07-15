"""
Unit tests for the recommendation engine (app/reco).

Covers the deterministic logic that ships the ranked list without any LLM / Redis /
pgvector — i.e. the part that must be correct even when every external dependency is
down. Semantic / LLM / DB paths are exercised for graceful degradation (they return
None / [] and never raise), not for their live output.
"""
import asyncio

from app.reco import personas, features, rank, intent, journey, personality, cold_start, explain
from app.reco.features import SetStats
from app.reco.transport_store import build_rows


# ── personas ────────────────────────────────────────────────────────────────────
def test_derive_persona():
    assert personas.derive_persona({"trip_purpose": "honeymoon"}) == personas.HONEYMOON
    assert personas.derive_persona({"trip_purpose": "business"}) == personas.BUSINESS
    assert personas.derive_persona({"traveller_type": "family"}) == personas.BUDGET_FAMILY
    assert personas.derive_persona({"budget_tier": "low", "traveller_type": "solo"}) == personas.BACKPACKER
    assert personas.derive_persona({}) == personas.DEFAULT


def test_weights_normalised_and_price_sensitivity():
    for p in personas.PERSONAS:
        w = personas.resolve_hotel_weights(p, {})
        assert abs(sum(w.values()) - 1.0) < 1e-6
        wt = personas.resolve_transport_weights(p, {})
        assert abs(sum(wt.values()) - 1.0) < 1e-6
    # Higher price sensitivity raises the price weight.
    lo = personas.resolve_hotel_weights(personas.DEFAULT, {"price_sensitivity": 0.1})
    hi = personas.resolve_hotel_weights(personas.DEFAULT, {"price_sensitivity": 0.9})
    assert hi["price"] > lo["price"]


# ── features ────────────────────────────────────────────────────────────────────
def test_budget_fit_direction():
    stats = SetStats(lo_price=1000, hi_price=9000)
    assert features._budget_fit(1000, stats, "low") > features._budget_fit(9000, stats, "low")
    assert features._budget_fit(9000, stats, "high") > features._budget_fit(1000, stats, "high")
    assert features._budget_fit(None, stats, "low") is None


def test_review_confidence_shrinks_thin_evidence():
    strong = features._review_confidence(9.0, 400)
    thin = features._review_confidence(9.0, 2)
    assert strong > thin


def test_cosine01_bounds():
    assert features.cosine01([1, 0], [1, 0]) == 1.0
    assert abs(features.cosine01([1, 0], [0, 1]) - 0.5) < 1e-9
    assert features.cosine01(None, [1, 0]) is None


def test_arrival_quality():
    assert features._arrival_quality(14) == 1.0            # daytime
    assert features._arrival_quality(3) < 0.3              # red-eye
    assert features._arrival_quality(None) is None


# ── rank ────────────────────────────────────────────────────────────────────────
def test_thin_cheap_beats_rich_pricey_low_budget():
    ctx = {"intent": {"budget_tier": "low"}, "budget": "low", "persona": personas.BACKPACKER}
    thin = {"name": "Thin", "price_per_night": 1200, "review_score": 8.0, "source": "easemytrip"}
    rich = {"name": "Rich", "price_per_night": 6000, "review_score": 8.2, "review_count": 400,
            "distance_km": 0.5, "subscores": {"facilities": 9}, "source": "booking"}
    rank.rank_hotels([thin, rich], ctx)
    assert thin["_reco_score"] >= rich["_reco_score"]
    assert thin["match"] in ("high", "good", "fair")


def test_mmr_avoids_near_duplicate_second():
    A = {"name": "A", "_reco_score": 90, "personality_tags": ["lake_view"], "city": "U", "address": "X", "price_per_night": 5000}
    A2 = {"name": "A2", "_reco_score": 85, "personality_tags": ["lake_view"], "city": "U", "address": "X", "price_per_night": 5100}
    B = {"name": "B", "_reco_score": 80, "personality_tags": ["beachfront"], "city": "U", "address": "Y", "price_per_night": 9000}
    C = {"name": "C", "_reco_score": 70, "personality_tags": ["party_hub"], "city": "U", "address": "Z", "price_per_night": 2000}
    out = rank.mmr_diversify([A, A2, B, C], k=4)
    assert out[0]["name"] == "A"
    assert out[1]["name"] == "B"      # the near-identical A2 is pushed down


# ── intent ──────────────────────────────────────────────────────────────────────
def test_deterministic_intent_from_intake():
    i = intent.deterministic_intent({"destination": "Goa", "budget": "low", "who": "couple",
                                     "vibe": "beach", "travelers": 2, "departure_date": "2026-08-03"})
    assert i["budget_tier"] == "low"
    assert i["traveller_type"] == "couple"
    assert i["price_sensitivity"] == 0.75
    assert "beach" in i["vibes"]
    assert i["hard"]["pax"] == 2 and i["hard"]["date_from"] == "2026-08-03"


def test_explicit_budget_overrides_llm():
    # normalise must honour the explicit intake budget over the model's guess.
    out = intent._normalise({"budget_tier": "high"}, {"budget": "low"})
    assert out["budget_tier"] == "low"


def test_extract_intent_no_text_is_deterministic():
    i = asyncio.run(intent.extract_intent("", {"budget": "mid", "travelers": 1}))
    assert i["budget_tier"] == "mid"
    assert i["hard"]["pax"] == 1


# ── journey ─────────────────────────────────────────────────────────────────────
def test_day_arrival_beats_redeye():
    ctx = {"intent": {"budget_tier": "mid"}, "budget": "mid"}
    day = {"airline": "Day", "price_total": 4000, "duration_min": 120, "stops": 0,
           "arrival_time": "2026-08-03T14:00", "source": "booking"}
    redeye = {"airline": "Red", "price_total": 3200, "duration_min": 600, "stops": 1,
              "arrival_time": "2026-08-03T02:30", "source": "easemytrip"}
    journey.rank_journeys([day, redeye], ctx, hotel_per_night=3000)
    assert day["_journey_score"] > redeye["_journey_score"]


def test_fatigue_increases_with_legs_and_redeye():
    calm = {"duration_min": 120, "stops": 0, "arrival_time": "2026-08-03T14:00"}
    rough = {"duration_min": 600, "stops": 2, "arrival_time": "2026-08-03T03:00", "layover_minutes": 180}
    assert journey._fatigue(rough) > journey._fatigue(calm)


# ── personality ─────────────────────────────────────────────────────────────────
def test_hotel_rule_tags():
    h = {"name": "Lake Palace Haveli", "star_rating": 4, "distance_km": 0.6,
         "review_score": 8.6, "subscores": {"location": 8.9}, "couple_friendly": True,
         "landmarks": [{"name": "Lake Pichola"}]}
    tags = personality.hotel_rule_tags(h, price_hi=7000, price_lo=1500)
    assert "lake_view" in tags and "heritage_royal" in tags and "romantic_couples" in tags
    assert tags <= set(personality.HOTEL_VOCAB)


def test_rule_tags_survive_null_db_fields():
    # DB columns present-but-null (property_type/landmarks/nearest_transport) must not
    # crash rule tagging — dict.get(k, "") returns None, not "", for a null value.
    h = {"name": "Some Hotel", "property_type": None, "landmarks": None,
         "nearest_transport": None, "star_rating": None, "review_score": None,
         "subscores": None}
    assert isinstance(personality.hotel_rule_tags(h), set)     # no TypeError
    h2 = {"name": None, "property_type": "Lakeside Resort", "landmarks": [None, {"name": None}]}
    assert isinstance(personality.hotel_rule_tags(h2), set)


def test_transport_tags_and_fare_tags():
    t = {"airline": "X", "price_total": 4000, "duration_min": 120, "stops": 0,
         "baggage_kg": 20, "fare_tags": ["PRICE"], "departure_time": "2026-08-03T06:30"}
    st = SetStats.for_transport([t])
    tags = personality.transport_tags(t, st)
    assert "nonstop_premium" in tags and "generous_baggage" in tags and "budget_hero" in tags
    assert "red_eye" not in tags and "early_bird" in tags   # 06:30 = early, not red-eye


def test_cluster_tags_graceful_without_centroids():
    assert personality.cluster_tags([0.1, 0.2], {}) == ([], 0.0)
    assert personality.cluster_tags(None, {"x": [0.1]}) == ([], 0.0)
    assert personality.needs_llm(set(), 0.1) is True
    assert personality.needs_llm({"lake_view", "city_central"}, 0.1) is False


# ── cold start ──────────────────────────────────────────────────────────────────
def test_is_cold():
    assert cold_start.is_cold({}) is True
    assert cold_start.is_cold({"trips_planned": 3}) is False
    assert cold_start.is_cold({"notes": "loves quiet"}) is False


# ── explain ─────────────────────────────────────────────────────────────────────
def test_deterministic_explanation_and_tradeoff():
    h = {"name": "Central Stay", "price_per_night": 2000, "distance_km": 0.8,
         "review_score": 8.7, "review_count": 120, "free_cancellation": True,
         "personality_tags": ["city_central", "romantic_couples"], "match": "good"}
    exp = explain.deterministic_explanation(h, cheapest=1200, kind="hotel")
    assert exp["why"]
    assert "over the cheapest" in exp["tradeoff"]
    assert "couples" in exp["best_for"]
    assert exp["match"] == "good"


def test_annotate_list_strips_internal_and_embedding():
    items = [{"name": "A", "price_per_night": 1000, "_reco_score": 88.0,
              "_embedding": [0.1] * 384, "_reco_components": {}, "match": "high"}]
    explain.annotate_list(items, {}, "hotel")
    it = items[0]
    for internal in ("_reco_score", "_embedding", "_reco_components", "_journey_score"):
        assert internal not in it
    assert "why" in it and "match" in it


# ── transport_store ─────────────────────────────────────────────────────────────
def test_build_rows_maps_modes():
    rows = build_rows("Delhi", "Goa", "2026-08-03",
                      flights=[{"airline": "IndiGo", "flight_number": "6E1", "price_total": 4200,
                                "duration_min": 150, "stops": 0, "baggage_kg": 15, "source": "booking"}],
                      trains=[{"train_name": "Rajdhani", "train_number": "12951", "price": 1800,
                               "class": "3A", "source": "confirmtkt"}],
                      buses=[{"operator": "VRL", "price": 1200, "source": "redbus"}])
    assert [r["mode"] for r in rows] == ["flight", "train", "bus"]
    flight = rows[0]
    assert flight["operator"] == "IndiGo" and flight["price"] == 4200 and flight["baggage_kg"] == 15
    assert rows[1]["operator"] == "Rajdhani" and rows[1]["service_no"] == "12951"
    assert all("raw" in r and "scraped_at" in r for r in rows)
