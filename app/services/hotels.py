"""
Hotel aggregator service — parallel multi-source search.

Sources searched simultaneously:
  1. EaseMyTrip  — best for Indian domestic inventory, real prices
  2. Booking.com — widest global inventory, live prices
  3. Expedia     — offbeat/rural destinations
  4. Google Places — fallback when all three fail (name/rating only, no live prices)

Results from all successful sources are merged, deduplicated by name,
and sorted so the richest (booking_url + price + photos) entries surface first.
"""
import asyncio
from app.services.easemytrip.hotels import search_hotels as emt_search_hotels
from app.services.booking.hotels import search_hotels as booking_search_hotels
from app.services.expedia.hotels import search_hotels as expedia_search_hotels
from app.services.places import search_places

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

        # Search all three sources simultaneously
        emt_r, booking_r, expedia_r = await asyncio.gather(
            emt_search_hotels(
                destination, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                limit=per_src, sort=emt_sort,
            ),
            booking_search_hotels(
                destination, checkin, checkout,
                rooms=rooms, adults=adults, children=children,
                sort=bkg_sort, limit=per_src,
            ),
            expedia_search_hotels(
                destination, checkin, checkout,
                rooms=rooms, adults=adults, limit=per_src, sort=exp_sort,
            ),
            return_exceptions=True,
        )

        all_hotels: list[dict] = []
        sources: list[str] = []

        if emt_r and not isinstance(emt_r, Exception):
            filtered = _filter_by_budget(emt_r, budget)
            all_hotels.extend(filtered)
            sources.append("easemytrip")
            print(f"[hotels] easemytrip: {len(filtered)} results for '{destination}'")
        else:
            if isinstance(emt_r, Exception):
                print(f"[hotels] easemytrip error: {emt_r}")

        if booking_r and not isinstance(booking_r, Exception):
            filtered = _filter_by_budget(booking_r, budget)
            all_hotels.extend(filtered)
            sources.append("booking")
            print(f"[hotels] booking: {len(filtered)} results for '{destination}'")
        else:
            if isinstance(booking_r, Exception):
                print(f"[hotels] booking error: {booking_r}")

        if expedia_r and not isinstance(expedia_r, Exception):
            filtered = _filter_by_budget(expedia_r, budget)
            all_hotels.extend(filtered)
            sources.append("expedia")
            print(f"[hotels] expedia: {len(filtered)} results for '{destination}'")
        else:
            if isinstance(expedia_r, Exception):
                print(f"[hotels] expedia error: {expedia_r}")

        if not all_hotels:
            print(f"[hotels] all sources failed for '{destination}', falling back to Google Places")
            gp_hotels = await self._google_places_fallback(destination, budget, nights)
            return {"hotels": gp_hotels, "source": "google_places", "nights": nights}

        deduped = _deduplicate_by_name(all_hotels)
        return {
            "hotels": deduped[:limit],
            "source": "+".join(sources),
            "nights": nights,
        }

    async def _google_places_fallback(self, destination: str, budget: str, nights: int) -> list[dict]:
        query = f"{_BUDGET_QUERY.get(budget, 'hotels')} in {destination}"
        try:
            results = await search_places(query)
            return [_normalise_gp_hotel(p, nights) for p in results[:8]]
        except Exception:
            return []


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


def _normalise_gp_hotel(place: dict, nights: int) -> dict:
    geo = (place.get("geometry") or {}).get("location", {})
    photos = place.get("photos") or []
    photo_ref = (
        photos[0].get("photo_reference") if photos and isinstance(photos[0], dict)
        else place.get("photo_reference")
    )
    name = place.get("name", "")
    query = f"{name} hotel booking".replace(" ", "+")
    return {
        "hotel_id": place.get("place_id"),
        "name": name,
        "category": "Hotel",
        "star_rating": None,
        "ta_rating": None,
        "address": place.get("formatted_address"),
        "distance_from_dest": None,
        "distance_km": None,
        "lat": geo.get("lat"),
        "lon": geo.get("lng"),
        "price_per_night": None,
        "total_price": None,
        "tax": None,
        "discount": None,
        "currency": "INR",
        "nights": nights,
        "checkin_time": None,
        "checkout_time": None,
        "is_breakfast_included": False,
        "cancellation_policy": None,
        "highlights": None,
        "amenities": [],
        "photos": [photo_ref] if photo_ref else [],
        "booking_url": f"https://www.google.com/search?q={query}",
        "is_sold_out": False,
        "coupon_code": None,
        "coupon_note": None,
        "source": "google_places",
        "google_rating": place.get("rating"),
        "google_reviews": place.get("user_ratings_total"),
    }
