"""
Partner stays inside hotel search — our own inventory, shown first.

Every other row in a stay search is an OTA listing we merely link out to. A
partner listing is different in kind: the owner is on Safar, the price is
theirs, and the booking completes *here* (instant checkout or an enquiry we
own). That is why these rows lead the list and carry a `Partner` tag — the
traveller is being shown a different product, and the tag says so. Ranking the
rest of the list stays untouched: no partner row is ever scored against an OTA
row, they are a pinned group above it.

Rules this module holds to:

  * **Only real, bookable inventory.** Published, in the searched destination,
    with a photo and a price, and not sold out for the requested nights. A
    partner row that renders as a broken card, or sends someone to a full
    house, costs more trust than the placement buys.
  * **Never invent a rating.** `review_score` is emitted only from published
    `listing_reviews` on that listing. A partner's own brand rating is not the
    property's rating and is never borrowed as one.
  * **The price is a from-price.** Cheapest active room type, or the listing's
    `price_from`. Season rates and per-night overrides are applied at /quote,
    which is the only place a number becomes a charge.
  * **A failure here is invisible.** Any exception returns no partner rows and
    the OTA list is served exactly as before.

Shape: rows come back in the same dict shape the scrapers emit (name,
price_per_night, photos, booking_url, …) so every existing card, map and
serializer renders them unchanged. `is_partner` is the only new thing the UI
must know about.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any

from app.config import settings
from app.db.client import get_db, run_db
from app.services.marketplace_availability import check_stay, room_types

logger = logging.getLogger(__name__)

# The `source` tag partner rows carry. Providers are matched on this string in
# the website's provider registry — keep the two in step.
PARTNER_SOURCE = "safar"

# Partner inventory is thin by design; more than three pinned rows and the
# organic list stops being the list.
MAX_PARTNER_STAYS = 3

# Candidates pulled per destination before availability filtering.
_CANDIDATE_CAP = 24
# How many of those get an availability check. Each costs two DB round-trips and
# only MAX_PARTNER_STAYS can be shown, so the shortlist is deliberately short.
_AVAILABILITY_CHECK_CAP = 8

_CACHE_TTL_S = 60.0
_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()

_LISTING_COLS = (
    "id, slug, title, summary, description, destination, city, address, lat, lon, "
    "photos, price_from, original_price, price_unit, amenities, tags, category, "
    "content, booking_mode, cancellation_policy, updated_at, "
    "partner:partners(brand_name, slug, verified, response_minutes)"
)

_LABELS = (
    (9.0, "Exceptional"),
    (8.0, "Excellent"),
    (7.0, "Very good"),
    (6.0, "Good"),
)


def _norm_place(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _as_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# ── data access ──────────────────────────────────────────────────────────────

def _fetch_candidates(destination: str) -> list[dict]:
    """Published partner stays for a destination. Memoised briefly — a trip
    generation searches several cities and the planner re-searches often."""
    key = _norm_place(destination)
    now = time.time()

    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]

    pattern = f"%{destination.strip()}%"
    rows = (
        get_db()
        .table("partner_listings")
        .select(_LISTING_COLS)
        .eq("status", "published")
        .eq("kind", "hotel")
        .or_(f"destination.ilike.{pattern},city.ilike.{pattern}")
        .limit(_CANDIDATE_CAP)
        .execute()
        .data
        or []
    )

    with _cache_lock:
        _cache[key] = (now, rows)
        if len(_cache) > 200:  # bounded; stay traffic is long-tail by city
            for stale in [k for k, v in _cache.items() if now - v[0] > _CACHE_TTL_S]:
                _cache.pop(stale, None)
    return rows


def _fetch_review_stats(listing_ids: list[str]) -> dict[str, tuple[float, int]]:
    """{listing_id: (avg_rating_5, count)} from published reviews. One read for
    the whole candidate set — never per listing."""
    if not listing_ids:
        return {}
    rows = (
        get_db()
        .table("listing_reviews")
        .select("item_id, rating")
        .eq("item_type", "hotel")
        .eq("status", "published")
        .in_("item_id", listing_ids)
        .execute()
        .data
        or []
    )
    acc: dict[str, list[int]] = {}
    for r in rows:
        try:
            acc.setdefault(str(r["item_id"]), []).append(int(r["rating"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {k: (sum(v) / len(v), len(v)) for k, v in acc.items() if v}


# ── row → hotel-card shape ───────────────────────────────────────────────────

def _review_fields(stats: tuple[float, int] | None) -> dict[str, Any]:
    """Ratings only when travellers actually left them."""
    if not stats:
        return {}
    avg5, count = stats
    score10 = round(avg5 * 2, 1)
    label = next((text for floor, text in _LABELS if score10 >= floor), None)
    out: dict[str, Any] = {"review_score": score10, "review_count": count}
    if label:
        out["review_label"] = label
    return out


def _free_cancellation(row: dict) -> bool:
    """Only claim it when the partner's own policy says so, in their words."""
    text = " ".join(
        str(v or "")
        for v in (
            row.get("cancellation_policy"),
            ((row.get("content") or {}).get("policies") or {}).get("cancellation"),
        )
    ).lower()
    return "free cancel" in text or "full refund" in text


