"""
Vertex AI / Gemini helper.

Authenticates using a service account JSON string (GOOGLE_SERVICE_ACCOUNT_JSON env var),
then calls the Vertex AI generateContent REST API directly.


Optimisations:
- Token is cached for 55 minutes (tokens are valid 1 hour) — avoids a Google OAuth
  round-trip on every request.
- Single persistent httpx.AsyncClient shared across all calls.
"""
import asyncio
import json
import logging
import random
import time

logger = logging.getLogger(__name__)
import httpx
from functools import lru_cache
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
import google.auth
from app.config import settings
from app.core.async_http import loop_client

# Legacy alias — prefer brain()/muscle() below; model IDs come from config.
MODEL_NAME = settings.vertex_model_brain
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _regions() -> list[str]:
    """Ordered list of Vertex regions to try: primary first, then fallbacks.

    Each region has independent quota, so on a 429/503 we switch to the next
    region and retry immediately rather than sleeping through a backoff in a
    region that's already saturated. Deduped, order preserved.
    """
    primary = (settings.gcp_location or "us-central1").strip()
    extra = [r.strip() for r in (settings.gcp_locations or "").split(",") if r.strip()]
    ordered: list[str] = []
    for region in [primary, *extra]:
        if region and region not in ordered:
            ordered.append(region)
    return ordered

# ── Shared HTTP client ────────────────────────────────────────────────────────
# Bound to the running event loop — see app.core.async_http for why a plain
# module-level client breaks under Celery's per-task asyncio.run() loops.
_clients_by_loop: dict[int, httpx.AsyncClient] = {}


def _get_http_client() -> httpx.AsyncClient:
    return loop_client(
        _clients_by_loop,
        lambda: httpx.AsyncClient(
            timeout=180.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        ),
    )

# ── Token cache ───────────────────────────────────────────────────────────────
_token_cache: dict = {"token": None, "expiry": 0.0, "project_id": ""}


@lru_cache(maxsize=1)
def _get_sa_info() -> dict | None:
    """Returns parsed service account dict, or None if JSON env var is not set."""
    raw = (settings.google_service_account_json or "").strip()
    if not raw:
        return None
    sa = json.loads(raw)
    # Env var managers (Coolify, Docker, etc.) often store literal \n instead of
    # real newlines. The RSA signer requires actual newlines in the PEM key.
    if "private_key" in sa and isinstance(sa["private_key"], str):
        sa["private_key"] = sa["private_key"].replace("\\n", "\n")
    return sa


