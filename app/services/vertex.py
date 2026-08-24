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
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
import httpx
from functools import lru_cache
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest
import google.auth
from app.config import settings
from app.core.async_http import loop_client
from app.services import model_catalog

# Legacy alias — prefer brain()/muscle() below; model IDs come from config.
MODEL_NAME = settings.vertex_model_brain
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def get_vertex_host(location: str) -> str:
    """
    The hostname serving a Vertex location.

    **`global` is not a region and takes no prefix.** Every regional endpoint is
    `{location}-aiplatform.googleapis.com`; `global` is bare
    `aiplatform.googleapis.com`, and `global-aiplatform.googleapis.com` does not
    resolve to anything.

    That single asymmetry is why this codebase believed for months that Gemini
    3.x was not enabled for the project. Almost the whole 3.x family is served
    from `global` and nowhere else, the URL was built by string-formatting the
    prefix unconditionally, and the resulting request came back `404` — the same
    status a model genuinely without access returns. The models were reachable
    the entire time.

    One function, called from one place, so the asymmetry cannot be reintroduced
    by a second call site that formats its own URL.
    """
    if location == model_catalog.GLOBAL:
        return "aiplatform.googleapis.com"
    return f"{location}-aiplatform.googleapis.com"


def _regions() -> list[str]:
    """The locations this deployment is willing to use, in preference order.

    The *universe*, not the itinerary: which of these can actually serve a given
    model is `model_catalog.locations_for`, because the two questions have
    different answers and conflating them is what sends a 3.x call to
    us-central1 and a 2.5-flash-lite call to asia-south1, both of which 404.

    Deduped, order preserved.
    """
    primary = (settings.gcp_location or "us-central1").strip()
    extra = [r.strip() for r in (settings.gcp_locations or "").split(",") if r.strip()]
    ordered: list[str] = []
    for region in [primary, *extra]:
        if region and region not in ordered:
            ordered.append(region)
    return ordered


# ── what went wrong ───────────────────────────────────────────────────────────
#
# Vertex answers four very different problems with four status codes, and the
# only one that used to be distinguished was 429. Everything else arrived as
# `httpx.HTTPStatusError` with the body glued into the message, so an operator
# reading a log could not tell "this model id does not exist" from "this service
# account cannot call Vertex" — and the first was diagnosed as the second for
# months. See `get_vertex_host`.
#
# The classes are the diagnosis. `retryable` is the policy that follows from it.


class VertexError(RuntimeError):
    """Base. Carries what is needed to explain the failure without the body."""

    retryable = False

    def __init__(self, message: str, *, model: str = "", location: str = "",
                 status: int | None = None, detail: str = ""):
        super().__init__(message)
        self.model = model
        self.location = location
        self.status = status
        self.detail = detail


class VertexModelNotFound(VertexError):
    """
    404. The model id, or the model/location pair, does not exist.

    **Never a permissions problem, and saying so costs weeks.** The three causes
    are a wrong id (`gemini-3-flash` for `gemini-3-flash-preview`), a model asked
    for in a location that does not serve it (any 3.x model in any region), or a
    malformed endpoint. All three are ours to fix and none of them is access.
    """


class VertexAccessDenied(VertexError):
    """403. Genuinely IAM: the credentials may not call this, or the API is off."""


class VertexQuotaExhausted(VertexError):
    """429 / RESOURCE_EXHAUSTED. Real, and the only one that is about capacity."""

    retryable = True


class VertexUpstreamError(VertexError):
    """5xx. Google's problem, and bounded-retryable."""

    retryable = True


class VertexTimeout(VertexError):
    """
    The request did not come back.

    Retryable at the transport level *only* because `generateContent` is a read:
    it creates nothing and charges for what it produced. A mutation must never
    be retried on a timeout, and none happen here — the tools do that, behind
    the runtime, where idempotency is decided per tool.
    """

    retryable = True


