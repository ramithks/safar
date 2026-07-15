"""
Flight aggregator service.

Sources (parallel): Booking.com + EaseMyTrip + Gotogate
Merges results, deduplicates by flight_number+departure_time, sorts by price.

Usage:
    from app.services.flights import FlightService
    svc = FlightService()
    result = await svc.search(origin="Delhi", destination="Leh", date="2026-06-15")
    # Pass from_iata/to_iata to skip re-resolution when pre-resolved upstream.
"""
import asyncio
import logging
import time
from app.services.booking.flights import (
    search_flights as booking_search_flights,
    resolve_iata,
    city_to_iata,
)
from app.services.easemytrip.flights import search_flights as emt_search_flights
from app.services.gotogate.flights import search_flights as gotogate_search_flights
from app.core.circuit_breaker import breaker_booking, breaker_emt, breaker_gotogate
from app.core.metrics import METRICS
from app.services.scoring import score_flights

logger = logging.getLogger(__name__)


async def _noop(value):
    return value


# ── No-service route cache ────────────────────────────────────────────────────
# Many routes have no air service at all (tiny airports like Kullu/KUU, or two
# cities with no direct flights). A single trip hammers each such route THREE
# times — outbound, return, and the multi-hop mirror re-scrape — burning 3
# providers × 3 passes = 9 dead calls. Once all providers agree a route is empty,
# remember it briefly and short-circuit: the return leg and mirror pass then cost
# nothing. Keyed by IATA pair (date-agnostic — a route without service on one day
# has none on adjacent days); only set when providers actually RESPONDED empty,
# never on errors/timeouts (those are transient).
_NO_SERVICE_TTL = 6 * 3600  # seconds
_no_service: dict[str, float] = {}  # "FROM>TO" → epoch when it expires


def _route_key(from_iata: str, to_iata: str) -> str:
    return f"{from_iata}>{to_iata}"


def _is_no_service(from_iata: str, to_iata: str) -> bool:
    exp = _no_service.get(_route_key(from_iata, to_iata))
    if exp is None:
        return False
    if exp < time.monotonic():
        _no_service.pop(_route_key(from_iata, to_iata), None)
        return False
    return True


def _mark_no_service(from_iata: str, to_iata: str) -> None:
    _no_service[_route_key(from_iata, to_iata)] = time.monotonic() + _NO_SERVICE_TTL


# ── Sorting (shared by /search/flights and the /plan journey) ────────────────
# Budget tier → sort intent. The single source of truth so the standalone search
# endpoint and the trip planner order flights identically.
_BUDGET_SORT = {"low": "CHEAPEST", "mid": "BEST", "high": "FASTEST"}


def budget_to_sort(budget: str | None) -> str | None:
    """Map a budget tier (low|mid|high) → sort key, or None if not a known tier."""
    return _BUDGET_SORT.get((budget or "").lower())


