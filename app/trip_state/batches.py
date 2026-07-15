"""
Search store — the append-only, immutable result store that sits beside the slim
TripState (docs/persistence_architecture.md).

A *batch* is one search event (a leg's transport pull, a stop's hotel pull); its
*results* are the individual bookable options. Results are written once and never
mutated — personalization lives in ``trip_result_views`` (Phase 2), not here.

Phase 1 is DUAL-WRITE only: the generator keeps writing the fat blocks into
``trip_states.state`` exactly as before AND mirrors the scraped options into this
store. Nothing reads from here yet, so every write is best-effort — a failure is
logged and dropped, never touching trip generation. Mirrors the fire-and-forget
contract of ``app.reco.transport_store`` and the hotel-master upsert.

Sync DB calls follow the store.py pattern: ``get_db()`` inside the callable,
hopped onto the DB executor via ``run_db``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from app.config import settings

logger = logging.getLogger(__name__)

# EXPERIMENT #3 — cross-thread Supabase client detector. Maps a client object id
# to the FIRST thread that used it; the one-client-per-thread invariant means a
# given client id must never appear under two thread ids. If it does, a client
# leaked across threads (the leading code-level bad_record_mac suspect).
_client_owner: dict[int, int] = {}
_client_owner_lock = threading.Lock()


def _instrumented_insert(trip_id: str, kind: str, batch_id: str, rows: list[dict]) -> str:
    """insert_results + finalize, wrapped with per-write diagnostics (thread id,
    client id, payload size, duration) and cross-thread client-reuse detection.
    Logs full context on SSL/transport failure, then re-raises so the caller's
    best-effort handler still runs. Only active under settings.instrument_batches."""
    from app.db.client import get_db

    tid = threading.get_ident()
    client = get_db()
    cid = id(client)
    payload_bytes = len(json.dumps(rows, default=str))

    with _client_owner_lock:
        owner = _client_owner.setdefault(cid, tid)
    if owner != tid:
        logger.error(
            "[batches-instr] ⚠ CROSS-THREAD CLIENT: client %s owned by thread %s "
            "but used on thread %s (trip %s batch %s) — invariant violated",
            cid, owner, tid, trip_id, batch_id,
        )

    t0 = time.monotonic()
    try:
        n = SearchBatchStore.insert_results(rows)
        SearchBatchStore.finalize(batch_id, n)
        dt = (time.monotonic() - t0) * 1000
        logger.info(
            "[batches-instr] ok trip=%s batch=%s kind=%s rows=%d bytes=%d "
            "thread=%s client=%s dur_ms=%.0f",
            trip_id, batch_id, kind, n, payload_bytes, tid, cid, dt,
        )
        return batch_id
    except Exception as exc:  # noqa: BLE001
        dt = (time.monotonic() - t0) * 1000
        logger.error(
            "[batches-instr] FAIL %s trip=%s batch=%s kind=%s rows=%d bytes=%d "
            "thread=%s client=%s owner_thread=%s dur_ms=%.0f err=%s: %s",
            type(exc).__name__, trip_id, batch_id, kind, len(rows), payload_bytes,
            tid, cid, owner, dt, type(exc).__name__, exc,
        )
        raise

# Transport block keys in a shaped journey (plural) → canonical item_type (singular).
_TRANSPORT_MODES = {"flights": "flight", "trains": "train", "buses": "bus"}

_CLOCK_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p")


# ── small coercers (mirror reco.transport_store) ─────────────────────────────────
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


def _first(d: dict, *keys):
    for k in keys:
        val = d.get(k)
        if val not in (None, ""):
            return val
    return None


def params_hash(kind: str, origin, destination, depart_date, extra: dict | None = None) -> str:
    """Stable short hash identifying an equivalent search — the dedup / cache key."""
    payload = {
        "kind": kind,
        "origin": (origin or "").strip().lower(),
        "destination": (destination or "").strip().lower(),
        "depart_date": str(depart_date or ""),
        **(extra or {}),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _resolve_dt(depart_date, timestr) -> str | None:
    """Best-effort ISO 8601 for a departure/arrival. Providers give either a full
    datetime or a bare clock ("06:10"); a bare clock is anchored to ``depart_date``.
    This is what makes ``departs_at`` a real timestamp and kills the missing-date bug.
    Returns None when nothing parseable is available (raw still has the original)."""
    if timestr in (None, ""):
        return None
    s = str(timestr).strip()
    # Already a datetime-ish string — let Postgres parse it.
    if "T" in s or (len(s) >= 10 and s[4:5] == "-"):
        return s
    if not depart_date:
        return None
    for fmt in _CLOCK_FORMATS:
        try:
            t = datetime.strptime(s.upper() if "M" in fmt else s, fmt).time()
            return f"{str(depart_date)[:10]}T{t.strftime('%H:%M:%S')}"
        except ValueError:
            continue
    return None


# ── row builders (pure — unit-tested without a DB) ───────────────────────────────
def transport_result_row(batch_id: str, trip_id: str, mode: str, depart_date, item: dict) -> dict:
    """One search_results row from a normalized transport item."""
    operator = _first(item, "airline", "operator", "train_name", "bus_operator")
    departs = _resolve_dt(depart_date, _first(item, "departure_time", "depart_time"))
    arrives = _resolve_dt(depart_date, _first(item, "arrival_time", "arrive_time"))
    refundable = item.get("is_refundable")
    summary = {
        "name": operator,
        "service_no": _first(item, "flight_number", "train_number", "service_number"),
        "fare_type": _first(item, "fare_type", "class", "cabin_class"),
        "route_kind": item.get("route_kind") or "direct",
        "booking_url": item.get("booking_url"),
        "stops": _int(item.get("stops")),
    }
    return {
        "batch_id": batch_id,
        "trip_id": trip_id,
        "item_type": mode,
        "ext_id": str(_first(item, "flight_number", "train_number", "service_number") or "") or None,
        "operator": operator,
        "price": _num(_first(item, "price_total", "price", "fare")),
        "currency": item.get("currency") or "INR",
        "departs_at": departs,
        "arrives_at": arrives,
        "duration_min": _int(item.get("duration_min")),
        "stops": _int(item.get("stops")),
        "rating": None,
        "refundable": refundable if isinstance(refundable, bool) else None,
        "summary": {k: v for k, v in summary.items() if v is not None},
        "raw": item,
    }


def hotel_result_row(batch_id: str, trip_id: str, item: dict) -> dict:
    """One search_results row from a normalized hotel item. Photos/amenities stay
    in ``raw`` — the summary is the compact card the LLM/UI reads."""
    price = _num(_first(item, "price_per_night", "price_total", "price_per_stay", "price", "total_price"))
    review = _num(item.get("review_score"))
    summary = {
        "name": item.get("name"),
        "hotel_id": item.get("hotel_id"),
        "source": item.get("source"),
        "star_rating": _num(item.get("star_rating")),
        "review_score": review,
        "review_label": item.get("review_label"),
        "address": item.get("address"),
        "property_type": item.get("property_type"),
        "booking_url": item.get("booking_url"),
    }
    return {
        "batch_id": batch_id,
        "trip_id": trip_id,
        "item_type": "hotel",
        "ext_id": str(item.get("hotel_id") or "") or None,
        "operator": item.get("source"),
        "price": price,
        "currency": item.get("currency") or "INR",
        "departs_at": None,
        "arrives_at": None,
        "duration_min": None,
        "stops": None,
        "rating": review,
        "refundable": None,
        "summary": {k: v for k, v in summary.items() if v is not None},
        "raw": item,
    }


def _mode_items(shaped: dict, mode_key: str) -> list[dict]:
    """Pull every transport item for a mode out of a shaped journey block — the
    top-level ``items`` plus any per-leg ``items`` (multi-leg train/bus bundles)."""
    block = shaped.get(mode_key)
    if not isinstance(block, dict):
        return []
    items: list[dict] = list(block.get("items") or [])
    for leg in block.get("legs") or []:
        if isinstance(leg, dict):
            items.extend(leg.get("items") or [])
    return [it for it in items if isinstance(it, dict)]


def transport_rows_from_shaped(batch_id: str, trip_id: str, depart_date, shaped: dict) -> list[dict]:
    rows: list[dict] = []
    for mode_key, mode in _TRANSPORT_MODES.items():
        for item in _mode_items(shaped, mode_key):
            rows.append(transport_result_row(batch_id, trip_id, mode, depart_date, item))
    return rows


def hotel_rows_from_block(batch_id: str, trip_id: str, hotels_block: dict) -> list[dict]:
    items = (hotels_block or {}).get("items") or []
    return [hotel_result_row(batch_id, trip_id, it) for it in items if isinstance(it, dict)]


# ── SearchBatchStore — DB access (sync; run via run_db) ──────────────────────────
class SearchBatchStore:
    """Create/fill/finalize batches and read summaries. Static methods so callers
    hop them onto the DB executor with ``run_db(SearchBatchStore.method, ...)``."""

    @staticmethod
    def create_batch(trip_id: str, kind: str, *, scope_id: str = "", mode: str | None = None,
                     origin=None, destination=None, depart_date=None,
                     phash: str | None = None, ttl_hours: int = 24) -> str:
        from app.db.client import get_db
        expires = (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat()
        row = get_db().table("search_batches").insert({
            "trip_id": trip_id, "kind": kind, "scope_id": scope_id, "mode": mode,
            "params_hash": phash or params_hash(kind, origin, destination, depart_date),
            "origin": origin, "destination": destination,
            "depart_date": str(depart_date)[:10] if depart_date else None,
            "status": "searching", "expires_at": expires,
        }).execute()
        return row.data[0]["id"]

    @staticmethod
    def insert_results(rows: list[dict]) -> int:
        if not rows:
            return 0
        from app.db.client import get_db
        get_db().table("search_results").insert(rows).execute()
        return len(rows)

    @staticmethod
    def finalize(batch_id: str, result_count: int) -> None:
        from app.db.client import get_db
        get_db().table("search_batches").update({
            "status": "ready" if result_count else "empty",
            "result_count": result_count,
        }).eq("id", batch_id).execute()

    @staticmethod
    def find_live(phash: str) -> dict | None:
        """Newest non-expired batch with these params — the re-search cache (Phase 2)."""
        from app.db.client import get_db
        now = datetime.now(timezone.utc).isoformat()
        res = (get_db().table("search_batches").select("*")
               .eq("params_hash", phash).gt("expires_at", now)
               .order("created_at", desc=True).limit(1).execute())
        return res.data[0] if res.data else None

    @staticmethod
    def load_summaries(batch_id: str, *, item_type: str | None = None,
                       sort: str = "price", limit: int = 50) -> list[dict]:
        """Thin cols + summary, NEVER raw. Read path for Phase 2 / TripContextService."""
        from app.db.client import get_db
        q = (get_db().table("search_results")
             .select("id, item_type, ext_id, operator, price, currency, departs_at, "
                     "arrives_at, duration_min, stops, rating, refundable, summary")
             .eq("batch_id", batch_id))
        if item_type:
            q = q.eq("item_type", item_type)
        desc = sort in ("rating", "score")
        return (q.order(sort if sort in ("price", "departs_at", "duration_min", "rating") else "price",
                        desc=desc).limit(limit).execute().data or [])

    @staticmethod
    def hydrate_raw(result_ids: list[str]) -> dict[str, dict]:
        """Full provider payloads for a few results — only on booking/detail."""
        if not result_ids:
            return {}
        from app.db.client import get_db
        res = (get_db().table("search_results").select("id, raw")
               .in_("id", result_ids).execute())
        return {r["id"]: r.get("raw") for r in (res.data or [])}


# ── async best-effort savers (called fire-and-forget from the generator) ─────────
async def _persist(trip_id: str, kind: str, scope_id: str, mode: str | None,
                   origin, destination, depart_date, rows: list[dict]) -> str | None:
    if not rows:
        return None
    from app.db.client import run_db

    def _work() -> str:
        batch_id = SearchBatchStore.create_batch(
            trip_id, kind, scope_id=scope_id, mode=mode,
            origin=origin, destination=destination, depart_date=depart_date,
        )
        for r in rows:
            r["batch_id"] = batch_id
        if settings.instrument_batches:
            return _instrumented_insert(trip_id, kind, batch_id, rows)
        n = SearchBatchStore.insert_results(rows)
        SearchBatchStore.finalize(batch_id, n)
        return batch_id

    try:
        batch_id = await run_db(_work)
        logger.info("[search-store] %s batch %s: %d results (%s→%s) for trip %s",
                    kind, batch_id, len(rows), origin, destination, trip_id)
        return batch_id
    except Exception as exc:  # noqa: BLE001 — dual-write is best-effort
        logger.warning("[search-store] %s batch save failed for %s: %s", kind, trip_id, exc)
        return None


async def save_transport_batch(trip_id: str, *, scope_id: str, origin, destination,
                               depart_date, shaped: dict) -> str | None:
    """Mirror a shaped journey's transport options into one transport batch."""
    rows = transport_rows_from_shaped("", trip_id, depart_date, shaped)
    return await _persist(trip_id, "transport", scope_id, None,
                          origin, destination, depart_date, rows)


async def save_hotel_batch(trip_id: str, *, scope_id: str, destination,
                           depart_date, hotels_block: dict) -> str | None:
    """Mirror a shaped journey's hotel list into one hotel batch."""
    rows = hotel_rows_from_block("", trip_id, hotels_block or {})
    return await _persist(trip_id, "hotel", scope_id, None,
                          None, destination, depart_date, rows)
