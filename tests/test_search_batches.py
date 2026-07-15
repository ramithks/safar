"""
Unit tests for the search-store row builders (app/trip_state/batches).

Pure functions only — no DB. Verifies the fat shaped-journey / hotel blocks are
split into flat search_results rows with resolved timestamps, thin indexable
columns, a compact summary, and the raw provider payload preserved verbatim.
"""
from app.trip_state import batches


# ── _resolve_dt: the missing-date fix ────────────────────────────────────────
def test_resolve_dt_anchors_bare_clock_to_depart_date():
    assert batches._resolve_dt("2026-08-01", "06:10") == "2026-08-01T06:10:00"


def test_resolve_dt_parses_am_pm():
    assert batches._resolve_dt("2026-08-01", "6:10 AM") == "2026-08-01T06:10:00"


def test_resolve_dt_passes_through_full_datetime():
    assert batches._resolve_dt("2026-08-01", "2026-08-02T09:40:00") == "2026-08-02T09:40:00"


def test_resolve_dt_none_when_unparseable_or_no_date():
    assert batches._resolve_dt("2026-08-01", "") is None
    assert batches._resolve_dt(None, "06:10") is None
    assert batches._resolve_dt("2026-08-01", "sometime") is None


# ── params_hash: stable + order-independent ──────────────────────────────────
def test_params_hash_stable_and_case_insensitive():
    a = batches.params_hash("transport", "Delhi", "Goa", "2026-08-01")
    b = batches.params_hash("transport", " delhi ", "GOA", "2026-08-01")
    assert a == b and len(a) == 16


def test_params_hash_varies_with_date():
    assert (batches.params_hash("transport", "Delhi", "Goa", "2026-08-01")
            != batches.params_hash("transport", "Delhi", "Goa", "2026-08-02"))


# ── transport extraction ─────────────────────────────────────────────────────
def _shaped():
    return {
        "flights": {"items": [
            {"airline": "IndiGo", "flight_number": "6E123", "price_total": 4200,
             "departure_time": "06:10", "arrival_time": "08:40", "duration_min": 150,
             "stops": 0, "is_refundable": False, "booking_url": "http://x", "currency": "INR"},
        ]},
        "trains": {"legs": [
            {"items": [
                {"train_name": "Rajdhani", "train_number": "12951", "price": 1800,
                 "departure_time": "16:00", "arrival_time": "08:00", "duration_min": 960},
            ]},
        ]},
        "buses": {"items": [
            {"bus_operator": "VRL", "price": 900, "departure_time": "21:00"},
        ]},
    }


def test_transport_rows_cover_all_modes():
    rows = batches.transport_rows_from_shaped("b1", "t1", "2026-08-01", _shaped())
    kinds = sorted(r["item_type"] for r in rows)
    assert kinds == ["bus", "flight", "train"]
    assert all(r["batch_id"] == "b1" and r["trip_id"] == "t1" for r in rows)


def test_transport_row_thin_columns_and_resolved_times():
    rows = batches.transport_rows_from_shaped("b1", "t1", "2026-08-01", _shaped())
    flight = next(r for r in rows if r["item_type"] == "flight")
    assert flight["operator"] == "IndiGo"
    assert flight["price"] == 4200.0
    assert flight["departs_at"] == "2026-08-01T06:10:00"
    assert flight["arrives_at"] == "2026-08-01T08:40:00"
    assert flight["duration_min"] == 150
    assert flight["refundable"] is False
    assert flight["ext_id"] == "6E123"


def test_transport_summary_compact_and_raw_preserved():
    rows = batches.transport_rows_from_shaped("b1", "t1", "2026-08-01", _shaped())
    flight = next(r for r in rows if r["item_type"] == "flight")
    # summary is the card, raw is verbatim ground truth
    assert flight["summary"]["name"] == "IndiGo"
    assert flight["summary"]["booking_url"] == "http://x"
    assert None not in flight["summary"].values()
    assert flight["raw"]["flight_number"] == "6E123"


def test_multileg_train_items_flattened():
    rows = batches.transport_rows_from_shaped("b1", "t1", "2026-08-01", _shaped())
    trains = [r for r in rows if r["item_type"] == "train"]
    assert len(trains) == 1 and trains[0]["operator"] == "Rajdhani"


def test_empty_shaped_yields_no_rows():
    assert batches.transport_rows_from_shaped("b1", "t1", "2026-08-01", {}) == []


# ── hotel extraction ─────────────────────────────────────────────────────────
def test_hotel_rows_from_block():
    block = {"items": [
        {"name": "Taj", "hotel_id": "h1", "source": "booking", "price_per_night": 8400,
         "star_rating": 5, "review_score": 9.1, "review_label": "Superb",
         "booking_url": "http://h", "photos": ["a", "b"], "amenities": ["pool"]},
    ]}
    rows = batches.hotel_rows_from_block("b2", "t1", block)
    assert len(rows) == 1
    row = rows[0]
    assert row["item_type"] == "hotel"
    assert row["price"] == 8400.0
    assert row["rating"] == 9.1
    assert row["ext_id"] == "h1"
    # heavy fields stay in raw, not the summary card
    assert "photos" not in row["summary"]
    assert row["raw"]["photos"] == ["a", "b"]
    assert row["summary"]["name"] == "Taj"