def sort_flights(flights: list[dict], sort: str) -> list[dict]:
    """
    Order flights by sort intent. CHEAPEST/FASTEST sort on a single field; BEST
    blends price and time (price dominates by design — kept behaviour-preserving).
    Unpriced flights always sink to the bottom.
    """
    if sort == "FASTEST":
        return sorted(flights, key=lambda f: (f.get("duration_min") or 9999))
    if sort == "BEST":
        # Prefer the weighted composite (rank_score, set by score_flights) when it
        # has been attached — a real multi-factor blend of price, duration, stops,
        # provider, and departure time. Fall back to the legacy price/time blend
        # for any flight that wasn't scored, sinking it below scored flights.
        def _best_key(f: dict) -> tuple[int, float]:
            rs = f.get("rank_score")
            if rs is not None:
                return (0, -rs)  # scored first, higher rank_score first
            price = f.get("price_total") or 99999
            dur   = f.get("duration_min") or 999
            stops = (f.get("stops") or 0) * 60
            return (1, price * 0.6 + (dur + stops) * 0.4)
        return sorted(flights, key=_best_key)
    # default: CHEAPEST
    return sorted(flights, key=lambda f: (f.get("price_total") is None, f.get("price_total") or 99999))


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

        # Known dead route (recently confirmed no service by all providers) —
        # skip the scrape entirely. Saves the return-leg + mirror re-scrapes.
        if _is_no_service(from_iata, to_iata):
            logger.info("[flights] %s→%s: skipped (cached no-service)", from_iata, to_iata)
            return {
                "flights": [], "from_iata": from_iata, "to_iata": to_iata,
                "resolved": True, "no_service": True,
            }

        # Push the requested sort UPSTREAM so each provider returns a slice that
        # already favours it — sorting after a biased page can't recover the
        # candidates a provider never returned. Booking + Gotogate sort natively;
        # EMT has no upstream sort but returns near-full inventory, so reranking it
        # locally is safe. A final merge-wide rerank by `sort` happens below.
        eff_sort = (sort or "CHEAPEST").upper()
        timed_out = False
        try:
            booking_r, emt_r, gotogate_r = await asyncio.wait_for(
                asyncio.gather(
                    breaker_booking.call(
                        booking_search_flights, from_iata, to_iata, date,
                        adults=adults, cabin_class=cabin_class, sort=eff_sort,
                        fallback=[],
                    ),
                    breaker_emt.call(
                        emt_search_flights, from_iata, to_iata, date,
                        adults=adults, cabin_class=cabin_class.lower(),
                        nonstop_only=nonstop_only,
                        fallback=[],
                    ),
                    breaker_gotogate.call(
                        gotogate_search_flights, from_iata, to_iata, date,
                        adults=adults, cabin_class=cabin_class,
                        nonstop_only=nonstop_only, sort=eff_sort,
                        fallback=[],
                    ),
                    return_exceptions=True,
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[flights] search timeout for %s→%s on %s", from_iata, to_iata, date)
            booking_r, emt_r, gotogate_r = [], [], []
            timed_out = True

        def _src_status(result) -> tuple[list, str]:
            if isinstance(result, Exception):
                return [], f"✗ {str(result)[:120]}"
            return (result, f"✓ {len(result)}") if result else ([], "✗ 0 results")

        booking_flights,  booking_status  = _src_status(booking_r)
        emt_flights,      emt_status      = _src_status(emt_r)
        gotogate_flights, gotogate_status = _src_status(gotogate_r)

        # If EVERY provider actually responded (no exception/timeout) and all came
        # back empty, this route has no service — cache it so the return leg and
        # the multi-hop mirror pass don't re-scrape the same dead route. Only when
        # all breakers are CLOSED: an open breaker returns the empty fallback too,
        # and caching a transient outage as "no service" would hide a real route.
        responses = (booking_r, emt_r, gotogate_r)
        breakers_healthy = not any(
            b.is_open for b in (breaker_booking, breaker_emt, breaker_gotogate)
        )
        all_responded = (
            not timed_out
            and breakers_healthy
            and all(not isinstance(r, Exception) for r in responses)
        )
        all_empty = not (booking_flights or emt_flights or gotogate_flights)
        if all_responded and all_empty:
            _mark_no_service(from_iata, to_iata)

        # Per-provider result counts for this search (observability).
        for prov, flights_list in (
            ("booking", booking_flights), ("easemytrip", emt_flights), ("gotogate", gotogate_flights),
        ):
            METRICS.record_search(prov, raw_count=len(flights_list), deduped_count=len(flights_list))

        logger.info(
            "[flights] %s→%s (%s) | Booking:%s EMT:%s Gotogate:%s",
            from_iata, to_iata, date, booking_status, emt_status, gotogate_status,
        )

        all_raw = booking_flights + emt_flights + gotogate_flights
        seen: set[str] = set()
        merged: list[dict] = []
        for flight in all_raw:
            key = flight.get("flight_number", "") + str(flight.get("departure_time", ""))
            if key and key in seen:
                continue
            seen.add(key)
            merged.append(flight)

        # Cross-source duplicate ratio for this search (observability).
        METRICS.record_search("flights_merge", raw_count=len(all_raw), deduped_count=len(merged))

        if nonstop_only:
            merged = [f for f in merged if (f.get("stops") or 0) == 0]

        # Weighted ranking: attach quality_score + rank_score (price+duration+stops
        # +provider+departure). BEST sort consumes rank_score; CHEAPEST/FASTEST stay
        # pure single-axis so an explicit user sort is honoured exactly.
        merged = score_flights(merged)
        # Final merge-wide rerank by the requested sort (matches what we asked each
        # provider for upstream). sort_flights sinks unpriced flights to the bottom.
        merged = sort_flights(merged, eff_sort)

        # Every provider returns price fields as the fare for ALL searched adults
        # (Booking/Gotogate sum per-traveler prices; EMT fares scale with px). The
        # app shows these per person ("/person"), so normalise to a per-person fare
        # here — the single point where the passenger count is known. Keeping the
        # existing keys (no new fields) means already-shipped app versions render
        # the correct per-head price without any client change. Booking URLs still
        # carry the full party via their own `adults` param.
        pax = max(adults, 1)
        if pax > 1:
            for f in merged:
                for key in ("price_total", "price_base_fare", "price_tax"):
                    val = f.get(key)
                    if val:
                        f[key] = round(val / pax)

        return {
            "flights": merged,
            "from_iata": from_iata,
            "to_iata": to_iata,
            "resolved": True,
        }
