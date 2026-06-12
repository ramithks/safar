"""
Vision client using Gemini 2.5 Flash via Vertex AI (google-genai SDK).

Key reliability improvements:
  1. Batch all frames for a reel into ONE Gemini call (reduces API calls N→1).
  2. Exponential backoff: 1s → 2s → 4s → 8s (max) on 429 / transient errors.
  3. Multi-region fallback: asia-south1 → global → us-central1.
  4. Google Search Grounding: reduces hallucinations, anchors to real places.

Authentication uses Application Default Credentials (ADC).
  - On GCP (Cloud Run, GCE, etc.): automatic via metadata server.
  - Locally: run `gcloud auth application-default login`, or set
    GOOGLE_APPLICATION_CREDENTIALS to a service account key JSON.
"""
import json
import logging
import re
import time
import urllib.parse
from collections import Counter
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

# ── Region fallback chain ─────────────────────────────────────────────────────
# Try asia-south1 first (closest to India), then the global endpoint, then
# us-central1 as last resort.  Each client is lazily created and cached.
_REGION_CHAIN = ["asia-south1", "global", "us-central1"]
_region_clients: dict[str, genai.Client] = {}

# ── Retry / backoff config ─────────────────────────────────────────────────────
# Exponential: 1 → 2 → 4 → 8 seconds between attempts.
_BACKOFF_BASE_SECONDS = 1
_BACKOFF_MULTIPLIER = 2
_BACKOFF_MAX_SECONDS = 8
MAX_RETRIES = 4                  # up to 4 retries across all regions
GEMINI_TIMEOUT_SECONDS = 90     # batch call can take longer; allow 90s

# Frames per Gemini call. One giant batch risks output-token truncation
# (max_output_tokens=8192) — a truncated JSON array used to silently zero out
# the ENTIRE reel. Chunks also isolate failures to a few frames.
VISION_BATCH_SIZE = 6

# ── Prompts ────────────────────────────────────────────────────────────────────

_BATCH_SYSTEM_PROMPT = """\
You are a world-class travel location expert with encyclopedic knowledge of
restaurants, cafes, hotels, bars, landmarks, and attractions globally.
You excel at identifying specific named venues from visual cues — signage,
architecture, decor, menus, uniforms, and surroundings.
When identifying places, use Google Search to verify that the place exists
and confirm its exact address, coordinates, and category.
"""

_JSON_SCHEMA = """\
Return ONLY a valid JSON array — one object per frame, in the SAME ORDER as
the images were provided.  No markdown fences, no explanation, nothing else.

Each object MUST have exactly these keys:
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
        "paris": "France", "new york": "United States",
        "los angeles": "United States",
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


def _build_batch_prompt(
    num_frames: int,
    caption_ctx: Optional[CaptionContext],
) -> str:
    """Assemble the full prompt for a BATCH Gemini call (all frames at once)."""
    parts = []

    if caption_ctx and (caption_ctx.city or caption_ctx.place_hints):
        parts.append(
            f"The following {num_frames} images are sequential frames from a single "
            f"Instagram travel reel.  Identify the exact cafe, restaurant, hotel, bar, "
            f"or other named venue shown in EACH frame."
        )
        if caption_ctx.city:
            loc = caption_ctx.city
            if caption_ctx.country:
                loc += f", {caption_ctx.country}"
            parts.append(
                f"IMPORTANT: This reel is about places in {loc}. "
                f"If a venue exists in multiple cities, pick the {loc} branch. "
                f"Do NOT return a location outside {loc}."
            )
        if caption_ctx.place_hints:
            hints = ", ".join(f'"{p}"' for p in caption_ctx.place_hints)
            parts.append(
                f"The reel caption mentions these specific places: {hints}. "
                f"Match each frame to ONE of these places where possible."
            )
    else:
        parts.append(
            f"The following {num_frames} images are sequential frames from a single "
            f"Instagram travel reel.  For EACH frame identify the exact cafe, "
            f"restaurant, hotel, bar, landmark, or other named venue shown."
        )

    parts.append(
        "Only identify real, visitable places a traveler would visit. "
        "If a frame shows a person, generic food with no establishment visible, "
        "a private home, or you genuinely cannot identify a specific named venue — "
        "set name=null and confidence=0.0. "
        "MANDATORY: use Google Search to verify EVERY place before returning it. "
        "A place may only be returned if the search confirms it exists on the "
        "internet (official page, Google Maps listing, reviews, or articles); "
        "if you cannot find it online, return name=null and confidence=0.0 "
        "instead of guessing. Be precise about the exact branch/location, and "
        "always fill place_type with the most specific matching category."
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
    Send ALL frames for a reel to Gemini in a SINGLE batched API call.

    Strategy:
      Pass 1 — batch call: all frames in one request with Google Search grounding.
      Pass 2 — consensus requery (when no caption city): re-query outliers with
               the majority-vote city locked in to eliminate cross-city drift.
    """
    if not frames:
        return []

    if not settings.gcp_project:
        logger.error("GCP_PROJECT not set — vision analysis disabled")
        return [VisionResult(frame=f) for f in frames]

    # Chunk frames so a single truncated/failed response can't zero the whole reel
    final: list[VisionResult] = []
    for start in range(0, len(frames), VISION_BATCH_SIZE):
        chunk = frames[start: start + VISION_BATCH_SIZE]
        prompt = _build_batch_prompt(len(chunk), caption_ctx)
        logger.info(
            f"Gemini batch {start // VISION_BATCH_SIZE + 1}: {len(chunk)} frames, "
            f"caption_ctx={'yes' if caption_ctx else 'no'}"
        )
        final.extend(_run_batch_with_retry(chunk, prompt))

    # Two-pass consensus requery — only when caption gave us no city context
    if not (caption_ctx and (caption_ctx.city or caption_ctx.place_hints)):
        final = _consensus_requery(frames, final)

    successful = sum(1 for r in final if r.confidence >= settings.min_vision_confidence)
    logger.info(f"Gemini vision: {successful}/{len(frames)} frames identified")
    return final


