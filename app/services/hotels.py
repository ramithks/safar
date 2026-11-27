"""
Hotel aggregator service — parallel multi-source search.

Sources searched simultaneously:
  1. EaseMyTrip  — best for Indian domestic inventory, real prices
  2. Booking.com — widest global inventory, live prices

Results are filtered to hotels within max_distance_km of the destination,
deduplicated by name, and sorted richest-first (booking_url + price + photos).

Safar's own partner stays are then PINNED above that merged list (see
app/services/partner_stays). They are not scored against OTA rows and never
enter the ranking — they are a small, tagged group on top, because they are the
only stays we can actually take a booking for. Injecting them here rather than
per-endpoint means every surface (quick search, planner, workspace, mobile)
gets them from one place.
"""
import asyncio
import logging
import urllib.parse
from app.services.easemytrip.hotels import search_hotels as emt_search_hotels

logger = logging.getLogger(__name__)
from app.services.booking.hotels import search_hotels as booking_search_hotels
from app.services.agoda.hotels import search_hotels as agoda_search_hotels
from app.services.ixigo.hotels import search_hotels as ixigo_search_hotels
from app.core.circuit_breaker import (
    breaker_emt, breaker_booking, breaker_agoda, breaker_ixigo,
)
from app.core.metrics import METRICS
from app.services.scoring import score_hotels
from app.services.hotels_filters import (
    budget_to_hotel_sort, derive_travel_type, preferred_types,
    filter_and_rank_by_persona, sanitize_hotel_filters, apply_hotel_filters,
    sort_hotels,
)
from app.services.hotels_destination import normalize_destination, distance_km_for
from app.services.partner_stays import (
    PARTNER_SOURCE, merge_partner_stays, partner_stays,
)

# Fixed per-source fetch size. No user-facing limit — each API still needs a page
# size (Booking self-caps at 25); the merged list is returned in full.
_PER_SOURCE = 40

# Partner inventory is two indexed reads plus an availability check per listing.
# It runs beside the scrapers, so this ceiling only ever fires if Supabase is
# in trouble — and then the OTA list must still ship on time.
_PARTNER_TIMEOUT_S = 6.0

# Per-night price thresholds for soft budget filtering, by currency
_BUDGET_PRICE_RANGE: dict[str, dict[str, tuple[int, int]]] = {
    "inr": {"low": (0, 2500),   "mid": (1000, 10000),  "high": (5000, 999_999)},
    "usd": {"low": (0, 60),     "mid": (40,   200),    "high": (120,  999_999)},
    "eur": {"low": (0, 55),     "mid": (35,   180),    "high": (100,  999_999)},
    "gbp": {"low": (0, 50),     "mid": (30,   160),    "high": (90,   999_999)},
    "jpy": {"low": (0, 8000),   "mid": (5000, 25000),  "high": (15000,999_999)},
    "aud": {"low": (0, 90),     "mid": (60,   250),    "high": (150,  999_999)},
    "aed": {"low": (0, 220),    "mid": (120,  600),    "high": (350,  999_999)},
    "sgd": {"low": (0, 80),     "mid": (50,   220),    "high": (120,  999_999)},
}


