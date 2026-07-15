"""
Unit tests for the Phase 2 retrieval cores (pure, no DB):
  - MemoryService.as_query_rules / describe  (app.trip_state.memory)
  - RetrievalService.rank_and_filter           (app.trip_state.retrieval)

These lock down the documented precedence: score_override > visible=false >
memory rules > reco score > default (cheapest) sort.
"""
from app.trip_state.memory import MemoryService
from app.trip_state.retrieval import rank_and_filter
from app.trip_state.agent import _coerce_memory_value


# ── memory → query rules ─────────────────────────────────────────────────────
def test_as_query_rules_translates_known_keys():
    rows = [
        {"key": "only_refundable", "value": True},
        {"key": "avoid_early_flights", "value": {"before": "09:00"}},
        {"key": "hide_operators", "value": ["IndiGo", "spicejet"]},
        {"key": "rank_axis", "value": "comfort"},
        {"key": "max_price", "value": "5000"},
    ]
    out = MemoryService.as_query_rules(rows)
    assert out["filters"]["refundable_only"] is True
    assert out["filters"]["min_depart_hour"] == 9
    assert out["filters"]["exclude_operators"] == ["indigo", "spicejet"]
    assert out["filters"]["max_price"] == 5000.0
    assert out["sort"] == "score"


def test_as_query_rules_ignores_unknown_keys():
    out = MemoryService.as_query_rules([{"key": "favourite_colour", "value": "blue"}])
    assert out == {"filters": {}, "sort": None}


def test_describe_is_human_readable():
    rows = [{"key": "only_refundable", "value": True},
            {"key": "rank_axis", "value": "comfort"}]
    desc = MemoryService.describe(rows)
    assert "only refundable options" in desc
    assert "rank by comfort" in desc


def test_user_memory_becomes_low_confidence_defaults():
    rows = MemoryService.from_user_memory({"transport_pref": "comfort", "budget_tier": "high"})
    keys = {r["key"]: r["value"] for r in rows}
    assert keys["rank_axis"] == "comfort"
    assert keys["stay_tier"] == "luxury"
    assert all(r["confidence"] < 0.5 for r in rows)


# ── rank_and_filter precedence ───────────────────────────────────────────────
def _results():
    return [
        {"id": "a", "item_type": "flight", "operator": "IndiGo", "price": 3000,
         "departs_at": "2026-08-01T06:00:00", "refundable": False, "summary": {}},
        {"id": "b", "item_type": "flight", "operator": "Vistara", "price": 5000,
         "departs_at": "2026-08-01T10:00:00", "refundable": True, "summary": {}},
        {"id": "c", "item_type": "flight", "operator": "Air India", "price": 4000,
         "departs_at": "2026-08-01T12:00:00", "refundable": True, "summary": {}},
        {"id": "d", "item_type": "train", "operator": "Rajdhani", "price": 1500,
         "departs_at": "2026-08-01T16:00:00", "refundable": True, "summary": {}},
    ]


def test_default_sort_is_cheapest_and_mode_filters():
    out = rank_and_filter(_results(), {}, {}, {}, mode="flight", limit=6)
    assert [o["id"] for o in out] == ["a", "c", "b"]  # trains excluded, price asc


def test_hidden_view_is_dropped():
    views = {"a": {"visible": False}}
    out = rank_and_filter(_results(), views, {}, {}, mode="flight")
    assert "a" not in [o["id"] for o in out]


def test_refundable_and_early_and_operator_rules():
    rules = {"filters": {"refundable_only": True, "min_depart_hour": 9,
                         "exclude_operators": ["air india"]}, "sort": None}
    out = rank_and_filter(_results(), {}, {}, rules, mode="flight")
    # a: not refundable + early → out; c: Air India excluded → out; b: survives
    assert [o["id"] for o in out] == ["b"]


def test_score_override_pins_to_top():
    views = {"c": {"score_override": 1.0}}
    out = rank_and_filter(_results(), views, {}, {}, mode="flight")
    assert out[0]["id"] == "c"  # pinned above cheaper 'a'


def test_score_sort_uses_reco_index():
    scores = {"a": {"score": 10}, "b": {"score": 90}, "c": {"score": 50}}
    rules = {"filters": {}, "sort": "score"}
    out = rank_and_filter(_results(), {}, scores, rules, mode="flight")
    assert [o["id"] for o in out] == ["b", "c", "a"]  # by score desc
    assert out[0]["score"] == 90


def test_favorite_and_notes_surface():
    views = {"b": {"favorite": True, "notes": "aunt loved this"}}
    out = rank_and_filter(_results(), views, {}, {}, mode="flight")
    b = next(o for o in out if o["id"] == "b")
    assert b["favorite"] is True and b["notes"] == "aunt loved this"


# ── copilot `remember` value coercion (agent tool → typed memory value) ───────
def test_coerce_memory_value_round_trips_through_query_rules():
    # what the LLM emits as strings → typed values → query rules
    coerced = [
        {"key": "only_refundable", "value": _coerce_memory_value("only_refundable", "yes")},
        {"key": "hide_operators", "value": _coerce_memory_value("hide_operators", "IndiGo, SpiceJet")},
        {"key": "avoid_early_flights", "value": _coerce_memory_value("avoid_early_flights", "09:00")},
        {"key": "max_price", "value": _coerce_memory_value("max_price", "5,000")},
        {"key": "rank_axis", "value": _coerce_memory_value("rank_axis", "Comfort")},
    ]
    assert coerced[1]["value"] == ["indigo", "spicejet"]
    assert coerced[3]["value"] == 5000.0
    rules = MemoryService.as_query_rules(coerced)
    assert rules["filters"]["refundable_only"] is True
    assert rules["filters"]["exclude_operators"] == ["indigo", "spicejet"]
    assert rules["filters"]["min_depart_hour"] == 9
    assert rules["filters"]["max_price"] == 5000.0
    assert rules["sort"] == "score"  # comfort → score axis


def test_coerce_only_refundable_false_forms():
    assert _coerce_memory_value("only_refundable", "no") is False
    assert _coerce_memory_value("only_refundable", "true") is True
