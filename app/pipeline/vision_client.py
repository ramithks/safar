"""
Vision client using Gemini 2.5 Flash REST API.

Replaces Google Vision entirely. Single API key (GEMINI_API_KEY) handles all frames.
Returns name, address, city, country, lat, lng directly — no Places Text Search needed
for most frames, only Nearby Search for enrichment.

Frames are sent in parallel using a thread pool (max 5 concurrent) to stay within
Gemini's default rate limits while keeping total latency low.
"""
import base64
import json
import logging
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.config import settings
from app.pipeline.frame_extractor import ExtractedFrame

logger = logging.getLogger(__name__)

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
)

# Used when no caption context is available
PROMPT_OPEN = (
    "Identify the exact place, landmark, or location shown in this image. "
    "Return ONLY a JSON object with these fields: "
    "name (string or null), address (string or null), city (string or null), "
    "country (string or null), latitude (number or null), longitude (number or null), "
    "confidence (number 0.0-1.0). "
    "If you cannot identify a specific location, set name to null and confidence to 0.0."
)

# Max parallel requests to Gemini (respect default rate limits)
MAX_CONCURRENT = 5


@dataclass
class CaptionContext:
    """Parsed context extracted from the Instagram post caption."""
    raw: str
    place_hints: list[str]   # places extracted from 📍 pins
    city: Optional[str]
    country: Optional[str]


def parse_caption(caption: str) -> Optional[CaptionContext]:
    """
    Extract place names and location context from an Instagram caption.

    Handles patterns like:
      📍Torte by kusha kapila
      📍Begeterre
      Best cafes in Gurgaon, Must visit cafes in Gurugram
    """
    if not caption or not caption.strip():
        return None

    # Extract 📍 pin mentions
    place_hints = [p.strip() for p in re.findall(r'📍([^\n📍#@]+)', caption) if p.strip()]

    # Extract city from common caption patterns
    city = None
    country = None
    city_patterns = [
        r'(?:in|at|cafes? in|places? in|visit in|spots? in)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
        r'#([A-Z][a-z]+(?:cafes?|restaurants?|food|places?))',
    ]
    for pattern in city_patterns:
        match = re.search(pattern, caption, re.IGNORECASE)
        if match:
            city = match.group(1).strip()
            break

    # Simple country hint from caption keywords
    country_keywords = {
        "india": "India", "gurgaon": "India", "gurugram": "India",
        "delhi": "India", "mumbai": "India", "bangalore": "India",
        "dubai": "United Arab Emirates", "london": "United Kingdom",
        "paris": "France", "new york": "United States",
    }
    caption_lower = caption.lower()
    for keyword, country_name in country_keywords.items():
        if keyword in caption_lower:
            country = country_name
            if not city:
                city = keyword.title()
            break

    if not place_hints and not city:
        return None

    logger.info(
        f"Caption parsed: {len(place_hints)} place hints, city={city}, country={country}"
    )
    return CaptionContext(raw=caption, place_hints=place_hints, city=city, country=country)


def _build_prompt(caption_ctx: Optional[CaptionContext]) -> str:
    """
    Build a constrained Gemini prompt when caption context is available.
    This prevents Gemini from guessing the wrong city for a place name
    that exists in multiple countries (e.g. 'NOMÉ' in Hyderabad vs São Paulo).
    """
    if not caption_ctx or (not caption_ctx.place_hints and not caption_ctx.city):
        return PROMPT_OPEN

    parts = ["This image is from an Instagram reel."]

    if caption_ctx.city:
        loc = caption_ctx.city
        if caption_ctx.country:
            loc += f", {caption_ctx.country}"
        parts.append(f"The reel is about places in {loc}.")

    if caption_ctx.place_hints:
        hints = ", ".join(f'"{p}"' for p in caption_ctx.place_hints)
        parts.append(f"The reel mentions these specific places: {hints}.")
        parts.append(
            "Identify which ONE of these places is shown in the image. "
            "Use the city context to pick the correct location — do NOT match "
            "the same name in a different country."
        )
    else:
        parts.append("Identify the exact place shown in the image.")

    parts.append(
        "Return ONLY a JSON object: "
        "name (string or null), address (string or null), city (string or null), "
        "country (string or null), latitude (number or null), longitude (number or null), "
        "confidence (number 0.0-1.0). "
        "If no match, set name to null and confidence to 0.0."
    )
    return " ".join(parts)