def classify_status(status: int, detail: str = "", *, model: str = "",
                    location: str = "") -> VertexError:
    """One HTTP status → the class that says what to do about it."""
    where = f"{model} in {location}" if location else model
    if status == 404:
        return VertexModelNotFound(
            f"Vertex 404 for {where}: the model id or model/location pair does not "
            f"exist. This is not an access problem — check the id against "
            f"model_catalog, and check the location serves it. {detail}".strip(),
            model=model, location=location, status=status, detail=detail,
        )
    if status == 403:
        return VertexAccessDenied(
            f"Vertex 403 for {where}: the credentials may not call this. {detail}".strip(),
            model=model, location=location, status=status, detail=detail,
        )
    if status == 429:
        return VertexQuotaExhausted(
            f"Vertex 429 for {where}: quota exhausted. {detail}".strip(),
            model=model, location=location, status=status, detail=detail,
        )
    if status >= 500:
        return VertexUpstreamError(
            f"Vertex {status} for {where}: upstream failure. {detail}".strip(),
            model=model, location=location, status=status, detail=detail,
        )
    return VertexError(
        f"Vertex {status} for {where}: {detail}".strip(),
        model=model, location=location, status=status, detail=detail,
    )


# ── what a call did ───────────────────────────────────────────────────────────

@dataclass
class CallTrace:
    """
    How one `generateContent` actually went.

    Filled in by `_post_generate` and read by whoever asked for it. It exists
    because "which model answered, from where, after how many retries" is the
    first question about a slow or expensive turn and none of it was recoverable
    from the response body — the runtime knew which model it *asked* for, which
    is a different fact once a fallback has fired.
    """

    model: str = ""
    location: str = ""
    attempts: int = 0
    retries: int = 0
    locations_tried: list[str] = field(default_factory=list)
    latency_ms: int = 0
    status: str = "ok"

    def as_log(self) -> dict:
        return {
            "model": self.model,
            "location": self.location,
            "attempts": self.attempts,
            "retries": self.retries,
            "latency_ms": self.latency_ms,
            "status": self.status,
        }

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

# ── Region health ─────────────────────────────────────────────────────────────
# A region that just answered 429/503 is saturated for everyone in this process,
# not just the call that hit it. Remember that for a short cooldown so the NEXT
# call starts in a healthy region instead of re-earning the same 429 (and its
# retry sleep) in the primary. Regions cool down automatically; when every
# region is cooling we fall back to the configured order.
_region_cooldown: dict[str, float] = {}
_REGION_COOLDOWN_S = 90.0


def _mark_saturated(region: str, retry_after: str | None = None) -> None:
    try:
        cooldown = float(retry_after) if retry_after else _REGION_COOLDOWN_S
    except ValueError:
        cooldown = _REGION_COOLDOWN_S
    _region_cooldown[region] = time.time() + min(cooldown, 300.0)


