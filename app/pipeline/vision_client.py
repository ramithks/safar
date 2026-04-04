"""
Vision client using Gemini 2.5 Flash Lite via Vertex AI (google-genai SDK).

Authentication uses Application Default Credentials (ADC).
  - On GCP (Cloud Run, GCE, etc.): automatic via metadata server.
  - Locally: run `gcloud auth application-default login`, or set
    GOOGLE_APPLICATION_CREDENTIALS to a service account key JSON.

Frames are sent in parallel using a thread pool (max 5 concurrent).
"""
import json
import logging
import re
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

from google import genai
from google.genai import types

from app.config import settings
from app.pipeline.frame_extractor import ExtractedFrame

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# Place types we accept — anything not in this set is rejected
VALID_PLACE_TYPES = {
    "cafe", "restaurant", "hotel", "bar", "club", "lounge",
    "landmark", "monument", "park", "museum", "gallery", "market",
    "attraction", "resort", "spa", "rooftop", "other",
}

# Max parallel requests to Gemini
MAX_CONCURRENT = 5

# Module-level client — created once on first use
_client: Optional[genai.Client] = None

# ── Prompts ────────────────────────────────────────────────────────────────────

_JSON_SCHEMA = """\
Return ONLY a valid JSON object — no markdown, no explanation, nothing else:
{
  "name": "exact establishment name, or null if cannot identify",
  "place_type": "one of: cafe | restaurant | hotel | bar | club | lounge | landmark | monument | park | museum | gallery | market | attraction | resort | spa | rooftop | other — or null",
  "address": "full street address or null",
  "city": "city name or null",
  "country": "country name or null",
  "latitude": <number> or null,
  "longitude": <number> or null,
  "confidence": <number 0.0–1.0>,
  "evidence": "one sentence: the specific visual cue that identified this place"
}\
"""

