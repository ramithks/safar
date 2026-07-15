"""
Hotels master-table persistence (identity cache).

Every hotel the scrapers return is upserted into the `hotels` master table, keyed
by (source, source_hotel_id). This is what lets us build SEO profiles + training
data over time; prices are NOT stored here (they're date-specific and live in
trip_journey). Written ASYNC + BEST-EFFORT — a persistence failure is logged and
dropped, never affecting search results.

Historically only the /plan path wrote here (plan._hotel_master_rows). This helper
centralises it so EVERY hotel fetch — workspace copilot search included — persists
by calling it once from HotelService.search.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _rows(hotels: list[dict], destination: str | None) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for h in hotels:
        source = h.get("source")
        hotel_id = h.get("hotel_id") or h.get("source_hotel_id")
        name = h.get("name")
        if not source or not hotel_id or not name:
            continue
        key = (str(source), str(hotel_id))
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "source": str(source),
            "source_hotel_id": str(hotel_id),
            "name": name,
            "address": h.get("address") or None,
            "city": h.get("city") or destination,
            "destination": destination,
            "lat": _num(h.get("lat") or h.get("latitude")),
            "lon": _num(h.get("lon") or h.get("longitude")),
            "star_rating": _num(h.get("star_rating")),
            "review_score": _num(h.get("review_score") or h.get("rating_score")),
            "review_count": _int(h.get("review_count")),
            "review_label": h.get("review_label") or h.get("rating_label") or None,
            "property_type": h.get("property_type") or None,
            "amenities": h.get("amenities") if isinstance(h.get("amenities"), (list, dict)) else None,
            "photos": h.get("photos") if isinstance(h.get("photos"), (list, dict)) else None,
            "booking_url": h.get("booking_url") or None,
            "map_url": h.get("map_url") or None,
            "description": h.get("description") or None,
            "subscores": h.get("subscores") if isinstance(h.get("subscores"), dict) else None,
            "landmarks": h.get("landmarks") if isinstance(h.get("landmarks"), (list, dict)) else None,
            "quality_score": _num(h.get("quality_score")),
            "last_seen_at": now,
            "updated_at": now,
        })
    return rows


def _upsert_sync(rows: list[dict]) -> None:
    from app.db.client import get_db
    get_db().table("hotels").upsert(rows, on_conflict="source,source_hotel_id").execute()


async def save_hotels_master(hotels: list[dict], destination: str | None) -> int:
    """Upsert scraped hotels into the master table. Returns rows written (0 on any
    failure). Best-effort — call fire-and-forget; never awaited on the response path."""
    rows = _rows(hotels or [], destination)
    if not rows:
        return 0
    try:
        from app.db.client import run_db
        await run_db(_upsert_sync, rows)
        logger.info("[hotel_store] upserted %d hotels (%s)", len(rows), destination)
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — master table is best-effort
        logger.debug("[hotel_store] save failed: %s", exc)
        return 0
