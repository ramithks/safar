"""
Flight aggregator service.

Sources (parallel): Booking.com + EaseMyTrip
Merges results, deduplicates by flight_number+departure_time, sorts by price.

Usage:
    from app.services.flights import FlightService
    svc = FlightService()
    result = await svc.search(
        origin="Delhi",
        destination="Leh",
        date="2026-06-15",
        adults=1,
    )
    # result = {"flights": [...], "from_iata": "DEL", "to_iata": "IXL", "resolved": bool}
"""
import asyncio
from app.services.booking.flights import search_flights as booking_search_flights, city_to_iata
from app.services.easemytrip.flights import search_flights as emt_search_flights


class FlightService:
    """Unified flight search: Booking.com + EaseMyTrip (parallel, deduplicated, price-sorted)."""

    def resolve_iata(self, city_name: str) -> str | None:
        return city_to_iata(city_name)

    async def search(
        self,
        origin: str,
        destination: str,
        date: str,              # "YYYY-MM-DD"
        adults: int = 1,
        cabin_class: str = "ECONOMY",
        sort: str = "CHEAPEST",
        limit: int = 5,
    ) -> dict:
        """
        Search one-way flights from origin → destination.
        Returns {"flights": [...], "from_iata": str, "to_iata": str, "resolved": bool}
        """
        from_iata = self.resolve_iata(origin)
        to_iata   = self.resolve_iata(destination)

        if not from_iata or not to_iata:
            return {
                "flights": [], "from_iata": from_iata,
                "to_iata": to_iata, "resolved": False,
            }

        booking_r, emt_r = await asyncio.gather(
            booking_search_flights(
                from_iata, to_iata, date,
                adults=adults, cabin_class=cabin_class, sort=sort, limit=limit,
            ),
            emt_search_flights(
                from_iata, to_iata, date,
                adults=adults, cabin_class=cabin_class.lower(), limit=limit,
            ),
        )

        seen: set[str] = set()
        merged: list[dict] = []
        for flight in (booking_r or []) + (emt_r or []):
            key = flight.get("flight_number", "") + str(flight.get("departure_time", ""))
            if key and key in seen:
                continue
            seen.add(key)
            merged.append(flight)

        merged.sort(key=lambda f: f.get("price_total") or 999_999)

        return {
            "flights": merged[:limit],
            "from_iata": from_iata,
            "to_iata": to_iata,
            "resolved": True,
        }