# ── Caption parsing ────────────────────────────────────────────────────────────


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

    # Extract city — require capital letter start so "in the end" doesn't match.
    # \b word boundary prevents matching "in" inside a word like "Diggin".
    city_patterns = [
        r'\b(?:in|at|cafes? in|places? in|visit in|spots? in)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)',
        r'#([A-Z][a-z]+(?:cafes?|restaurants?|food|places?))',
    ]
    city = None
    country = None
    for pattern in city_patterns:
        match = re.search(pattern, caption)
        if match:
            city = match.group(1).strip()
            break

    # Country hint from known city/country keywords in caption
    country_keywords = {
        "india": "India", "gurgaon": "India", "gurugram": "India",
        "delhi": "India", "mumbai": "India", "bangalore": "India",
        "hyderabad": "India", "pune": "India", "chennai": "India",
        "dubai": "United Arab Emirates", "abu dhabi": "United Arab Emirates",
        "london": "United Kingdom", "manchester": "United Kingdom",
        "paris": "France", "new york": "United States", "los angeles": "United States",
        "singapore": "Singapore", "bangkok": "Thailand", "tokyo": "Japan",
        "bali": "Indonesia", "istanbul": "Turkey",
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


# ── Prompt builder ─────────────────────────────────────────────────────────────


def _build_prompt(caption_ctx: Optional[CaptionContext]) -> str:
    """Assemble the full prompt for a Gemini call."""
    parts = []

    if caption_ctx and (caption_ctx.city or caption_ctx.place_hints):
        # ── Constrained prompt: caption gave us city/place context ──────────────
        parts.append(
            "You are a travel location expert. This image is a frame from an Instagram "
            "travel reel. Identify the exact cafe, restaurant, hotel, bar, or other "
            "named venue shown."
        )

        if caption_ctx.city:
            loc = caption_ctx.city
            if caption_ctx.country:
                loc += f", {caption_ctx.country}"
            parts.append(
                f"IMPORTANT: This reel is about places in {loc}. "
                f"If the venue exists in multiple cities, pick the {loc} branch. "
                f"Do NOT return a location outside {loc}."
            )

        if caption_ctx.place_hints:
            hints = ", ".join(f'"{p}"' for p in caption_ctx.place_hints)
            parts.append(
                f"The reel caption mentions these specific places: {hints}. "
                f"Identify which ONE of these is shown in this frame."
            )

    else:
        # ── Open prompt: no caption context ─────────────────────────────────────
        parts.append(
            "You are a travel location expert. This image is a frame from an Instagram "
            "travel reel. Identify the exact cafe, restaurant, hotel, bar, landmark, or "
            "other named venue shown in this image."
        )

    parts.append(
        "Only identify real, visitable places a traveler would visit. "
        "If this is a person, generic food with no establishment visible, a private home, "
        "or you genuinely cannot identify a specific named venue — set name=null and confidence=0.0. "
        "Use your full geographic and cultural knowledge. "
        "Be precise about the exact branch/location."
    )
    parts.append(_JSON_SCHEMA)
    return "\n\n".join(parts)


# ── Data model ─────────────────────────────────────────────────────────────────


@dataclass
class VisionResult:
    frame: ExtractedFrame
    detection_source: str = "unknown"
    confidence: float = 0.0
    landmark_name: Optional[str] = None
    place_type: Optional[str] = None          # cafe, restaurant, hotel, landmark, …
    lat: Optional[float] = None
    lng: Optional[float] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    maps_search_url: Optional[str] = None
    scene_description: Optional[str] = None
    evidence: Optional[str] = None            # what visual cue Gemini used
    text_detected: list[str] = field(default_factory=list)
    visual_cues: list[str] = field(default_factory=list)


# ── Public API ─────────────────────────────────────────────────────────────────


def analyze_frames(
    frames: list[ExtractedFrame],
    caption_ctx: Optional[CaptionContext] = None,
) -> list[VisionResult]:
    """
    Send all frames to Gemini 2.5 Flash Lite (Vertex AI) in parallel.

    Two-pass strategy when no caption city context is available:
      Pass 1 — unconstrained: analyze all frames freely.
      Pass 2 — consensus requery: lock outlier frames to the dominant city so
               Gemini picks the local branch, not a famous foreign one.
    """
    if not frames:
        return []

    if not settings.gcp_project:
        logger.error("GCP_PROJECT not set — vision analysis disabled")
        return [VisionResult(frame=f) for f in frames]

    prompt = _build_prompt(caption_ctx)
    if caption_ctx and caption_ctx.place_hints:
        logger.info(
            f"Caption-constrained prompt — {len(caption_ctx.place_hints)} hints, "
            f"city={caption_ctx.city}"
        )

    final = _run_parallel(frames, prompt)

    # Two-pass consensus requery — only when caption gave us no city context
    if not (caption_ctx and (caption_ctx.city or caption_ctx.place_hints)):
        final = _consensus_requery(frames, final)

    successful = sum(1 for r in final if r.confidence >= settings.min_vision_confidence)
    logger.info(f"Gemini vision: {successful}/{len(frames)} frames identified")
    return final


# ── Internal helpers ───────────────────────────────────────────────────────────


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        if not settings.gcp_project:
            raise RuntimeError("GCP_PROJECT is not set — required for Vertex AI")
        _client = genai.Client(
            vertexai=True,
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
        logger.info(
            f"Vertex AI client initialised (project={settings.gcp_project}, "
            f"location={settings.gcp_location})"
        )
    return _client


def _run_parallel(frames: list[ExtractedFrame], prompt: str) -> list[VisionResult]:
    """Run Gemini on all frames in parallel with the given prompt."""
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
    return [r if r is not None else VisionResult(frame=frames[i]) for i, r in enumerate(results)]


def _consensus_requery(
    frames: list[ExtractedFrame],
    results: list[VisionResult],
) -> list[VisionResult]:
    """
    Majority-vote on city/country across all high-confidence results.
    Re-query outlier frames with the consensus city locked in.
    """
    votes: list[tuple[str, str]] = [
        (r.city.strip().lower(), r.country.strip().lower())
        for r in results
        if r.confidence >= settings.min_vision_confidence and r.city and r.country
    ]
    if not votes:
        return results

    (consensus_city_lower, consensus_country_lower), vote_count = Counter(votes).most_common(1)[0]
    if vote_count < 2:
        # No clear majority — all cities tied, nothing to correct
        logger.info("No consensus city (all frames disagree) — skipping requery")
        return results

    # Outliers = results that disagree with consensus OR have no city (can't be verified)
    outlier_indices = [
        i for i, r in enumerate(results)
        if r.confidence >= settings.min_vision_confidence and r.landmark_name
        and (
            # Has a city/country that differs from consensus
            (r.city and r.country and (
                r.city.strip().lower() != consensus_city_lower
                or r.country.strip().lower() != consensus_country_lower
            ))
            # OR has a name but no city — can't verify, re-query to ground it
            or (not r.city or not r.country)
        )
    ]

    if not outlier_indices:
        return results

    winner = next(
        (r for r in results
         if r.city and r.country
         and r.city.strip().lower() == consensus_city_lower
         and r.country.strip().lower() == consensus_country_lower),
        None,
    )
    consensus_city = winner.city if winner else consensus_city_lower.title()
    consensus_country = winner.country if winner else consensus_country_lower.title()

    logger.info(
        f"Consensus city: {consensus_city}, {consensus_country} — "
        f"re-querying {len(outlier_indices)} outlier frame(s)"
    )

    city_ctx = CaptionContext(
        raw="", place_hints=[], city=consensus_city, country=consensus_country,
    )
    constrained_prompt = _build_prompt(city_ctx)

    updated = list(results)
    for i in outlier_indices:
        original = results[i]
        try:
            requeried = _call_gemini(frames[i], constrained_prompt)
            if requeried.confidence >= settings.min_vision_confidence:
                logger.info(
                    f"Frame {frames[i].scene_index}: corrected '{original.landmark_name}' "
                    f"({original.city}) → '{requeried.landmark_name}' ({requeried.city})"
                )
                updated[i] = requeried
            else:
                # Requery couldn't confidently place this frame in the consensus city.
                # Drop the original rather than showing a wrong location.
                logger.info(
                    f"Frame {frames[i].scene_index}: dropping '{original.landmark_name}' "
                    f"({original.city}) — could not verify in {consensus_city}"
                )
                updated[i] = VisionResult(frame=frames[i])
        except Exception as e:
            logger.warning(f"Frame {frames[i].scene_index}: consensus requery failed: {e}")
            updated[i] = VisionResult(frame=frames[i])

    return updated


def _call_gemini(frame: ExtractedFrame, prompt: str) -> VisionResult:
    """Single Gemini call for one frame via Vertex AI SDK."""
    result = VisionResult(frame=frame)

    try:
        with open(frame.frame_path, "rb") as f:
            image_bytes = f.read()
    except Exception as e:
        logger.warning(f"Frame {frame.scene_index}: could not read image: {e}")
        return result

    try:
        client = _get_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt,
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                max_output_tokens=8192,  # thinking model needs headroom for reasoning tokens
            ),
        )
    except Exception as e:
        logger.warning(f"Frame {frame.scene_index}: Gemini request failed: {e}")
        return result

    return _parse_response(response, frame)