def _healthy_first(regions: list[str]) -> list[str]:
    now = time.time()
    healthy = [r for r in regions if _region_cooldown.get(r, 0.0) <= now]
    cooling = [r for r in regions if r not in healthy]
    return healthy + cooling


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

    data = await _post_generate(body, model=model, timeout=timeout)
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def _post_generate(body: dict, *, model: str, timeout: float,
                         trace: CallTrace | None = None) -> dict:
    """
    POST generateContent and return the raw JSON, retrying the way this model
    can actually be retried.

    **Rotation is a capability, not a policy.** The original loop assumed every
    model was served from every configured region and that regions hold
    independent quota, so a 429 meant "go next door". That is true of
    `gemini-2.5-flash`, which answers from thirteen locations. It is false of
    almost the whole 3.x family, which answers from `global` alone — and there,
    rotating meant re-issuing the identical request to the identical pool with
    almost no delay, burning the retry budget to make the same call four times.

    So the itinerary comes from `model_catalog.locations_for`, and the ladder
    forks on how long it is:

        several locations   switch to a fresh one almost instantly (jitter
                            only), and fall back to exponential backoff once
                            every location has been tried.
        one location        exponential backoff with jitter from the first 429.
                            There is nowhere to go; waiting is the strategy.

    A 404 is treated as "not served here" rather than as fatal *while untried
    locations remain* — which makes an uncatalogued model self-correcting
    instead of a hard failure — and raises `VertexModelNotFound` naming
    everywhere it looked once they are exhausted.

    Extracted from `vertex_generate` so the hotel agent runtime can reuse this
    machinery instead of growing a second one. Everything the agent needs that
    text generation does not — function declarations, a system instruction, a
    thinking budget, the usage metadata — differs only in the request body and
    in which part of the response the caller reads. The token cache, the region
    cooldown, the retry ladder and the circuit breaker are the same problem for
    both, and duplicating them would mean a saturated region punishing the agent
    runtime that the trip planner had already learned to avoid.
    """
    token, project_id = _get_access_token()
    locations = model_catalog.locations_for(model, _regions())
    n_locations = len(locations)
    single_pool = n_locations == 1
    max_attempts = max(6, n_locations + 2)
    base_delay = 5.0
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    trace = trace if trace is not None else CallTrace()
    trace.model = model
    started = time.monotonic()

    # Concurrency gate on the tightest external quota (Gemini). Caps simultaneous
    # generateContent calls per process so a burst of trips queues instead of
    # stampeding the quota; also feeds the health dashboard. The breaker keeps its
    # own region-retry loop below (guard imposes no timeout).
    from app.core.circuit_breaker import breaker_gemini, CircuitOpenError

    async def _run() -> dict:
        # Snapshot the rotation order once per call, healthy locations first —
        # a saturated primary is skipped instead of retried by every caller.
        order = _healthy_first(locations)
        # Locations that answered 404: this model is not served there, which is a
        # fact for the whole call rather than a transient. Never retried.
        missing: set[str] = set()
        last_error: VertexError | None = None

        # An explicit cursor rather than `attempt % len(live)`.
        #
        # The modular form is wrong as soon as a location is crossed off: with
        # `[asia-south1, asia-southeast1, global, …]`, a 404 on the first
        # shortens the list, and attempt 1 then indexes past asia-southeast1
        # straight to global — silently skipping a location the model is served
        # from. Removing an entry shifts its successor into the same index, so
        # the cursor advances on a 429 (move on) and holds on a 404 (the next
        # one is already here).
        cursor = 0

        for attempt in range(max_attempts):
            live = [loc for loc in order if loc not in missing]
            if not live:
                break
            cursor %= len(live)
            location = live[cursor]
            trace.attempts = attempt + 1
            trace.location = location
            if location not in trace.locations_tried:
                trace.locations_tried.append(location)

            url = (
                f"https://{get_vertex_host(location)}/v1/projects/{project_id}"
                f"/locations/{location}/publishers/google/models/{model}:generateContent"
            )
            try:
                r = await _get_http_client().post(
                    url, headers=headers, json=body, timeout=timeout
                )
            except httpx.TimeoutException as exc:
                # Safe to retry: generateContent creates nothing. A mutation
                # timing out is a different problem and is not decided here.
                last_error = VertexTimeout(
                    f"Vertex timed out after {timeout:.0f}s for {model} in {location}",
                    model=model, location=location,
                )
                if attempt >= max_attempts - 1:
                    raise last_error from exc
                trace.retries += 1
                cursor += 1
                await asyncio.sleep(min(base_delay * (2 ** attempt), 30.0)
                                    + random.uniform(0, 1.0))
                continue

            if r.status_code == 404 and len(live) > 1:
                # Not served here. Cross it off and try somewhere that does,
                # rather than failing a turn over a location list that is one
                # entry out of date.
                logger.warning(
                    "[vertex] %s is not served from %s — trying the next location",
                    model, location,
                )
                missing.add(location)
                last_error = classify_status(404, model=model, location=location)
                # Cursor held: the next location has shifted into this index.
                continue

            if r.status_code in (429, 503) and attempt < max_attempts - 1:
                _mark_saturated(location, r.headers.get("Retry-After"))
                trace.retries += 1
                retry_after = r.headers.get("Retry-After")

                cursor += 1
                if single_pool or len(live) == 1:
                    # **One quota pool. There is nowhere to rotate to.**
                    # Rotating "global to global" is the same call again, so the
                    # only honest response is to wait — exponentially, with
                    # jitter so a burst of turns does not re-synchronise.
                    base = float(retry_after) if retry_after else base_delay * (2 ** attempt)
                    delay = min(base + random.uniform(0, base * 0.3), 60.0)
                    logger.warning(
                        "[vertex] %s in %s (single pool) — backing off %.1fs "
                        "(attempt %d/%d)",
                        r.status_code, location, delay, attempt + 1, max_attempts,
                    )
                else:
                    cycle = cursor // len(live)   # times we've wrapped the list
                    if cycle == 0:
                        # Fresh location still available — switch near-instantly.
                        delay = random.uniform(0.2, 0.8)
                    else:
                        base = (float(retry_after) if retry_after
                                else base_delay * (2 ** (cycle - 1)))
                        delay = min(base + random.uniform(0, base * 0.3), 60.0)
                    logger.warning(
                        "[vertex] %s in %s — switching to %s (attempt %d/%d) in %.1fs",
                        r.status_code, location,
                        live[cursor % len(live)], attempt + 1, max_attempts, delay,
                    )

                last_error = classify_status(
                    r.status_code, model=model, location=location
                )
                await asyncio.sleep(delay)
                continue

            if r.status_code >= 400:
                # Vertex explains itself in the body and `raise_for_status`
                # throws it away. That cost an afternoon: a multi-tool turn was
                # 400ing with "the number of function response parts must equal
                # the number of function call parts" and the log said only
                # "Client error '400 Bad Request'".
                detail = ""
                try:
                    detail = str((r.json().get("error") or {}).get("message", ""))[:300]
                except Exception:  # noqa: BLE001
                    detail = r.text[:300]
                raise classify_status(
                    r.status_code, detail, model=model, location=location
                )

            return r.json()

        # Out of attempts, or every location said 404.
        if missing and len(missing) == len(order):
            raise VertexModelNotFound(
                f"Vertex 404 for {model}: not served from any of "
                f"{', '.join(sorted(missing))}. Check the id against "
                f"model_catalog — this is not an access problem.",
                model=model, status=404,
            )
        raise last_error or VertexError(f"Vertex gave up on {model}", model=model)

    try:
        async with breaker_gemini.guard():
            data = await _run()
            trace.latency_ms = int((time.monotonic() - started) * 1000)
            if trace.retries:
                logger.info("[vertex] %s", trace.as_log())
            return data
    except CircuitOpenError:
        # Gemini is shedding load (sustained outage). Surface as a normal upstream
        # failure so the supervisor degrades the affected agent, same as a raise
        # from _run would.
        trace.status = "circuit_open"
        trace.latency_ms = int((time.monotonic() - started) * 1000)
        raise RuntimeError("Vertex temporarily unavailable (circuit open)")
    except VertexError as exc:
        trace.status = type(exc).__name__
        trace.latency_ms = int((time.monotonic() - started) * 1000)
        raise