# ── Region client management ───────────────────────────────────────────────────


def _get_client(location: str) -> genai.Client:
    """Return (or create) a cached Vertex AI client for the given location."""
    if location not in _region_clients:
        if not settings.gcp_project:
            raise RuntimeError("GCP_PROJECT is not set — required for Vertex AI")
        _region_clients[location] = genai.Client(
            vertexai=True,
            project=settings.gcp_project,
            location=location,
        )
        logger.info(
            f"Vertex AI client initialised (project={settings.gcp_project}, "
            f"location={location})"
        )
    return _region_clients[location]


# ── Exponential backoff helper ─────────────────────────────────────────────────


def _backoff_delay(attempt: int) -> float:
    """Return the sleep duration for attempt N (0-indexed), capped at max."""
    delay = _BACKOFF_BASE_SECONDS * (_BACKOFF_MULTIPLIER ** attempt)
    return min(delay, _BACKOFF_MAX_SECONDS)


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


# ── Batch call with retry + regional fallback ──────────────────────────────────


def _run_batch_with_retry(
    frames: list[ExtractedFrame],
    prompt: str,
) -> list[VisionResult]:
    """
    Send all frames in one batch Gemini call.
    Regional fallback: asia-south1 → global → us-central1.
    Exponential backoff: 1s → 2s → 4s → 8s between attempts.
    """
    # Load all frame images once
    image_bytes_list: list[bytes] = []
    for frame in frames:
        try:
            with open(frame.frame_path, "rb") as fh:
                image_bytes_list.append(fh.read())
        except Exception as e:
            logger.warning(f"Frame {frame.scene_index}: could not read image: {e}")
            image_bytes_list.append(b"")  # placeholder so indices stay aligned

    # Attempt across regions with exponential backoff
    region_idx = 0
    attempt = 0

    while attempt <= MAX_RETRIES:
        location = _REGION_CHAIN[min(region_idx, len(_REGION_CHAIN) - 1)]

        try:
            client = _get_client(location)
            results = _call_gemini_batch(client, location, frames, image_bytes_list, prompt)
            logger.info(f"Batch call succeeded on region '{location}' (attempt {attempt + 1})")
            return results

        except Exception as exc:
            rate_limited = _is_rate_limit(exc)
            logger.warning(
                f"Batch call failed on region '{location}' "
                f"(attempt {attempt + 1}/{MAX_RETRIES + 1}): "
                f"{'rate-limit 429' if rate_limited else type(exc).__name__}: {exc}"
            )

            if attempt >= MAX_RETRIES:
                logger.error("All retry attempts exhausted — returning empty results")
                return [VisionResult(frame=f) for f in frames]

            # Advance to next region on 429; stay on same region for transient errors
            if rate_limited and region_idx < len(_REGION_CHAIN) - 1:
                region_idx += 1
                next_region = _REGION_CHAIN[region_idx]
                logger.info(f"Rate-limited — falling back to region '{next_region}'")

            delay = _backoff_delay(attempt)
            logger.info(f"Waiting {delay:.1f}s before retry (attempt {attempt + 2})...")
            time.sleep(delay)
            attempt += 1

    return [VisionResult(frame=f) for f in frames]