@dataclass
class VisionResult:
    frame: ExtractedFrame
    detection_source: str = "unknown"
    confidence: float = 0.0
    landmark_name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    # Pre-built Google Maps search URL — reliable even when lat/lng is imprecise
    maps_search_url: Optional[str] = None
    scene_description: Optional[str] = None
    text_detected: list[str] = field(default_factory=list)
    visual_cues: list[str] = field(default_factory=list)


def analyze_frames(
    frames: list[ExtractedFrame],
    caption_ctx: Optional[CaptionContext] = None,
) -> list[VisionResult]:
    """
    Send all frames to Gemini 2.5 Flash in parallel, return VisionResults.
    When caption_ctx is provided, uses a constrained prompt that locks
    Gemini to the places and city mentioned in the caption.
    """
    if not frames:
        return []

    if not settings.gemini_api_key:
        logger.error("GEMINI_API_KEY not set — vision analysis disabled")
        return [VisionResult(frame=f) for f in frames]

    prompt = _build_prompt(caption_ctx)
    if caption_ctx and caption_ctx.place_hints:
        logger.info(
            f"Using caption-constrained prompt — {len(caption_ctx.place_hints)} place hints, "
            f"city={caption_ctx.city}"
        )

    results: list[Optional[VisionResult]] = [None] * len(frames)

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        future_to_idx = {
            executor.submit(_call_gemini, frame, prompt): idx
            for idx, frame in enumerate(frames)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.warning(f"Frame {frames[idx].scene_index}: unexpected error: {e}")
                results[idx] = VisionResult(frame=frames[idx])

    final = [r if r is not None else VisionResult(frame=frames[i]) for i, r in enumerate(results)]

    successful = sum(1 for r in final if r.confidence >= settings.min_vision_confidence)
    logger.info(f"Gemini vision: {successful}/{len(frames)} frames identified")
    return final


def _call_gemini(frame: ExtractedFrame, prompt: str) -> VisionResult:
    """
    Single Gemini 2.5 Flash REST call for one frame.
    Uses response_mime_type=application/json to get structured output directly.
    """
    result = VisionResult(frame=frame)

    try:
        with open(frame.frame_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        logger.warning(f"Frame {frame.scene_index}: could not read image: {e}")
        return result

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": image_b64,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 8192,  # 2.5-flash thinking consumes tokens before output
        },
    }

    try:
        response = httpx.post(
            GEMINI_URL,
            params={"key": settings.gemini_api_key},
            json=payload,
            timeout=30.0,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.warning(f"Frame {frame.scene_index}: Gemini HTTP {e.response.status_code}: {e.response.text[:200]}")
        return result
    except Exception as e:
        logger.warning(f"Frame {frame.scene_index}: Gemini request failed: {e}")
        return result

    return _parse_response(response.json(), frame)


def _parse_response(raw: dict, frame: ExtractedFrame) -> VisionResult:
    result = VisionResult(frame=frame)

    try:
        candidates = raw.get("candidates", [])
        if not candidates:
            return result

        # Gemini 2.5 Flash is a thinking model — response parts look like:
        #   parts[0] = { "thought": true, "text": "...internal reasoning..." }
        #   parts[1] = { "text": "{actual json output}" }
        # Always take the last non-thought part to get the real output.
        parts = candidates[0]["content"]["parts"]
        text = next(
            (p["text"] for p in reversed(parts) if not p.get("thought", False)),
            None,
        )
        if not text:
            logger.warning(f"Frame {frame.scene_index}: Gemini returned no non-thought part")
            return result

        data = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning(f"Frame {frame.scene_index}: could not parse Gemini response: {e}")
        return result

    name = data.get("name")
    confidence = float(data.get("confidence", 0.0))

    if not name or confidence == 0.0:
        return result

    result.detection_source = "gemini"
    result.confidence = confidence
    result.landmark_name = name
    result.address = data.get("address")
    result.city = data.get("city")
    result.country = data.get("country")

    lat = data.get("latitude")
    lng = data.get("longitude")
    if lat is not None and lng is not None:
        try:
            result.lat = float(lat)
            result.lng = float(lng)
        except (TypeError, ValueError):
            pass

    # Build scene description
    parts = [p for p in [name, data.get("city"), data.get("country")] if p]
    result.scene_description = ", ".join(parts)

    # Build Google Maps search URL from name + city (more reliable than raw coords)
    query_parts = [p for p in [name, data.get("city")] if p]
    query = " ".join(query_parts)
    result.maps_search_url = (
        "https://www.google.com/maps/search/?api=1&query="
        + urllib.parse.quote(query)
    )

    logger.debug(
        f"Frame {frame.scene_index}: '{name}' "
        f"({'lat/lng' if result.lat else 'no coords'}, conf={confidence:.2f})"
    )
    return result