class HotelService:
    """
    Unified hotel search: EaseMyTrip + Booking.com in parallel.
    Merges, deduplicates, and returns the richest combined list.
    """

    async def search(
        self,
        destination: str,
        checkin: str,
        checkout: str,
        rooms: int = 1,
        adults: int = 2,
        children: int = 0,
        budget: str = "mid",
        travel_type: str | None = None,
        sort: str | None = None,
        max_distance_km: float | None = None,
        currency_code: str = "inr",
        filters: dict | None = None,
    ) -> dict:
        from datetime import datetime
        try:
            nights = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days
        except Exception:
            nights = 1

        # Canonical destination: one query string for every source (EMT has no
        # autocomplete) + a class that adapts the distance radius.
        dest  = normalize_destination(destination)
        city  = dest.query

        # Explicit user filters (canonical vocabulary, see hotels_filters) are
        # HARD and override the persona/budget soft heuristics they overlap:
        # explicit property types replace the persona matrix, an explicit price
        # range disables the budget tier's price band, an explicit sort beats
        # the budget→sort mapping.
        filters        = sanitize_hotel_filters(filters)
        explicit_types = filters.get("property_types")
        explicit_price = "price_min" in filters or "price_max" in filters

        # Persona + budget drive property type; budget drives sort (budget wins).
        persona      = derive_travel_type(adults, travel_type)
        ptypes       = explicit_types or preferred_types(persona, budget)
        canonical_sort = filters.get("sort") or budget_to_hotel_sort(budget) or sort or "recommended"

        # Our own inventory, fetched alongside the scrapers so it costs no extra
        # wall time. `dest.query` is the canonical lowercase city, which is what
        # partners type into their listing's city/destination.
        partners_task = asyncio.create_task(
            partner_stays(
                city or destination, checkin, checkout, rooms=rooms,
                price_min=filters.get("price_min"), price_max=filters.get("price_max"),
            )
        )

        emt_r, booking_r, agoda_r, ixigo_r = await asyncio.gather(
            breaker_emt.call(
                emt_search_hotels, city, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                limit=_PER_SOURCE, sort=canonical_sort, property_types=ptypes, fallback=[],
            ),
            breaker_booking.call(
                booking_search_hotels, city, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                sort=canonical_sort, limit=_PER_SOURCE, property_types=ptypes, fallback=[],
            ),
            breaker_agoda.call(
                agoda_search_hotels, city, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                limit=_PER_SOURCE, sort=canonical_sort, property_types=ptypes,
                travel_type=persona, fallback=[],
            ),
            breaker_ixigo.call(
                ixigo_search_hotels, city, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                limit=_PER_SOURCE, sort=canonical_sort, property_types=ptypes, fallback=[],
            ),
            return_exceptions=True,
        )

        all_hotels: list[dict] = []
        sources: list[str] = []

        def _src_status(result, name: str) -> str:
            # Budget filter per source; distance filtering happens once after merge
            # so its radius can adapt and widen on sparse results.
            if isinstance(result, Exception):
                return f"✗ error"
            if not result:
                METRICS.record_search(name, raw_count=0, deduped_count=0)
                return "✗ 0"
            raw = len(result)
            # Per-provider contribution to this search (drives avg_result_count).
            METRICS.record_search(name, raw_count=raw, deduped_count=raw)
            # With explicit filters the budget band moves AFTER the hard filters
            # (see below) — pre-dropping here would starve them: a mid-tier band
            # would remove the only ₹16k sea-view resort before the facility
            # filter ever saw it.
            budget_filtered = result if filters else _filter_by_budget(result, budget, currency_code)
            all_hotels.extend(budget_filtered)
            sources.append(name)
            return f"✓ {len(budget_filtered)}/{raw}"

        emt_status     = _src_status(emt_r,    "easemytrip")
        booking_status = _src_status(booking_r, "booking")
        agoda_status   = _src_status(agoda_r,  "agoda")
        ixigo_status   = _src_status(ixigo_r,  "ixigo")

        # Adaptive distance radius by destination class (override wins if passed).
        base_km = max_distance_km if max_distance_km is not None else distance_km_for(dest.dest_class)

        logger.info(
            "[hotels] %r→%r [%s, %.0fkm] | EaseMyTrip: %s | Booking: %s | Agoda: %s | Ixigo: %s",
            destination, city, dest.dest_class, base_km,
            emt_status, booking_status, agoda_status, ixigo_status,
        )

        partners = await self._await_partners(partners_task)

        if not all_hotels:
            # Every OTA came back empty (or broke). Partner inventory is still
            # bookable, so a screen with our own stays beats an empty one.
            return {"hotels": partners, "source": PARTNER_SOURCE if partners else "none",
                    "nights": nights,
                    "travel_type": persona, "property_types": ptypes,
                    "filters_applied": filters or None, "filters_relaxed": False,
                    "partner_count": len(partners),
                    "destination": dest.display_name, "dest_class": dest.dest_class}

        total_raw = len(all_hotels)
        deduped = _deduplicate_by_name(all_hotels)
        # Cross-source duplicate ratio for this search (observability).
        METRICS.record_search("hotels_merge", raw_count=total_raw, deduped_count=len(deduped))
        # Context-aware distance filter (widens automatically if too few survive).
        nearby = _filter_by_distance_adaptive(deduped, base_km)
        # Weighted ranking: attach quality_score + rank_score and order by the
        # composite (price+rating+reviews+distance+provider+images+amenities+
        # freshness) — replaces the old richness-only ordering.
        scored = score_hotels(nearby)
        # Hard user filters FIRST — the soft persona/budget shaping below must
        # never starve an explicit request (e.g. couple/mid prefers hotels, but
        # "free cancellation" may only match a hostel). If nothing survives,
        # fall back to the unfiltered pool with filters_relaxed=True so callers
        # can say so instead of showing an empty screen.
        filtered = apply_hotel_filters(scored, filters)
        relaxed = bool(filters) and not filtered
        if relaxed:
            filtered = scored
        # Soft persona filter: keep the persona's preferred property types (never
        # empties); a STABLE sort by persona tier preserves rank_score order within
        # each tier, so preferred types lead and the best-scored lead within them.
        # Explicit property types skip it — the hard filter above was the request.
        if not explicit_types:
            filtered = filter_and_rank_by_persona(filtered, persona, budget)
        if filters and not explicit_price and not relaxed:
            # Budget band was skipped per-source when filters exist; re-apply it
            # softly (never empties) so the tier still shapes prices unless the
            # user set an explicit price range.
            filtered = _filter_by_budget(filtered, budget, currency_code)
        if filters.get("sort"):
            filtered = sort_hotels(filtered, filters["sort"])
        hotels_with_map = [_add_map_url(h) for h in filtered]

        # Grow the hotels master table with whatever came back — async, best-effort,
        # so EVERY hotel fetch persists (workspace copilot search included), not just
        # the /plan path. Idempotent upsert on (source, source_hotel_id).
        self._persist_master(hotels_with_map, dest.display_name)

        # Pinned LAST, and after _persist_master on purpose: partner listings are
        # our own rows, not scraped OTA inventory, and must never be written into
        # the hotels master table as if they were.
        merged = merge_partner_stays(hotels_with_map, partners)

        return {
            "hotels": merged,
            "source": "+".join(sources + ([PARTNER_SOURCE] if partners else [])),
            "nights": nights,
            "travel_type": persona,
            "property_types": ptypes,
            "filters_applied": filters or None,
            "filters_relaxed": relaxed,
            "partner_count": len(partners),
            "destination": dest.display_name,
            "dest_class": dest.dest_class,
        }

    @staticmethod
    async def _await_partners(task) -> list[dict]:
        """Partner inventory is additive — a failure or a slow read gives an
        empty list and the OTA results ship exactly as before."""
        try:
            return await asyncio.wait_for(task, timeout=_PARTNER_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            task.cancel()
            logger.warning("[hotels] partner stays unavailable: %s", type(exc).__name__)
            return []


    @staticmethod
    def _persist_master(hotels: list[dict], destination: str) -> None:
        try:
            import asyncio
            from app.services.hotel_store import save_hotels_master
            asyncio.create_task(save_hotels_master(hotels, destination))
        except RuntimeError:
            pass  # no running loop (e.g. sync context) — skip, non-critical
        except Exception:  # noqa: BLE001 — persistence must never break search
            pass


def _add_map_url(hotel: dict) -> dict:
    """Inject a Google Maps search URL using hotel name and address."""
    name    = hotel.get("name", "")
    address = hotel.get("address") or hotel.get("location") or ""
    query   = f"{name} {address}".strip() if address else name
    hotel["map_url"] = (
        f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"
    )
    return hotel


def _filter_by_distance_adaptive(hotels: list[dict], base_km: float,
                                 min_results: int = 6) -> list[dict]:
    """
    Distance filter with automatic widening: try the class base radius, then 2x,
    then 3x; if even that is too sparse, keep everything. Prevents a tight radius
    (e.g. a hill town) from emptying the list while still favouring nearby hotels.
    """
    for mult in (1.0, 2.0, 3.0):
        kept = _filter_by_distance(hotels, base_km * mult)
        if len(kept) >= min_results:
            return kept
    return hotels


def _filter_by_distance(hotels: list[dict], max_km: float) -> list[dict]:
    """
    Keep only hotels within max_km of the destination.
    Hotels without a distance field are kept (benefit of doubt).
    """
    result = []
    for h in hotels:
        km = h.get("distance_km") or h.get("distanceKM")
        if km is None:
            result.append(h)
        else:
            try:
                if float(km) <= max_km:
                    result.append(h)
            except Exception:
                result.append(h)
    return result


def _filter_by_budget(hotels: list[dict], budget: str, currency_code: str = "inr") -> list[dict]:
    """
    Soft filter — prefer hotels in the budget tier, but never return empty.
    Thresholds are currency-aware so USD/EUR searches aren't filtered by INR values.
    """
    safe_code = (currency_code or "inr").lower()
    ranges = _BUDGET_PRICE_RANGE.get(safe_code) or _BUDGET_PRICE_RANGE["inr"]
    lo, hi = ranges.get(budget, ranges["mid"])
    filtered = [h for h in hotels if lo <= (h.get("price_per_night") or 0) <= hi]
    return filtered if filtered else hotels


# Metadata carried across duplicate providers onto the representative row when it's
# missing the field. Includes the enriched signals the parsers now emit (subscores,
# pricing story, scarcity, social proof, landmarks) — a hotel that's bare on the
# cheapest provider still shows the richest provider's metadata. Price + booking_url
# are NEVER borrowed (they must stay from the same provider), so what we display
# always matches the link we send the user to.
_MERGE_FIELDS = (
    # original set
    "amenities", "review_label", "free_cancellation", "free_cancellation_until",
    "meal_plan", "highlights", "couple_friendly", "distance_km",
    "no_prepayment", "is_breakfast_included", "checkin_time", "checkout_time",
    # rating/identity (fill if the cheap provider didn't carry them)
    "review_score", "review_count", "star_rating", "property_type", "description",
    "address", "lat", "lon",
    # enriched signals (Agoda/Booking parser enrichment)
    "subscores", "recommendation_score", "review_snippet", "landmarks",
    "nearest_transport", "room_facilities", "has_private_pool", "room_size_sqm",
    "free_wifi", "social_proof", "people_looking", "badges", "top_selling_point",
    "is_popular", "family_friendly", "has_airport_transfer",
    "price_original", "savings", "discount_pct", "discount_label", "available_rooms",
)


def _hotel_price(h: dict) -> float | None:
    try:
        v = h.get("price_per_night") or h.get("estimated_price_per_night")
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _deduplicate_by_name(hotels: list[dict]) -> list[dict]:
    """
    Collapse the same hotel listed by multiple sources into one row.

    Representative = the CHEAPEST *bookable* listing (booking_url + a live price), so
    a budget traveller isn't shown a pricier duplicate just because it had richer
    metadata — and the price we display always matches the link we send them to.
    Ties, and groups with no bookable listing, fall back to the RICHEST listing.
    Every other listing's metadata is then unioned onto the representative
    (``_MERGE_FIELDS`` + photos up to 6), so no scraped signal is discarded.
    """
    def _richness(h: dict) -> int:
        return (
            bool(h.get("booking_url")) * 10
            + bool(_hotel_price(h)) * 5
            + min(len(h.get("photos") or []), 5)
            + bool(h.get("address")) * 2
            + bool(h.get("review_score") or h.get("star_rating") or h.get("google_rating"))
        )

    def _bookable(h: dict) -> bool:
        return bool(h.get("booking_url")) and (_hotel_price(h) or 0) > 0

    def _norm(name: str) -> str:
        """Normalize hotel name for dedup — strip spaces/punctuation, first 24 chars."""
        return "".join(c for c in (name or "").lower() if c.isalnum())[:24]

    def _better(cand: dict, cur: dict) -> bool:
        """Is `cand` a better representative of the same hotel than `cur`?
        Cheapest bookable wins (consistent price↔link); else richest."""
        cb, rb = _bookable(cand), _bookable(cur)
        if cb != rb:
            return cb                      # a bookable listing beats a non-bookable one
        if cb and rb:
            pc, pr = _hotel_price(cand), _hotel_price(cur)
            if pc is not None and pr is not None and pc != pr:
                return pc < pr             # both bookable → the cheaper one represents
        return _richness(cand) > _richness(cur)

    def _absorb(winner: dict, other: dict) -> None:
        for field in _MERGE_FIELDS:
            if not winner.get(field) and other.get(field) not in (None, "", [], {}):
                winner[field] = other[field]
        w_photos = winner.get("photos") or []
        if len(w_photos) < 6:
            for p in (other.get("photos") or []):
                if p not in w_photos:
                    w_photos.append(p)
                if len(w_photos) >= 6:
                    break
            winner["photos"] = w_photos

    # Group by normalized name, preserving first-seen order.
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for h in hotels:
        key = _norm(h.get("name", ""))
        if not key:
            continue
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(h)

    result: list[dict] = []
    for key in order:
        members = groups[key]
        winner = members[0]
        for m in members[1:]:
            if _better(m, winner):
                winner = m
        for m in members:
            if m is not winner:
                _absorb(winner, m)
        result.append(winner)

    # Richest first (a stable starting order; score_hotels re-sorts by rank_score next).
    result.sort(key=_richness, reverse=True)
    return result
