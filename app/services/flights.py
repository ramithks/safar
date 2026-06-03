"""
Flight aggregator service.

Sources (parallel): Booking.com + EaseMyTrip
Merges results, deduplicates by flight_number+departure_time, sorts by price.

Usage:
    from app.services.flights import FlightService
    svc = FlightService()
    result = await svc.search(origin="Delhi", destination="Leh", date="2026-06-15")
    # Pass from_iata/to_iata to skip re-resolution when pre-resolved upstream.
"""
import asyncio
from app.services.booking.flights import (
    search_flights as booking_search_flights,
    resolve_iata,
    city_to_iata,
)
from app.services.easemytrip.flights import search_flights as emt_search_flights
from app.core.circuit_breaker import breaker_booking, breaker_emt


async def _noop(value):
    return value


class FlightService:
    """Unified flight search: Booking.com + EaseMyTrip (parallel, deduplicated, price-sorted)."""

    async def resolve_iata(self, city_name: str) -> str | None:
        """Async IATA resolution — cache first, then API autocomplete."""
        return await resolve_iata(city_name)

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,
        adults: int = 1,
        cabin_class: str = "ECONOMY",
        sort: str = "CHEAPEST",
        limit: int = 5,
        from_iata: str | None = None,
        to_iata: str | None = None,
        nonstop_only: bool = False,
    ) -> dict:
        """
        Search one-way flights from origin → destination.
        Pass from_iata/to_iata to skip re-resolution when pre-resolved upstream.
        Returns {"flights": [...], "from_iata": str, "to_iata": str, "resolved": bool}
        """
        from_iata, to_iata = await asyncio.gather(
            _noop(from_iata) if from_iata else resolve_iata(origin),
            _noop(to_iata)   if to_iata   else resolve_iata(destination),
        )

        if not from_iata or not to_iata:
            return {
                "flights": [], "from_iata": from_iata,
                "to_iata": to_iata, "resolved": False,
            }

        booking_r, emt_r = await asyncio.gather(
            breaker_booking.call(
                booking_search_flights, from_iata, to_iata, date,
                adults=adults, cabin_class=cabin_class, sort=sort, limit=limit,
                fallback=[],
            ),
            breaker_emt.call(
                emt_search_flights, from_iata, to_iata, date,
                adults=adults, cabin_class=cabin_class.lower(), limit=limit,
                nonstop_only=nonstop_only,
                fallback=[],
            ),
            return_exceptions=True,
        )

        def _src_status(result) -> tuple[list, str]:
            if isinstance(result, Exception):
                return [], f"✗ {str(result)[:120]}"
            return (result, f"✓ {len(result)}") if result else ([], "✗ 0 results")

        booking_flights, booking_status = _src_status(booking_r)
        emt_flights,     emt_status     = _src_status(emt_r)

        print(
            f"[flights] {from_iata}→{to_iata} ({date}) | "
            f"Booking: {booking_status} | EaseMyTrip: {emt_status}"
        )

        seen: set[str] = set()
        merged: list[dict] = []
        for flight in booking_flights + emt_flights:
            key = flight.get("flight_number", "") + str(flight.get("departure_time", ""))
            if key and key in seen:
                continue
            seen.add(key)
            merged.append(flight)

        if nonstop_only:
            merged = [f for f in merged if (f.get("stops") or 0) == 0]

        # Priced flights first, then unpriced (EMT schedule-only)
        merged.sort(key=lambda f: (f.get("price_total") is None, f.get("price_total") or 999_999))

        return {
            "flights": merged[:limit],
            "from_iata": from_iata,
            "to_iata": to_iata,
            "resolved": True,
        }
