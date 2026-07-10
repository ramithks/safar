"""
Destination weather — Open-Meteo (free, keyless).

One call per trip: geocode the destination, pull the daily forecast for the
trip window, and shape it into the TripState `/weather` block the workspace
renders and the copilot grounds answers in.

Open-Meteo's forecast covers ~16 days out. When the trip starts beyond that
horizon we still return the next days at the destination flagged
`is_preview: true` ("conditions there right now"), so the UI can label it
honestly instead of showing nothing.
"""
import logging
from datetime import date, datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_FORECAST_HORIZON_DAYS = 15

# One fetch per trip — a short-lived client per call avoids binding a shared
# client to an event loop that scripts/tests may have already closed.
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=8.0)

# WMO weather codes → (label, icon key the frontend maps to an icon)
_CODE_MAP: list[tuple[set[int], str, str]] = [
    ({0}, "Clear sky", "sun"),
    ({1, 2}, "Partly cloudy", "cloud-sun"),
    ({3}, "Overcast", "cloud"),
    ({45, 48}, "Foggy", "fog"),
    ({51, 53, 55, 56, 57}, "Light drizzle", "drizzle"),
    ({61, 63, 65, 66, 67}, "Rain", "rain"),
    ({71, 73, 75, 77, 85, 86}, "Snow", "snow"),
    ({80, 81, 82}, "Rain showers", "rain"),
    ({95, 96, 99}, "Thunderstorm", "thunder"),
]


def _describe(code: int) -> tuple[str, str]:
    for codes, label, icon in _CODE_MAP:
        if code in codes:
            return label, icon
    return "Mixed conditions", "cloud"


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def _geocode(destination: str) -> dict | None:
    """
    Google Places first: it ranks the famous destination on top (plain
    Open-Meteo geocoding puts Manali TN above Manali HP). Open-Meteo geocoding
    is the keyless fallback.
    """
    try:
        from app.services.places import search_places

        for p in await search_places(destination):
            loc = (p.get("geometry") or {}).get("location") or {}
            if loc.get("lat") and loc.get("lng"):
                return {
                    "name": p.get("name") or destination,
                    "latitude": loc["lat"],
                    "longitude": loc["lng"],
                }
    except Exception as exc:  # noqa: BLE001 — fall through to Open-Meteo
        logger.info("[weather] places geocode failed for %r: %s", destination, exc)

    async with _client() as client:
        r = await client.get(
            _GEOCODE_URL, params={"name": destination, "count": 1, "language": "en"}
        )
    r.raise_for_status()
    results = r.json().get("results") or []
    return results[0] if results else None


def _summary(daily: list[dict]) -> str:
    if not daily:
        return ""
    highs = [d["temp_max"] for d in daily if d.get("temp_max") is not None]
    lows = [d["temp_min"] for d in daily if d.get("temp_min") is not None]
    rainy = [d for d in daily if (d.get("precip_prob") or 0) >= 50]
    bits = []
    if highs and lows:
        bits.append(f"{round(min(lows))}–{round(max(highs))}°C")
    if rainy:
        bits.append(
            "rain likely most days" if len(rainy) > len(daily) / 2
            else f"rain likely on {len(rainy)} of {len(daily)} days"
        )
    else:
        bits.append("little rain expected")
    return ", ".join(bits)


async def get_trip_weather(
    destination: str,
    start_date: str | None,
    end_date: str | None = None,
    days: int = 3,
) -> dict:
    """
    Forecast block for the trip window. Returns {} when the destination can't
    be geocoded — callers treat weather as strictly optional.
    """
    place = await _geocode(destination)
    if not place:
        logger.info("[weather] no geocode result for %r", destination)
        return {}

    today = datetime.now(timezone.utc).date()
    start = _parse_date(start_date) or today
    end = _parse_date(end_date) or (start + timedelta(days=max(days - 1, 0)))
    if end < start:
        end = start

    horizon = today + timedelta(days=_FORECAST_HORIZON_DAYS)
    is_preview = start > horizon
    if is_preview:
        # Trip too far out for a real forecast — show conditions there now.
        span = min((end - start).days, 6)
        start, end = today, today + timedelta(days=span)
    else:
        start = max(start, today)
        end = min(end, horizon)

    async with _client() as client:
        r = await client.get(_FORECAST_URL, params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "timezone": "auto",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        })
    r.raise_for_status()
    block = r.json().get("daily") or {}

    daily = []
    for i, day in enumerate(block.get("time") or []):
        code = (block.get("weather_code") or [None] * 99)[i]
        label, icon = _describe(int(code)) if code is not None else ("", "cloud")
        daily.append({
            "date": day,
            "temp_max": (block.get("temperature_2m_max") or [None] * 99)[i],
            "temp_min": (block.get("temperature_2m_min") or [None] * 99)[i],
            "precip_prob": (block.get("precipitation_probability_max") or [None] * 99)[i],
            "code": code,
            "label": label,
            "icon": icon,
        })

    return {
        "location": place.get("name") or destination,
        "latitude": place["latitude"],
        "longitude": place["longitude"],
        "units": "celsius",
        "is_preview": is_preview,
        "daily": daily,
        "summary": _summary(daily),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
