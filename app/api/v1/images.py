"""
Image proxy for Google Places photos.

Frontend calls:  GET /api/v1/images/{photo_reference}
Backend fetches: Google Places photo API (server-side, key never exposed)
Response:        image bytes with correct Content-Type + aggressive cache headers

Why this exists:
  The Google Places photo URL includes our API key as a query param.
  Exposing that URL directly to the frontend would leak the key in browser
  DevTools / network logs. This proxy keeps the key server-side and gives
  the frontend a clean, key-free URL to use for img src.
"""
import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from app.config import settings
from app.core.limiter import limiter

router = APIRouter()

_GOOGLE_PHOTO_URL = "https://maps.googleapis.com/maps/api/place/photo"

# Shared client — keep-alive for repeated image fetches
_client = httpx.AsyncClient(
    timeout=15.0,
    follow_redirects=True,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)

# Cache for 7 days — place photos almost never change
_CACHE_HEADER = "public, max-age=604800, immutable"


@router.get("/images/{photo_reference}", include_in_schema=False)
# Unauthenticated (URLs go straight into <Image> tags), so the rate limit is
# the only thing standing between the public internet and our Places quota.
# shared_limit + scope: one bucket per client across ALL photo references —
# plain limit() would bucket per-path, giving each reference its own 120/min.
@limiter.shared_limit("120/minute", scope="place_images")
async def proxy_place_photo(request: Request, photo_reference: str, maxwidth: int = 800) -> Response:
    """
    Proxy a Google Places photo by reference.

    The photo_reference comes from the Places API and is opaque — just pass it through.
    maxwidth: 400 | 800 | 1600  (default 800, capped at 1600)
    """
    key = settings.google_maps_api_key
    if not key:
        raise HTTPException(503, "Image service not configured")

    # Clamp maxwidth to avoid abuse
    maxwidth = min(max(maxwidth, 100), 1600)

    try:
        r = await _client.get(
            _GOOGLE_PHOTO_URL,
            params={
                "photo_reference": photo_reference,
                "maxwidth": maxwidth,
                "key": key,
            },
        )
    except httpx.TimeoutException:
        raise HTTPException(504, "Image fetch timed out")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Image fetch failed: {type(e).__name__}")

    if r.status_code == 400:
        raise HTTPException(400, "Invalid photo reference")
    if r.status_code == 403:
        raise HTTPException(503, "Image service quota exceeded")
    if r.status_code != 200:
        raise HTTPException(502, f"Upstream returned {r.status_code}")

    content_type = r.headers.get("content-type", "image/jpeg")

    return Response(
        content=r.content,
        media_type=content_type,
        headers={"Cache-Control": _CACHE_HEADER},
    )
