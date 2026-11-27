"""
Free LLM backend — OpenRouter-compatible (OpenAI-style) transport.

This module is the zero-cost alternative to the Vertex AI transport in
``app.services.vertex``. When ``settings.llm_backend == "openrouter"`` the two
public entry points in ``vertex.py`` (``vertex_generate`` /
``vertex_generate_call``) delegate here, so every call site in the codebase
(trip planner, journey reasoning, marketplace extraction, onboarding, the
agent runtime) keeps working unchanged — only the vendor underneath changes.

Design notes:
- The app speaks Gemini's generateContent shape internally. This module
  translates Gemini ``contents``/``tools``/``generationConfig`` into OpenAI
  chat-completions payloads and translates the response back into the Gemini
  candidate shape callers expect.
- Free models are rate-limited in shared pools, so calls rotate through a
  fallback list on 429/5xx with small backoff, then raise.
- Structured output: prefer the provider's JSON-schema response format; if a
  model rejects it, fall back to plain JSON mode, then to a prompt
  instruction. ``vertex.extract_json`` already strips fences downstream.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

# OpenRouter's account-level "free-models-per-day" 429 means every free model
# on the key is dead until the daily reset. Sweeping the fallback list anyway
# burns minutes while a trip spinner sits at "Starting", so the first such
# 429 short-circuits all calls for a while (re-probed at most every 30 min,
# since the reset moment is provider-side and unknown).
_daily_quota_blocked_until = 0.0  # time.monotonic() deadline
_DAILY_QUOTA_MARKERS = ("free-models-per-day", "free models per day",
                        "daily quota")


class FreeLLMError(RuntimeError):
    """All fallback models failed."""


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.openrouter_base_url.rstrip("/"),
            timeout=httpx.Timeout(120.0),
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key or ''}",
                "Content-Type": "application/json",
                "X-Title": settings.app_brand_name,
            },
        )
    return _client


# ── Gemini → OpenAI translation ───────────────────────────────────────────────

def _part_to_openai(part: dict) -> dict | None:
    """One Gemini part → one OpenAI content part (or None to skip)."""
    if "text" in part:
        return {"type": "text", "text": part["text"]}
    blob = part.get("inline_data") or part.get("inlineData")
    if blob and blob.get("data"):
        mime = blob.get("mime_type") or blob.get("mimeType") or "image/jpeg"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{blob['data']}"},
        }
    return None


def _contents_to_messages(contents: list[dict] | str) -> list[dict]:
    if isinstance(contents, str):
        return [{"role": "user", "content": contents}]
    messages: list[dict] = []
    for turn in contents:
        role = "assistant" if turn.get("role") == "model" else "user"
        parts = turn.get("parts") or []
        # Function calls / responses ride along in agent loops.
        fn_calls = [p["functionCall"] for p in parts if "functionCall" in p]
        fn_resps = [p["functionResponse"] for p in parts if "functionResponse" in p]
        if fn_calls:
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": fc.get("name", "tool"),
                        "arguments": json.dumps(fc.get("args", {})),
                    },
                } for i, fc in enumerate(fn_calls)],
            })
            continue
        if fn_resps:
            for i, fr in enumerate(fn_resps):
                messages.append({
                    "role": "tool",
                    "tool_call_id": f"call_{i}",
                    "content": json.dumps(fr.get("response", {})),
                })
            continue
        oa_parts = [p for p in (_part_to_openai(p) for p in parts) if p]
        if not oa_parts:
            continue
        if all(p["type"] == "text" for p in oa_parts):
            messages.append({"role": role, "content": "\n".join(p["text"] for p in oa_parts)})
        else:
            messages.append({"role": role, "content": oa_parts})
    return messages


def _tools_to_openai(declarations: list[dict]) -> list[dict]:
    tools = []
    for decl in declarations:
        tools.append({
            "type": "function",
            "function": {
                "name": decl.get("name", "tool"),
                "description": decl.get("description", ""),
                "parameters": decl.get("parameters", {"type": "object", "properties": {}}),
            },
        })
    return tools


def _choice_to_candidate(data: dict) -> dict:
    """OpenAI chat completion → Gemini generateContent response shape."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    parts: list[dict] = []
    if msg.get("content"):
        parts.append({"text": msg["content"]})
    if not parts and msg.get("content") is not None and not msg.get("tool_calls"):
        # An empty string answer is useless downstream — keep one empty text
        # part so callers don't IndexError, and let _chat's validation retry.
        parts.append({"text": ""})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        parts.append({"functionCall": {"name": fn.get("name", "tool"), "args": args}})
    usage = data.get("usage") or {}
    return {
        "candidates": [{
            "content": {"role": "model", "parts": parts},
            "finishReason": (choice.get("finish_reason") or "").upper() or None,
        }],
        "usageMetadata": {
            "promptTokenCount": usage.get("prompt_tokens", 0),
            "candidatesTokenCount": usage.get("completion_tokens", 0),
            "totalTokenCount": usage.get("total_tokens", 0),
        },
    }


