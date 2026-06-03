"""
Hotel aggregator service — parallel multi-source search.

Sources searched simultaneously:
  1. EaseMyTrip  — best for Indian domestic inventory, real prices
  2. Booking.com — widest global inventory, live prices
  3. Expedia     — offbeat/rural destinations

Results are filtered to hotels within max_distance_km of the destination,
deduplicated by name, and sorted richest-first (booking_url + price + photos).
"""
import asyncio
import urllib.parse
from app.services.easemytrip.hotels import search_hotels as emt_search_hotels
from app.services.booking.hotels import search_hotels as booking_search_hotels
from app.services.expedia.hotels import search_hotels as expedia_search_hotels
from app.core.circuit_breaker import breaker_emt, breaker_booking, breaker_expedia

_BUDGET_SORT_EMT = {
    "low":  "Price|ASC",
    "mid":  "Popular|DESC",
    "high": "Rating|DESC",
}
_BUDGET_SORT_BOOKING = {
    "low":  "price",
    "mid":  None,
    "high": "class",
}
_BUDGET_SORT_EXPEDIA = {
    "low":  "PRICE_LOW_TO_HIGH",
    "mid":  "RECOMMENDED",
    "high": "REVIEW_RELEVANT",
}
_BUDGET_QUERY = {
    "low":  "budget affordable hotels homestay",
    "mid":  "mid-range 3-star hotels",
    "high": "luxury 5-star hotels resort",
}


class HotelService:
    """
    Unified hotel search: EaseMyTrip + Booking.com + Expedia in parallel.
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
        limit: int = 10,
        max_distance_km: float = 20.0,
    ) -> dict:
        from datetime import datetime
        try:
            nights = (datetime.strptime(checkout, "%Y-%m-%d") - datetime.strptime(checkin, "%Y-%m-%d")).days
        except Exception:
            nights = 1

        emt_sort     = _BUDGET_SORT_EMT.get(budget, "Popular|DESC")
        bkg_sort     = _BUDGET_SORT_BOOKING.get(budget)
        exp_sort     = _BUDGET_SORT_EXPEDIA.get(budget, "RECOMMENDED")
        per_src      = max(6, limit)   # fetch slightly more per source so dedup has room

        # Search all three sources simultaneously (circuit-breaker-wrapped)
        emt_r, booking_r, expedia_r = await asyncio.gather(
            breaker_emt.call(
                emt_search_hotels, destination, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                limit=per_src, sort=emt_sort, fallback=[],
            ),
            breaker_booking.call(
                booking_search_hotels, destination, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                sort=bkg_sort, limit=per_src, fallback=[],
            ),
            breaker_expedia.call(
                expedia_search_hotels, destination, checkin, checkout,
                rooms=rooms, adults=adults, limit=per_src, sort=exp_sort, fallback=[],
            ),
            return_exceptions=True,
        )

        all_hotels: list[dict] = []
        sources: list[str] = []

        def _src_status(result, name: str) -> str:
            if isinstance(result, Exception):
                return f"✗ {str(result)[:120]}"
            if result:
                budget_filtered = _filter_by_budget(result, budget)
                nearby = _filter_by_distance(budget_filtered, max_distance_km)
                all_hotels.extend(nearby)
                sources.append(name)
                return f"✓ {len(nearby)} results"
            return "✗ 0 results"

        emt_status     = _src_status(emt_r,     "easemytrip")
        booking_status = _src_status(booking_r,  "booking")
        expedia_status = _src_status(expedia_r,  "expedia")

        print(
            f"[hotels] {destination!r} | "
            f"EaseMyTrip: {emt_status} | "
            f"Booking: {booking_status} | "
            f"Expedia: {expedia_status}"
        )

        if not all_hotels:
            return {"hotels": [], "source": "none", "nights": nights}

        deduped = _deduplicate_by_name(all_hotels)
        hotels_with_map = [_add_map_url(h) for h in deduped[:limit]]
        return {
            "hotels": hotels_with_map,
            "source": "+".join(sources),
            "nights": nights,
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


def _filter_by_budget(hotels: list[dict], budget: str) -> list[dict]:
    """Soft filter — prefer hotels in the budget tier, but never return empty."""
    if budget == "low":
        filtered = [h for h in hotels if (h.get("price_per_night") or 0) <= 2500]
    elif budget == "high":
        filtered = [h for h in hotels if (h.get("price_per_night") or 0) >= 5000]
    else:
        filtered = [h for h in hotels if 1000 <= (h.get("price_per_night") or 0) <= 10000]
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