async def vertex_generate_call(
    contents: list[dict],
    *,
    model: str,
    system_instruction: str | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,
    timeout: float = 120.0,
    trace: CallTrace | None = None,
) -> dict:
    """
    generateContent for an agent turn: function declarations in, a candidate out.

    The difference from `vertex_generate` is the whole reason this exists. That
    one returns `parts[0].text` and hardcodes `thinkingBudget: 0`, both of which
    are right for a structured-extraction call and wrong for an agent — an agent
    turn may answer with a `functionCall` part and no text at all, and dropping
    the rest of the candidate would silently discard the tool call.

    Returns the raw candidate plus `usageMetadata`, because the caller needs the
    token counts for the meter and the finish reason for the audit trail.

    `thinking_budget` is passed through rather than fixed: routing decides how
    much reasoning a task is worth (see services/ai/model_router.py), and a
    classification call and a rate negotiation should not cost the same.
    """
    body: dict = {"contents": contents}

    if system_instruction:
        # A separate field, not a first user turn. Gemini treats it differently,
        # and it keeps the hotel's own instructions out of the message list where
        # a later turn could be mistaken for part of the conversation.
        body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
        # AUTO, explicitly. The model decides whether a message needs a tool;
        # forcing a call would make "what time is breakfast" invent one, and
        # forbidding calls would make the agent useless.
        body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

    gc: dict = {}
    if temperature is not None:
        gc["temperature"] = temperature
    if max_output_tokens:
        gc["maxOutputTokens"] = max_output_tokens
    if thinking_budget is not None:
        gc["thinkingConfig"] = {"thinkingBudget": thinking_budget}
    if gc:
        body["generationConfig"] = gc

    return await _post_generate(body, model=model, timeout=timeout, trace=trace)


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