def _photo_urls(row: dict) -> list[str]:
    """Absolute photo URLs.

    Partner uploads are stored as the website's own branded proxy path —
    `/image/<base64url object id>` (lib/marketplace/media.ts) — not as storage
    URLs. Every hotel card here treats a photo as an absolute URL and drops
    anything that isn't (`^https?://`), so a relative path renders as a hotel
    with no pictures, and the mobile app has no origin to resolve it against.
    Absolutise once, on the way out.
    """
    out: list[str] = []
    for p in row.get("photos") or []:
        if not isinstance(p, str) or not p.strip():
            continue
        p = p.strip()
        if p.startswith("//"):
            out.append(f"https:{p}")
        elif p.startswith("/"):
            out.append(f"{settings.site_url.rstrip('/')}{p}")
        elif p.startswith(("http://", "https://")):
            out.append(p)
    return out


def _discount(row: dict, price: int) -> dict[str, Any]:
    """The strike-through price and its saving, in the same fields the OTA
    parsers emit (`price_original` / `savings` / `discount_pct`), so one card
    renders both.

    Only when the original genuinely exceeds the price we're showing. A partner
    sets `original_price` against `price_from`, but the price on the card can be
    a cheaper room type — and a strike-through that isn't a saving is a lie the
    DB constraint can't catch.
    """
    try:
        original = int(row.get("original_price") or 0)
    except (TypeError, ValueError):
        return {}
    if original <= price:
        return {}
    return {
        "price_original": original,
        "savings": original - price,
        "discount_pct": round((original - price) / original * 100),
    }


def _price_of(row: dict, rooms: list[dict]) -> int | None:
    prices = [
        int(r["base_price"])
        for r in rooms
        if isinstance(r.get("base_price"), (int, float)) and r["base_price"] > 0
    ]
    if prices:
        return min(prices)
    try:
        return int(row["price_from"]) if row.get("price_from") else None
    except (TypeError, ValueError):
        return None


def _partner_of(row: dict) -> dict:
    """The joined partner row. PostgREST returns a to-one embed as an object,
    but a list when it resolves the relationship as to-many — accept both."""
    partner = row.get("partner") or {}
    if isinstance(partner, list):
        partner = partner[0] if partner else {}
    return partner if isinstance(partner, dict) else {}


def _to_hotel(row: dict, price: int, rooms: list[dict],
              stats: tuple[float, int] | None, units_left: int | None) -> dict:
    partner = _partner_of(row)
    content = row.get("content") or {}
    photos = _photo_urls(row)

    hotel: dict[str, Any] = {
        # identity — `id` is the listing id, so a selection made in the
        # workspace round-trips to the booking flow without a lookup.
        "id": str(row["id"]),
        "hotel_id": str(row["id"]),
        "name": row.get("title"),
        "source": PARTNER_SOURCE,
        "provider": PARTNER_SOURCE,
        "currency": "INR",
        # pricing
        "price_per_night": price,
        "price": price,
        "price_unit": row.get("price_unit") or "per_night",
        **_discount(row, price),
        # where
        "address": row.get("address") or row.get("city") or row.get("destination"),
        "city": row.get("city") or row.get("destination"),
        "lat": row.get("lat"),
        "lon": row.get("lon"),
        # what
        "property_type": row.get("category") or "homestay",
        "description": row.get("summary") or row.get("description"),
        "amenities": [a for a in (row.get("amenities") or []) if isinstance(a, str)][:12],
        "photos": photos[:6],
        "checkin_time": content.get("checkin"),
        "checkout_time": content.get("checkout"),
        "free_cancellation": _free_cancellation(row),
        # how it's booked — an internal page, not an OTA hand-off
        "booking_url": f"{settings.site_url.rstrip('/')}/stays/{row['slug']}",
        "booking_mode": row.get("booking_mode") or "whatsapp",
        # the tag itself
        "is_partner": True,
        "partner_name": partner.get("brand_name") or "Safar",
        "partner_slug": partner.get("slug"),
        "partner_verified": bool(partner.get("verified")),
        "listing_id": str(row["id"]),
        "listing_slug": row.get("slug"),
        "room_types": [
            {"id": r.get("id"), "name": r.get("name"), "price": r.get("base_price"),
             "occupancy": r.get("occupancy")}
            for r in rooms[:6]
        ],
    }
    hotel.update(_review_fields(stats))
    if units_left is not None and 0 < units_left <= 3:
        hotel["available_rooms"] = units_left
    return {k: v for k, v in hotel.items() if v not in (None, [], "")}


