"""
Transport persistence — the training-data flywheel (Part 12.5).

Every scraped flight / train / bus is written to ``transport_options`` so that, over
time, we accumulate the corpus behind the price-fairness model, demand curves, and a
future learned ranker. Written ASYNC and BEST-EFFORT off the hot path (mirrors
``plan._hotel_master_rows``): a persistence failure is logged and dropped — it never
affects search results. The full normalised dict is kept in ``raw`` as ground truth.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _row(mode: str, origin: str, destination: str, depart_date: str, x: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "mode": mode,
        "source": x.get("source") or x.get("provider"),
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date or None,
        "operator": x.get("airline") or x.get("operator") or x.get("train_name") or x.get("bus_operator"),
        "operator_code": x.get("airline_code") or x.get("operator_code"),
        "service_no": x.get("flight_number") or x.get("train_number") or x.get("service_number"),
        "depart_time": x.get("departure_time") or x.get("depart_time"),
        "arrive_time": x.get("arrival_time") or x.get("arrive_time"),
        "duration_min": _int(x.get("duration_min")),
        "stops": _int(x.get("stops")),
        "price": _num(x.get("price_total") or x.get("price") or x.get("fare")),
        "currency": x.get("currency") or "INR",
        "fare_type": x.get("fare_type") or x.get("class") or x.get("cabin_class"),
        "baggage_kg": _int(x.get("baggage_kg")),
        "cabin_baggage_kg": _int(x.get("cabin_baggage_kg")),
        "layover_airports": x.get("layover_airports") if isinstance(x.get("layover_airports"), list) else None,
        "seats_available": _int(x.get("seats_available")),
        "refundable": x.get("is_refundable") if isinstance(x.get("is_refundable"), bool) else None,
        "raw": x,
        "scraped_at": now,
    }


def build_rows(origin: str, destination: str, depart_date: str, *,
               flights=None, trains=None, buses=None) -> list[dict]:
    rows: list[dict] = []
    for mode, items in (("flight", flights), ("train", trains), ("bus", buses)):
        for x in (items or []):
            if isinstance(x, dict):
                rows.append(_row(mode, origin, destination, depart_date, x))
    return rows


def _insert_sync(rows: list[dict]) -> None:
    from app.db.client import get_db
    get_db().table("transport_options").insert(rows).execute()


async def save_transport_options(origin: str, destination: str, depart_date: str, *,
                                 flights=None, trains=None, buses=None) -> int:
    """Persist scraped transport. Returns rows written (0 on any failure). Best-effort
    — call fire-and-forget; never awaited on the response path."""
    rows = build_rows(origin, destination, depart_date, flights=flights, trains=trains, buses=buses)
    if not rows:
        return 0
    try:
        from app.db.client import run_db
        await run_db(_insert_sync, rows)
        logger.info("[transport_store] saved %d transport options %s→%s", len(rows), origin, destination)
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — training data is best-effort
        logger.debug("[transport_store] save failed: %s", exc)
        return 0
