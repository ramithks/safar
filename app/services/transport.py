"""
Transport aggregator service.

Bus: RedBus (primary) + FlixBus (secondary, fills gaps).
Train: ConfirmTKT (India-only).
Flight: Booking.com + EaseMyTrip via FlightService.

Usage:
    from app.services.transport import TransportService
    svc = TransportService()
    result = await svc.search_buses("Delhi", "Manali", "2026-06-15", travelers=2)
    result = await svc.search_trains("Delhi", "Chandigarh", "2026-06-15")
    result = await svc.search_all("Delhi", "Manali", "2026-06-15", travelers=2)
"""
import asyncio
import logging
from app.services.bus.redbus import search_city as redbus_search_city, search_buses as redbus_search_buses
from app.services.bus.flixbus import search_city as flixbus_search_city, search_buses as flixbus_search_buses
from app.services.flights import FlightService
from app.services.railway import RailwayService
from app.core.circuit_breaker import breaker_redbus, breaker_flixbus

logger = logging.getLogger(__name__)

_flights = FlightService()
_railway = RailwayService()


class TransportService:
    """Unified transport search: bus (RedBus+FlixBus), train (ConfirmTKT), flight."""

    async def resolve_bus_city_id(self, city_name: str) -> int | None:
        cities = await redbus_search_city(city_name)
        return _pick_city_id(cities)

    async def search_buses(
        self,
        origin: str,
        destination: str,
        date: str,
        travelers: int = 1,
        limit: int = 30,
    ) -> dict:
        """Search buses: resolves city IDs then delegates to search_buses_direct."""
        from_id, to_id = await asyncio.gather(
            self.resolve_bus_city_id(origin),
            self.resolve_bus_city_id(destination),
        )
        if not from_id or not to_id:
            logger.warning("[buses] %r→%r | city not resolved", origin, destination)
            return {"buses": [], "from_city_id": from_id, "to_city_id": to_id, "resolved": False}
        return await self.search_buses_direct(from_id, to_id, origin, destination, date, travelers, limit)

    async def search_buses_direct(
        self,
        from_id: int,
        to_id: int,
        origin: str,
        destination: str,
        date: str,
        travelers: int = 1,
        limit: int = 30,
        budget: str = "mid",
    ) -> dict:
        """Search buses with pre-resolved RedBus city IDs — skips autocomplete."""
        # Low budget: sort by price to surface cheapest options first
        bus_sort = "price" if budget == "low" else "departure"
        redbus_buses: list[dict] = []
        result = await breaker_redbus.call(
            redbus_search_buses, from_id, to_id, date, limit=limit,
            from_city_name=origin, to_city_name=destination, sort=bus_sort, fallback=[],
        )
        redbus_buses = result or []
        redbus_status = f"✓ {len(redbus_buses)}" if redbus_buses else "✗ 0 results"

        flixbus_buses: list[dict] = []
        flixbus_status = "— skipped (RedBus sufficient)"
        if len(redbus_buses) < limit:
            result = await breaker_flixbus.call(
                self._flixbus_search, origin, destination, date, travelers, fallback=[],
            )
            flixbus_buses = result or []
            flixbus_status = f"✓ {len(flixbus_buses)}" if flixbus_buses else "✗ 0 results"

        logger.info("[buses] %r→%r (%s) | RedBus:%s FlixBus:%s", origin, destination, date, redbus_status, flixbus_status)

        buses = redbus_buses + [b for b in flixbus_buses if b]
        return {
            "buses": buses,
            "from_city_id": from_id,
            "to_city_id": to_id,
            "resolved": True,
        }

    async def _flixbus_search(
        self, origin: str, destination: str, date: str, travelers: int
    ) -> list[dict]:
        from_cities, to_cities = await asyncio.gather(
            flixbus_search_city(origin),
            flixbus_search_city(destination),
        )
        from_id = from_cities[0]["id"] if from_cities else None
        to_id   = to_cities[0]["id"]   if to_cities   else None
        if not from_id or not to_id:
            return []
        return await flixbus_search_buses(from_id, to_id, date, adults=travelers)

    async def search_trains(
        self,
        origin: str,
        destination: str,
        date: str,
        from_code: str | None = None,
        to_code: str | None = None,
    ) -> dict:
        """
        Search Indian trains via ConfirmTKT.
        Caller can provide station codes directly (from Gemini) to skip lookup.
        Returns {"trains": [...], "from_code": str, "to_code": str, "resolved": bool}
        """
        return await _railway.search(
            origin, destination, date,
            from_code=from_code, to_code=to_code,
        )

    async def search_flights(self, origin: str, destination: str, date: str, **kwargs) -> dict:
        return await _flights.search(origin, destination, date, **kwargs)

    async def search_all(
        self,
        origin: str,
        destination: str,
        date: str,
        travelers: int = 1,
        modes: list[str] | None = None,
    ) -> dict:
        """
        Run bus + train + flight searches in parallel.
        modes: subset of ["bus", "train", "flight"] — None means all three.
        Returns {"buses": {...}, "trains": {...}, "flights": {...}}
        """
        want_bus    = not modes or "bus"                    in modes
        want_train  = not modes or "train" in modes or "railway" in modes
        want_flight = not modes or "flight"                 in modes

        tasks = {}
        if want_bus:
            tasks["buses"]   = self.search_buses(origin, destination, date, travelers)
        if want_train:
            tasks["trains"]  = _railway.search(origin, destination, date)
        if want_flight:
            tasks["flights"] = _flights.search(origin, destination, date, adults=travelers)

        keys = list(tasks.keys())
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        output: dict = {}
        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                output[key] = {"resolved": False, "error": str(result)}
            else:
                output[key] = result or {"resolved": False}

        return output


def _pick_city_id(cities: list[dict]) -> int | None:
    if not cities:
        return None
    for c in cities:
        if c.get("type") == "CITY":
            return c["id"]
    for c in cities:
        if c.get("type") == "AREA":
            return c["id"]
    return cities[0]["id"] if cities else None