# ── availability ─────────────────────────────────────────────────────────────

async def _bookable(row: dict, checkin: str, checkout: str,
                    rooms_wanted: int) -> tuple[dict, list[dict], int | None] | None:
    """One listing's room types + availability. None when it can't take the stay."""
    listing_id = str(row["id"])
    types, check = await asyncio.gather(
        room_types(listing_id),
        check_stay(listing_id, checkin, checkout, rooms_wanted),
        return_exceptions=True,
    )
    if isinstance(types, Exception):
        types = []
    if isinstance(check, Exception) or not isinstance(check, dict):
        check = {"ok": True, "units_left": None}
    if not check.get("ok"):
        return None
    return row, types, check.get("units_left")


# ── public entry points ──────────────────────────────────────────────────────

async def partner_stays(
    destination: str,
    checkin: str,
    checkout: str,
    rooms: int = 1,
    price_min: float | None = None,
    price_max: float | None = None,
    limit: int = MAX_PARTNER_STAYS,
) -> list[dict]:
    """
    Bookable partner stays for a destination and date range, best first.

    Best = verified partners, then reviewed ones, then the cheapest. Commission,
    subscription plan and `is_featured` are deliberately NOT read: which partner
    leads the pinned group is decided by what the traveller would care about,
    even though the group itself is placed for commercial reasons.
    """
    ci, co = _as_date(checkin), _as_date(checkout)
    if not destination or not ci or not co or co <= ci:
        return []

    try:
        candidates = await run_db(lambda: _fetch_candidates(destination))
    except Exception:  # noqa: BLE001 — never break a hotel search
        logger.warning("[partner-stays] candidate fetch failed for %r", destination, exc_info=True)
        return []

    renderable = [
        r for r in candidates
        if r.get("slug") and _photo_urls(r) and (r.get("price_from") or 0) > 0
    ]
    if not renderable:
        return []

    # Each survivor costs two DB round-trips to check, and a trip generation
    # searches several cities at once. Only a few rows can be shown, so check a
    # shortlist — verified partners first, then the cheapest, both known from
    # the row we already have.
    renderable.sort(key=lambda r: (
        not _partner_of(r).get("verified"),
        r.get("price_from") or 10**9,
    ))
    renderable = renderable[:_AVAILABILITY_CHECK_CAP]

    try:
        checked = await asyncio.gather(
            *(_bookable(r, checkin, checkout, rooms) for r in renderable),
            return_exceptions=True,
        )
        stats = await run_db(
            lambda: _fetch_review_stats([str(r["id"]) for r in renderable])
        )
    except Exception:  # noqa: BLE001
        logger.warning("[partner-stays] availability pass failed for %r", destination, exc_info=True)
        return []

    out: list[dict] = []
    for item in checked:
        if isinstance(item, Exception) or item is None:
            continue
        row, types, units_left = item
        price = _price_of(row, types)
        if not price:
            continue
        # Explicit price filters are the traveller's own words; a pinned row
        # that ignores them reads as an ad, not a result.
        if price_min is not None and price < price_min:
            continue
        if price_max is not None and price > price_max:
            continue
        out.append(_to_hotel(row, price, types, stats.get(str(row["id"])), units_left))

    out.sort(key=lambda h: (
        not h.get("partner_verified"),
        -(h.get("review_score") or 0),
        h.get("price_per_night") or 10**9,
    ))
    if out:
        logger.info("[partner-stays] %r → %d bookable partner stay(s)", destination, len(out))
    return out[:limit]


def is_partner(hotel: dict) -> bool:
    return bool(hotel.get("is_partner"))


def pin_partners(hotels: list[dict]) -> list[dict]:
    """
    Move partner rows to the front, keeping every row's relative order.

    Call this after ANY re-sort of a merged hotel list. Sorting by price or
    distance would otherwise bury the one stay we can actually book for the
    traveller, and the placement would silently depend on which screen they
    happened to open.
    """
    if not hotels:
        return hotels
    partners = [h for h in hotels if is_partner(h)]
    if not partners:
        return hotels
    return partners + [h for h in hotels if not is_partner(h)]


def merge_partner_stays(hotels: list[dict], partners: list[dict]) -> list[dict]:
    """Prepend partner rows, dropping any OTA row for the same property.

    A partner who also sells on Booking.com would otherwise appear twice, once
    at our price and once at the OTA's — the version we can book wins.
    """
    if not partners:
        return hotels

    def _key(name: str | None) -> str:
        return "".join(c for c in (name or "").lower() if c.isalnum())[:24]

    taken = {_key(p.get("name")) for p in partners}
    return partners + [h for h in hotels if _key(h.get("name")) not in taken]


def clear_cache() -> None:
    """Test hook — the candidate memo is process-local."""
    with _cache_lock:
        _cache.clear()