def _parse_response(response, frame: ExtractedFrame) -> VisionResult:
    result = VisionResult(frame=frame)

    try:
        # Thinking model: skip thought parts, take the last non-thought text
        text = None
        for candidate in response.candidates:
            for part in reversed(candidate.content.parts):
                if not getattr(part, "thought", False) and part.text:
                    text = part.text
                    break
            if text:
                break

        if not text:
            logger.warning(f"Frame {frame.scene_index}: no output from Gemini")
            return result

        data = json.loads(text)
    except (AttributeError, json.JSONDecodeError) as e:
        logger.warning(f"Frame {frame.scene_index}: could not parse response: {e}")
        return result

    name = data.get("name")
    confidence = float(data.get("confidence", 0.0))
    place_type = (data.get("place_type") or "").lower().strip() or None

    # Reject if no name, low confidence, or not a visitable place type
    if not name or confidence < settings.min_vision_confidence:
        return result
    if place_type and place_type not in VALID_PLACE_TYPES:
        logger.debug(f"Frame {frame.scene_index}: rejected type '{place_type}' for '{name}'")
        return result

    result.detection_source = "gemini"
    result.confidence = confidence
    result.landmark_name = name
    result.place_type = place_type
    result.address = data.get("address")
    result.city = data.get("city")
    result.country = data.get("country")
    result.evidence = data.get("evidence")

    lat = data.get("latitude")
    lng = data.get("longitude")
    if lat is not None and lng is not None:
        try:
            result.lat = float(lat)
            result.lng = float(lng)
        except (TypeError, ValueError):
            pass

    desc_parts = [p for p in [name, place_type, data.get("city"), data.get("country")] if p]
    result.scene_description = ", ".join(desc_parts)

    query_parts = [p for p in [name, data.get("city")] if p]
    result.maps_search_url = (
        "https://www.google.com/maps/search/?api=1&query="
        + urllib.parse.quote(" ".join(query_parts))
    )

    logger.debug(
        f"Frame {frame.scene_index}: '{name}' [{place_type}] "
        f"({'coords' if result.lat else 'no coords'}, conf={confidence:.2f}) — {result.evidence}"
    )
    return result
