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

MODEL_NAME = "gemini-2.5-flash"
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# ── Shared HTTP client ────────────────────────────────────────────────────────
_http_client = httpx.AsyncClient(
    timeout=180.0,
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
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
    model: str = MODEL_NAME,
    response_mime_type: str | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    timeout: float = 120.0,
) -> str:
    """
    Call Vertex AI generateContent and return the raw text response.

    contents can be:
      - a string  → wrapped as single user message
      - a list    → passed as-is (role/parts format)
    """
    token, project_id = _get_access_token()
    location = settings.gcp_location or "us-central1"

    if isinstance(contents, str):
        contents = [{"role": "user", "parts": [{"text": contents}]}]

    body: dict = {"contents": contents}
    gc: dict = {
        # Disable extended thinking — cuts latency from ~30-40s to ~5-10s
        # for structured JSON generation tasks that don't need deep reasoning.
        "thinkingConfig": {"thinkingBudget": 0},
    }
    if response_mime_type:
        gc["responseMimeType"] = response_mime_type
    if temperature is not None:
        gc["temperature"] = temperature
    if max_output_tokens:
        gc["maxOutputTokens"] = max_output_tokens
    body["generationConfig"] = gc

    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project_id}"
        f"/locations/{location}/publishers/google/models/{model}:generateContent"
    )

    # Retry with exponential backoff on 429 / 503
    max_attempts = 6
    base_delay = 5.0
    for attempt in range(max_attempts):
        r = await _http_client.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        if r.status_code in (429, 503) and attempt < max_attempts - 1:
            # Honour Retry-After header if present, otherwise exponential backoff + jitter
            retry_after = r.headers.get("Retry-After")
            base = float(retry_after) if retry_after else base_delay * (2 ** attempt)
            jitter = random.uniform(0, base * 0.3)   # ±30% jitter to spread retries
            delay = min(base + jitter, 120.0)         # cap at 2 minutes
            logger.warning("[vertex] %s rate limit — retry %d/%d in %.1fs", r.status_code, attempt + 1, max_attempts - 1, delay)
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
