"""
goSTOPS API client — raw fetch layer.

goSTOPS runs a public JSON API on api.gostops.com plus a Next.js site whose
hostel-detail data is only exposed inside the server-component (RSC) stream.
None of these need cookies or auth — plain browser-like headers are enough.

Endpoints wrapped here:
  catalog()             GET api/core/get-active-hostels-with-city/  — all hostels
  hostel_detail(slug)   GET gostops.com/stay/{city}/{slug} (RSC)    — static detail
  live_rooms(...)       GET api/core/v4/get-rooms-list/             — live inventory + price
  availability(...)     GET api/core/v3/check-rooms-availability/   — per-night calendar
  activities(...)       GET api/activity/v5/activities/             — experiences/cabs/scooty

Everything fails closed: a blocked or unexpected response raises GostopsError and
the caller decides whether to skip that hostel or surface the failure.
"""
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote, urlencode

from app.services.curl_pool import get_curl_session

logger = logging.getLogger(__name__)

_API = "https://api.gostops.com"
_SITE = "https://gostops.com"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
_TIMEOUT = 25


class GostopsError(Exception):
    """Raised when a goSTOPS endpoint is unreachable or returns an unusable shape."""


def _api_headers() -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": _SITE,
        "referer": f"{_SITE}/",
        "user-agent": _UA,
    }


def _rsc_headers() -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "rsc": "1",
        "referer": f"{_SITE}/",
        "user-agent": _UA,
    }


async def _get_json(url: str, headers: dict) -> Any:
    async with get_curl_session() as s:
        try:
            r = await s.get(url, headers=headers, timeout=_TIMEOUT)
        except Exception as exc:  # network / TLS / timeout
            raise GostopsError(f"request failed: {url}: {exc}") from exc
    if r.status_code != 200:
        raise GostopsError(f"HTTP {r.status_code}: {url}")
    try:
        return r.json()
    except Exception as exc:
        raise GostopsError(f"non-JSON response: {url}: {exc}") from exc


def _unwrap(payload: Any, url: str) -> Any:
    """goSTOPS wraps success responses as {status, message, data}."""
    if isinstance(payload, dict) and payload.get("status") == "success":
        return payload.get("data")
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    raise GostopsError(f"unexpected payload from {url}: {str(payload)[:200]}")


# ── Catalog ───────────────────────────────────────────────────────────────────
async def catalog() -> list[dict]:
    """All active goSTOPS hostels with city + pricing summary. No params."""
    url = f"{_API}/api/core/get-active-hostels-with-city/"
    data = _unwrap(await _get_json(url, _api_headers()), url)
    if not isinstance(data, list):
        raise GostopsError("catalog data is not a list")
    return data


# ── Hostel detail (RSC parse) ───────────────────────────────────────────────────
# goSTOPS has no JSON detail endpoint (all api.gostops.com detail paths 404); the
# hostel's static data lives only in the Next.js RSC stream as `initialHostelData`.
_ROW_START = re.compile(r"[0-9a-f]{1,3}:(?=[\[{])")


def _find_key(obj: Any, key: str):
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from _find_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _find_key(v, key)


def parse_hostel_detail(rsc_text: str) -> dict:
    """Extract `initialHostelData` from a goSTOPS hostel-page RSC stream.

    The stream is a sequence of `id:<json>` rows. We scan every row that opens a
    JSON value, decode it, and return the first object carrying the hostel-detail
    marker keys. Raises GostopsError if the expected structure is absent (so a
    silent goSTOPS redeploy surfaces loudly instead of writing partial rows).
    """
    decoder = json.JSONDecoder()
    for m in _ROW_START.finditer(rsc_text):
        try:
            obj, _ = decoder.raw_decode(rsc_text, m.end())
        except json.JSONDecodeError:
            continue
        for hit in _find_key(obj, "initialHostelData"):
            detail = hit.get("initialHostelData")
            if isinstance(detail, dict) and _looks_like_detail(detail):
                return detail
    # Fallback: some layouts embed the object without the wrapper key.
    for m in _ROW_START.finditer(rsc_text):
        try:
            obj, _ = decoder.raw_decode(rsc_text, m.end())
        except json.JSONDecodeError:
            continue
        for hit in _find_key(obj, "amenities"):
            if _looks_like_detail(hit):
                return hit
    raise GostopsError("initialHostelData not found in RSC stream (site layout changed?)")


def _looks_like_detail(d: dict) -> bool:
    required = {"amenities", "images", "hostel_slug"}
    return required.issubset(d.keys())


async def hostel_detail(city_slug: str, hostel_slug: str) -> dict:
    """Fetch and parse a hostel's static detail object."""
    url = f"{_SITE}/stay/{quote(city_slug, safe='')}/{quote(hostel_slug, safe='')}"
    async with get_curl_session() as s:
        try:
            r = await s.get(url, headers=_rsc_headers(), timeout=_TIMEOUT)
        except Exception as exc:
            raise GostopsError(f"detail request failed: {url}: {exc}") from exc
    if r.status_code != 200:
        raise GostopsError(f"detail HTTP {r.status_code}: {url}")
    return parse_hostel_detail(r.text)


# ── Live inventory + pricing ────────────────────────────────────────────────────
async def live_rooms(hostel_slug: str, check_in: str, check_out: str) -> dict:
    """Live room list with variants, prices and remaining stock for a date range.

    Dates are 'YYYY-MM-DD'. Returns the raw `data` object
    ({hotel_rooms, coupon_code_applied, identifier}).
    """
    qs = urlencode({"hostel_slug": hostel_slug, "check_in": check_in, "check_out": check_out})
    url = f"{_API}/api/core/v4/get-rooms-list/?{qs}"
    return _unwrap(await _get_json(url, _api_headers()), url)


async def availability(
    hostel_slug: str, check_in: str, check_out: str, rateplan_id: str
) -> dict:
    """Per-night beds-available + price calendar for one rate plan."""
    qs = urlencode({
        "hostel_slug": hostel_slug,
        "check_in": check_in,
        "check_out": check_out,
        "rateplan_id": rateplan_id,
    })
    url = f"{_API}/api/core/v3/check-rooms-availability/?{qs}"
    return _unwrap(await _get_json(url, _api_headers()), url)


async def activities(
    hostel_id: int | str,
    check_in: str,
    check_out: str,
    activity_type: str = "experience,vehicle",
    page: int = 1,
) -> dict:
    """Experiences / cabs / scooty tied to a hostel. Keyed by numeric hostel_id."""
    qs = urlencode({
        "hostel_id": hostel_id,
        "check_in_date": check_in,
        "check_out_date": check_out,
        "activity_type": activity_type,
        "page": page,
    })
    url = f"{_API}/api/activity/v5/activities/?{qs}"
    return _unwrap(await _get_json(url, _api_headers()), url)