def _call_gemini_batch(
    client: genai.Client,
    location: str,
    frames: list[ExtractedFrame],
    image_bytes_list: list[bytes],
    prompt: str,
) -> list[VisionResult]:
    """
    Single batch Gemini API call: all frame images + prompt in one request.
    Uses Google Search Grounding to reduce hallucinations.
    Returns one VisionResult per frame (same order).
    """
    # Build the contents list: interleave images + one final prompt
    contents: list = []
    for i, (frame, img_bytes) in enumerate(zip(frames, image_bytes_list)):
        if img_bytes:
            contents.append(
                types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg")
            )
        else:
            # Skip frames we couldn't load; insert a text placeholder so the
            # model knows there's a gap — we'll fill with empty result later.
            contents.append(types.Part.from_text(
                text=f"[Frame {i + 1}: image unavailable]"
            ))

    contents.append(types.Part.from_text(text=prompt))

    # Google Search Grounding tool — anchors answers to real world data
    google_search_tool = types.Tool(
        google_search=types.GoogleSearch()
    )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=_BATCH_SYSTEM_PROMPT,
            temperature=0.1,
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=4096),
            tools=[google_search_tool],
        ),
    )

    return _parse_batch_response(response, frames)


# ── Response parsing ───────────────────────────────────────────────────────────


def _parse_batch_response(response, frames: list[ExtractedFrame]) -> list[VisionResult]:
    """
    Parse the batch JSON array response from Gemini.
    Gemini returns a JSON array with one object per frame.
    Falls back to an empty result for any frame it can't parse.
    """
    empty = [VisionResult(frame=f) for f in frames]

    try:
        # Thinking model: skip thought parts, collect the last non-thought text
        text = None
        for candidate in response.candidates:
            for part in reversed(candidate.content.parts):
                if not getattr(part, "thought", False) and part.text:
                    text = part.text
                    break
            if text:
                break

        if not text:
            logger.warning("Batch response: no output text from Gemini")
            return empty

        # Strip markdown fences if the model included them despite instructions
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.DOTALL)
        data_list = json.loads(cleaned)

    except (AttributeError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Batch response: could not parse JSON: {e}")
        return empty

    if not isinstance(data_list, list):
        logger.warning("Batch response: expected JSON array, got something else")
        return empty

    results: list[VisionResult] = []
    for i, frame in enumerate(frames):
        if i >= len(data_list):
            logger.warning(f"Batch response: missing entry for frame {frame.scene_index}")
            results.append(VisionResult(frame=frame))
            continue

        results.append(_build_vision_result(frame, data_list[i]))

    return results


def _build_vision_result(frame: ExtractedFrame, data: dict) -> VisionResult:
    """Convert a single JSON object from the batch array into a VisionResult."""
    result = VisionResult(frame=frame)

    if not isinstance(data, dict):
        return result

    name = data.get("name")
    confidence = float(data.get("confidence", 0.0))
    place_type = (data.get("place_type") or "").lower().strip() or None

    # Reject if no name, low confidence, or not a visitable place type
    if not name or confidence < settings.min_vision_confidence:
        return result
    if place_type and place_type not in VALID_PLACE_TYPES:
        logger.debug(
            f"Frame {frame.scene_index}: rejected type '{place_type}' for '{name}'"
        )
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


# ── Consensus requery (second pass) ───────────────────────────────────────────


def _consensus_requery(
    frames: list[ExtractedFrame],
    results: list[VisionResult],
) -> list[VisionResult]:
    """
    Majority-vote on city/country across all high-confidence results.
    Re-query outlier frames (as a second batch) with the consensus city locked in.
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
        logger.info("No consensus city (all frames disagree) — skipping requery")
        return results

    outlier_indices = [
        i for i, r in enumerate(results)
        if r.confidence >= settings.min_vision_confidence and r.landmark_name
        and (
            (r.city and r.country and (
                r.city.strip().lower() != consensus_city_lower
                or r.country.strip().lower() != consensus_country_lower
            ))
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
        f"re-querying {len(outlier_indices)} outlier frame(s) in second batch"
    )

    city_ctx = CaptionContext(
        raw="", place_hints=[], city=consensus_city, country=consensus_country,
    )
    constrained_prompt = _build_batch_prompt(len(outlier_indices), city_ctx)
    outlier_frames = [frames[i] for i in outlier_indices]

    try:
        requeried = _run_batch_with_retry(outlier_frames, constrained_prompt)
    except Exception as e:
        logger.warning(f"Consensus requery batch failed: {e}")
        return results

    updated = list(results)
    for local_idx, global_idx in enumerate(outlier_indices):
        if local_idx >= len(requeried):
            continue
        requeried_result = requeried[local_idx]
        original = results[global_idx]
        if requeried_result.confidence >= settings.min_vision_confidence:
            logger.info(
                f"Frame {frames[global_idx].scene_index}: corrected "
                f"'{original.landmark_name}' ({original.city}) → "
                f"'{requeried_result.landmark_name}' ({requeried_result.city})"
            )
            updated[global_idx] = requeried_result
        else:
            logger.info(
                f"Frame {frames[global_idx].scene_index}: dropping "
                f"'{original.landmark_name}' ({original.city}) — "
                f"could not verify in {consensus_city}"
            )
            updated[global_idx] = VisionResult(frame=frames[global_idx])

    return updated