def _fallback_models(primary: str) -> list[str]:
    models = [primary]
    for m in (settings.openrouter_fallback_models or "").split(","):
        m = m.strip()
        if m and m not in models:
            models.append(m)
    return models


def _usable(data: dict) -> bool:
    """Some free-pool models return 200 with an error body or an empty message.
    Both are useless to callers — treat them as a failed attempt."""
    if not isinstance(data, dict) or data.get("error"):
        return False
    choices = data.get("choices") or []
    if not choices:
        return False
    msg = choices[0].get("message") or {}
    return bool(msg.get("content") or msg.get("tool_calls"))


async def _chat(body: dict, *, models: list[str], timeout: float) -> dict:
    """Try each model in turn on rate-limit / transient errors."""
    global _daily_quota_blocked_until
    if time.monotonic() < _daily_quota_blocked_until:
        raise FreeLLMError("free-models daily quota exhausted (cached)")
    client = _get_client()
    last_err: Exception | None = None
    for attempt, model in enumerate(models):
        # Free reasoning models would otherwise spend the whole token budget on
        # hidden thinking and return empty content.
        payload = {**body, "model": model, "reasoning": {"exclude": True}}
        for retry in range(2):  # one same-model retry, then move on
            try:
                resp = await client.post("/chat/completions", json=payload, timeout=timeout)
                if resp.status_code in (429, 500, 502, 503, 504):
                    if resp.status_code == 429:
                        try:
                            body_l = resp.text[:600].lower()
                        except Exception:  # noqa: BLE001
                            body_l = ""
                        if any(mk in body_l for mk in _DAILY_QUOTA_MARKERS):
                            _daily_quota_blocked_until = time.monotonic() + 1800
                            logger.warning("[freellm] %s → daily free quota exhausted; failing fast (no fallback sweep)", model)
                            raise FreeLLMError(
                                f"{model}: free daily quota exhausted (free-models-per-day)")
                    ra = resp.headers.get("retry-after")
                    delay = min(float(ra), 8.0) if ra else min(2.0 * (retry + 1), 6.0)
                    logger.warning("[freellm] %s → %s, backing off %.1fs", model, resp.status_code, delay)
                    await asyncio.sleep(delay)
                    last_err = FreeLLMError(f"{model}: HTTP {resp.status_code}")
                    continue
                if resp.status_code == 400 and "response_format" in payload:
                    # Model/provider can't do structured output — degrade to plain.
                    logger.warning("[freellm] %s rejected response_format, retrying without it", model)
                    payload.pop("response_format", None)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not _usable(data):
                    logger.warning("[freellm] %s returned an empty/error body, moving on", model)
                    last_err = FreeLLMError(f"{model}: empty or error response")
                    break  # next model, not a same-model retry
                return data
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_err = exc
                await asyncio.sleep(min(2.0 * (retry + 1), 6.0))
    raise last_err or FreeLLMError("all fallback models failed")


def _tier_model(requested: str | None) -> str:
    """Map a Gemini tier id from config onto the configured free models."""
    if requested and requested == settings.vertex_model_muscle:
        return settings.openrouter_model_muscle
    if requested and requested == settings.vertex_model_brain:
        return settings.openrouter_model_brain
    return settings.openrouter_model_brain


