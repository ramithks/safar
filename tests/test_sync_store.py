"""
Unit tests for the graph→store mirroring extractor (app.trip_state.sync_store).

Pure functions only — verifies a trip's graph (real leg/node ids) and the legacy
transport/stay fallback both flatten into the right batch groups + result rows,
and that the content signature is stable (idempotent re-sync).
"""
from app.trip_state import sync_store


# ── graph path: real leg/node scope ids ──────────────────────────────────────
def _graph_state():
    return {
        "request": {"destination": "Goa", "budget_tier": "mid"},
        "graph": {
            "nodes": [
                {"id": "n_goa", "city": "Goa", "arrival_date": "2026-08-01",
                 "stay": {"search_results": [
                     {"name": "Taj", "hotel_id": "h1", "source": "booking", "price_per_night": 8400,
                      "photos": ["a.jpg"]},   # fresh (unstored) hotels carry heavy fields
                     {"name": "Novotel", "hotel_id": "h2", "source": "agoda", "price_per_night": 6000,
                      "photos": ["b.jpg"]},
                 ], "selected_id": "h1"}},
            ],
            "edges": [
                {"id": "leg1", "from_city": "Delhi", "to_city": "Goa", "date": "2026-08-01",
                 "routes": [{"hops": [{"options": [
                     {"id": "o1", "mode": "flight",
                      "raw": {"airline": "IndiGo", "flight_number": "6E1", "price_total": 4200,
                              "departure_time": "06:00", "arrival_time": "08:30"}},
                     {"id": "o2", "mode": "train",
                      "raw": {"train_name": "Rajdhani", "price": 1800, "departure_time": "16:00"}},
                 ]}]}]},
            ],
        },
    }


def test_graph_groups_use_real_ids():
    groups = sync_store.graph_groups(_graph_state())
    by_kind = {(g["kind"], g["scope_id"]) for g in groups}
    assert ("transport", "leg1") in by_kind      # real leg id, not "going"
    assert ("hotel", "n_goa") in by_kind          # real node id, not dest string


def test_graph_transport_rows_from_option_raw():
    groups = sync_store.graph_groups(_graph_state())
    tg = next(g for g in groups if g["kind"] == "transport")
    rows = sync_store._rows_for(tg, "t1")
    kinds = sorted(r["item_type"] for r in rows)
    assert kinds == ["flight", "train"]
    flight = next(r for r in rows if r["item_type"] == "flight")
    assert flight["price"] == 4200.0
    assert flight["departs_at"] == "2026-08-01T06:00:00"  # anchored to leg date


def test_dedup_repeated_option_ids():
    state = _graph_state()
    # same option id appears in a second route — must not double-count
    state["graph"]["edges"][0]["routes"].append(
        {"hops": [{"options": [state["graph"]["edges"][0]["routes"][0]["hops"][0]["options"][0]]}]})
    tg = next(g for g in sync_store.graph_groups(state) if g["kind"] == "transport")
    rows = sync_store._rows_for(tg, "t1")
    assert len(rows) == 2  # still just flight + train


# ── legacy fallback: no graph ────────────────────────────────────────────────
def test_legacy_fallback_when_no_graph():
    state = {
        "request": {"destination": "Manali"},
        "transport": {"going": {"flights": {"items": [
            {"airline": "Vistara", "price_total": 5000, "departure_time": "07:00"}]}}},
        "stay": {"hotels": [{"name": "Snow Retreat", "hotel_id": "h9", "source": "booking",
                             "price_per_night": 3000}]},
    }
    groups = sync_store.graph_groups(state)
    scopes = {(g["kind"], g["scope_id"]) for g in groups}
    assert ("transport", "going") in scopes
    assert ("hotel", "Manali") in scopes


# ── signature stability (idempotent re-sync) ─────────────────────────────────
def test_signature_is_order_independent_and_stable():
    tg = next(g for g in sync_store.graph_groups(_graph_state()) if g["kind"] == "transport")
    rows = sync_store._rows_for(tg, "t1")
    assert sync_store._signature(rows) == sync_store._signature(list(reversed(rows)))


def test_empty_state_yields_no_groups():
    assert sync_store.graph_groups({}) == []
