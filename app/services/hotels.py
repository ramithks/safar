"""
Hotel aggregator service — parallel multi-source search.

Sources searched simultaneously:
  1. EaseMyTrip  — best for Indian domestic inventory, real prices
  2. Booking.com — widest global inventory, live prices

Results are filtered to hotels within max_distance_km of the destination,
deduplicated by name, and sorted richest-first (booking_url + price + photos).
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
from app.services.hotels_filters import (
    budget_to_hotel_sort, derive_travel_type, preferred_types,
    filter_and_rank_by_persona,
)
from app.services.hotels_destination import normalize_destination, distance_km_for

# Fixed per-source fetch size. No user-facing limit — each API still needs a page
# size (Booking self-caps at 25); the merged list is returned in full.
_PER_SOURCE = 40

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

        # Persona + budget drive property type; budget drives sort (budget wins).
        persona      = derive_travel_type(adults, travel_type)
        ptypes       = preferred_types(persona, budget)
        canonical_sort = budget_to_hotel_sort(budget) or sort or "recommended"

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
                return "✗ 0"
            raw = len(result)
            budget_filtered = _filter_by_budget(result, budget, currency_code)
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

        if not all_hotels:
            return {"hotels": [], "source": "none", "nights": nights,
                    "travel_type": persona, "property_types": ptypes,
                    "destination": dest.display_name, "dest_class": dest.dest_class}

        deduped = _deduplicate_by_name(all_hotels)
        # Context-aware distance filter (widens automatically if too few survive).
        nearby = _filter_by_distance_adaptive(deduped, base_km)
        # Soft persona filter: keep the persona's preferred property types (never
        # empties), preferred types ranked first.
        persona_filtered = filter_and_rank_by_persona(nearby, persona, budget)
        hotels_with_map = [_add_map_url(h) for h in persona_filtered]
        return {
            "hotels": hotels_with_map,
            "source": "+".join(sources),
            "nights": nights,
            "travel_type": persona,
            "property_types": ptypes,
            "destination": dest.display_name,
            "dest_class": dest.dest_class,
        }


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


def _deduplicate_by_name(hotels: list[dict]) -> list[dict]:
    """
    Remove near-duplicate entries (same hotel listed by multiple sources).
    When duplicates exist, keep the entry with the most data:
    booking_url + price_per_night + photos score highest.
    """
    def _richness(h: dict) -> int:
        return (
            bool(h.get("booking_url")) * 10
            + bool(h.get("price_per_night") or h.get("estimated_price_per_night")) * 5
            + min(len(h.get("photos") or []), 5)
            + bool(h.get("address")) * 2
            + bool(h.get("review_score") or h.get("star_rating") or h.get("google_rating"))
        )

    def _norm(name: str) -> str:
        """Normalize hotel name for dedup — strip spaces/punctuation, first 24 chars."""
        return "".join(c for c in (name or "").lower() if c.isalnum())[:24]

    seen: dict[str, dict] = {}
    for h in hotels:
        key = _norm(h.get("name", ""))
        if not key:
            continue
        if key not in seen or _richness(h) > _richness(seen[key]):
            seen[key] = h

    # Sort: richest entries first (booking_url > price > photos)
    result = list(seen.values())
    result.sort(key=_richness, reverse=True)
    return result