async def or_generate(
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
    """Drop-in replacement for ``vertex.vertex_generate`` (returns text)."""
    messages = _contents_to_messages(contents)
    body: dict[str, Any] = {"messages": messages}
    if response_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "response", "schema": response_schema},
        }
    elif response_mime_type == "application/json":
        body["response_format"] = {"type": "json_object"}
    if temperature is not None:
        body["temperature"] = temperature
    if max_output_tokens:
        body["max_tokens"] = max_output_tokens

    data = await _chat(body, models=_fallback_models(_tier_model(model)), timeout=timeout)
    return _choice_to_candidate(data)["candidates"][0]["content"]["parts"][0]["text"]


async def or_generate_call(
    contents: list[dict],
    *,
    model: str,
    system_instruction: str | None = None,
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    thinking_budget: int | None = None,  # accepted for signature parity; unused
    timeout: float = 120.0,
    trace: Any | None = None,  # CallTrace passthrough; unused here
) -> dict:
    """Drop-in replacement for ``vertex.vertex_generate_call`` (candidate out)."""
    messages = _contents_to_messages(contents)
    if system_instruction:
        messages = [{"role": "system", "content": system_instruction}] + messages
    body: dict[str, Any] = {"messages": messages}
    if tools:
        body["tools"] = _tools_to_openai(tools)
        body["tool_choice"] = "auto"
    if temperature is not None:
        body["temperature"] = temperature
    if max_output_tokens:
        body["max_tokens"] = max_output_tokens

    data = await _chat(body, models=_fallback_models(_tier_model(model)), timeout=timeout)
    return _choice_to_candidate(data)


async def vision_generate(
    prompt: str,
    frames_b64: list[tuple[str, str]],
    *,
    max_output_tokens: int | None = None,
    timeout: float = 180.0,
) -> str:
    """Vision call for the reel pipeline: one prompt + (mime, b64) frames.

    Uses the configured free vision-capable model; the caller parses the
    returned text (JSON) the same way it parsed Gemini Vision output.
    """
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for mime, data in frames_b64:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    body: dict[str, Any] = {"messages": [{"role": "user", "content": parts}]}
    if max_output_tokens:
        body["max_tokens"] = max_output_tokens
    data = await _chat(body, models=_fallback_models(settings.openrouter_model_vision), timeout=timeout)
    return _choice_to_candidate(data)["candidates"][0]["content"]["parts"][0]["text"]


def _chat_sync(body: dict, *, models: list[str], timeout: float) -> dict:
    """Synchronous twin of _chat for the Celery-side vision pipeline."""
    last_err: Exception | None = None
    with httpx.Client(
        base_url=settings.openrouter_base_url.rstrip("/"),
        timeout=httpx.Timeout(timeout),
        headers={
            "Authorization": f"Bearer {settings.openrouter_api_key or ''}",
            "Content-Type": "application/json",
            "X-Title": settings.app_brand_name,
        },
    ) as client:
        for model in models:
            payload = {**body, "model": model, "reasoning": {"exclude": True}}
            for retry in range(2):
                try:
                    resp = client.post("/chat/completions", json=payload)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        delay = min(2.0 * (retry + 1), 6.0)
                        logger.warning("[freellm] %s → %s, backing off %.1fs", model, resp.status_code, delay)
                        time.sleep(delay)
                        last_err = FreeLLMError(f"{model}: HTTP {resp.status_code}")
                        continue
                    resp.raise_for_status()
                    data = resp.json()
                    if not _usable(data):
                        logger.warning("[freellm] %s returned an empty/error body, moving on", model)
                        last_err = FreeLLMError(f"{model}: empty or error response")
                        break
                    return data
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_err = exc
                    time.sleep(min(2.0 * (retry + 1), 6.0))
    raise last_err or FreeLLMError("all fallback models failed")


def vision_generate_sync(
    prompt: str,
    system_instruction: str | None,
    frames_b64: list[tuple[str, str]],
    *,
    max_output_tokens: int | None = None,
    timeout: float = 240.0,
) -> str:
    """Sync vision call used by the reel worker (runs outside an event loop)."""
    parts: list[dict] = []
    for mime, data in frames_b64:
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
    parts.append({"type": "text", "text": prompt})
    messages: list[dict] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": parts})
    body: dict[str, Any] = {"messages": messages, "temperature": 0.1}
    if max_output_tokens:
        body["max_tokens"] = max_output_tokens
    data = _chat_sync(body, models=_fallback_models(settings.openrouter_model_vision), timeout=timeout)
    return _choice_to_candidate(data)["candidates"][0]["content"]["parts"][0]["text"]