def _get_access_token() -> tuple[str, str]:
    """Returns (access_token, project_id). Token is cached for 55 minutes."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expiry"]:
        return _token_cache["token"], _token_cache["project_id"]

    sa_info = _get_sa_info()
    if sa_info:
        # Explicit service account JSON provided
        creds = service_account.Credentials.from_service_account_info(sa_info, scopes=_SCOPES)
        creds.refresh(GoogleAuthRequest())
        project_id = sa_info["project_id"]
    else:
        # Fall back to Application Default Credentials (ADC)
        creds, project_id = google.auth.default(scopes=_SCOPES)
        creds.refresh(GoogleAuthRequest())
        project_id = project_id or settings.gcp_project or ""
        if not project_id:
            raise RuntimeError(
                "Cannot determine GCP project ID: set GOOGLE_SERVICE_ACCOUNT_JSON "
                "or GCP_PROJECT env var, or configure ADC with a project."
            )

    _token_cache["token"] = creds.token
    _token_cache["expiry"] = now + 55 * 60
    _token_cache["project_id"] = project_id
    return creds.token, project_id


async def vertex_generate(
    contents: list[dict] | str,
    *,
    model: str | None = None,
    response_mime_type: str | None = None,
    response_schema: dict | None = None,
    temperature: float | None = None,
    seed: int | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 120.0,
) -> str:
    """
    Call Vertex AI generateContent and return the raw text response.

    contents can be:
      - a string  → wrapped as single user message
      - a list    → passed as-is (role/parts format)

    response_schema: a Gemini-compatible JSON schema — enables native
    structured output (forces responseMimeType to application/json).
    """
    token, project_id = _get_access_token()
    regions = _regions()
    model = model or settings.vertex_model_brain

    if isinstance(contents, str):
        contents = [{"role": "user", "parts": [{"text": contents}]}]

    body: dict = {"contents": contents}
    gc: dict = {
        # Disable extended thinking — cuts latency from ~30-40s to ~5-10s
        # for structured JSON generation tasks that don't need deep reasoning.
        "thinkingConfig": {"thinkingBudget": 0},
    }
    if response_schema:
        gc["responseSchema"] = response_schema
        gc["responseMimeType"] = "application/json"
    elif response_mime_type:
        gc["responseMimeType"] = response_mime_type
    if temperature is not None:
        gc["temperature"] = temperature
    if seed is not None:
        gc["seed"] = seed
    if max_output_tokens:
        gc["maxOutputTokens"] = max_output_tokens
    body["generationConfig"] = gc

    # Retry on 429/503 by rotating through regions. While fresh regions remain
    # in the current cycle we switch almost instantly (tiny jitter only); once
    # every region has been tried we fall back to exponential backoff before
    # cycling again. This keeps a rate-limited call from blocking on long sleeps
    # in a single saturated region (which was blowing the supervisor's budget).
    n_regions = len(regions)
    max_attempts = max(6, n_regions + 2)
    base_delay = 5.0
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    for attempt in range(max_attempts):
        location = regions[attempt % n_regions]
        url = (
            f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}"
            f"/locations/{location}/publishers/google/models/{model}:generateContent"
        )
        r = await _get_http_client().post(url, headers=headers, json=body, timeout=timeout)

        if r.status_code in (429, 503) and attempt < max_attempts - 1:
            next_location = regions[(attempt + 1) % n_regions]
            cycle = (attempt + 1) // n_regions   # how many times we've wrapped the region list
            if cycle == 0 and n_regions > 1:
                # Fresh region still available — switch over near-instantly.
                delay = random.uniform(0.2, 0.8)
            else:
                # Exhausted the region list (or only one region) — back off.
                retry_after = r.headers.get("Retry-After")
                base = float(retry_after) if retry_after else base_delay * (2 ** (cycle - 1))
                delay = min(base + random.uniform(0, base * 0.3), 60.0)
            logger.warning(
                "[vertex] %s in %s — switching to %s (attempt %d/%d) in %.1fs",
                r.status_code, location, next_location, attempt + 1, max_attempts, delay,
            )
            await asyncio.sleep(delay)
            continue

        r.raise_for_status()
        break

    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def extract_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON from a Gemini response."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[: text.rfind("```")]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


# ── Tiered LLM gateway ─────────────────────────────────────────────────────────
#
# brain()  — reasoning tier (itinerary generation, journey tradeoffs, re-ranking,
#            edit-intent routing, QA passes). Quality over cost.
# muscle() — volume tier (extraction, tagging, dedup, summarization, onboarding).
#            Cost/latency over depth.
#
# Model IDs are config-only (settings.vertex_model_brain / _muscle) so tier
# upgrades never touch call sites.

async def brain(contents: list[dict] | str, **kwargs) -> str:
    """Reasoning-tier generate. Same signature as vertex_generate minus model."""
    kwargs.pop("model", None)
    return await vertex_generate(contents, model=settings.vertex_model_brain, **kwargs)


async def muscle(contents: list[dict] | str, **kwargs) -> str:
    """Volume-tier generate. Same signature as vertex_generate minus model."""
    kwargs.pop("model", None)
    return await vertex_generate(contents, model=settings.vertex_model_muscle, **kwargs)


async def generate_json(
    contents: list[dict] | str,
    *,
    tier: str = "muscle",
    response_schema: dict | None = None,
    max_muscle_attempts: int = 2,
    **kwargs,
) -> dict:
    """
    Structured-output call with the fallback ladder:
    muscle fails to produce valid JSON `max_muscle_attempts` times → escalate
    that single call to brain. brain-tier calls get one retry, no escalation.

    Returns the parsed dict; raises the last error if every rung fails.
    """
    kwargs.pop("model", None)
    kwargs.pop("response_mime_type", None)
    models = (
        [settings.vertex_model_muscle] * max_muscle_attempts + [settings.vertex_model_brain]
        if tier == "muscle"
        else [settings.vertex_model_brain] * 2
    )
    last_err: Exception | None = None
    for i, model in enumerate(models):
        try:
            raw = await vertex_generate(
                contents, model=model, response_schema=response_schema,
                response_mime_type=None if response_schema else "application/json",
                **kwargs,
            )
            return extract_json(raw)
        except Exception as e:  # noqa: BLE001 — ladder decides, caller sees final error
            last_err = e
            escalating = tier == "muscle" and i == max_muscle_attempts - 1
            logger.warning(
                "[llm-gateway] %s attempt %d failed (%s: %s)%s",
                model, i + 1, type(e).__name__, e,
                " — escalating to brain" if escalating else "",
            )
    assert last_err is not None
    raise last_err
