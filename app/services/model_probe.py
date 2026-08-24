"""
Can we actually call this model, from here, right now?

The diagnostic that did not exist, and whose absence is the whole reason a
reachable model family was recorded in `config.py` as unavailable for months.
The 404 was real; the conclusion drawn from it — "the models are not enabled for
this project" — was not, and nothing in the system could tell the two apart
because nothing ever asked the question in a form that produced an answer.

    gemini-3.5-flash / asia-south1
      reachable      yes
      latency        1.8s
      tool calling   yes
      schema output  yes

**Not on the request path.** A probe costs a real generateContent call, so it
belongs to startup validation, a health endpoint, a deploy check and an operator
running it by hand — never to a guest turn. Nothing here is cached for the
runtime to consult, deliberately: a cache would make this a dependency, and the
runtime already has the right behaviour when a model fails, which is to fall
back to the next one.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.services import model_catalog

logger = logging.getLogger(__name__)

# Small, cheap, and shaped like the two things the agent runtime actually needs.
# The prompt is deliberately trivial: this measures reachability, not quality.
_PROMPT = "Reply with the single word: ok"

_TOOL = {
    "name": "probe_tool",
    "description": "A probe. Call it with the city named in the message.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
}

_SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}},
    "required": ["city"],
}


@dataclass
class ProbeResult:
    """One model in one location, and what it can do there."""

    model: str
    location: str
    reachable: bool = False
    latency_ms: int = 0
    # The class name from `vertex.classify_status` — the actionable half. A probe
    # that fails with `VertexModelNotFound` and a probe that fails with
    # `VertexAccessDenied` call for opposite fixes.
    error_class: str = ""
    reason: str = ""
    # Retries the probe itself needed before the model answered. Kept apart from
    # latency because they measure different things: latency is how fast the
    # model is, this is how contended it is, and adding the backoff sleeps into
    # the first number reports a fast model as a slow one.
    retries: int = 0
    tool_calling: bool | None = None
    response_schema: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def agent_ready(self) -> bool:
        """Whether the agent runtime could actually run on this."""
        return bool(self.reachable and self.tool_calling and self.response_schema)

    def line(self) -> str:
        if not self.reachable:
            return (f"{self.model} / {self.location}\n"
                    f"  status: UNREACHABLE ({self.error_class})\n"
                    f"  reason: {self.reason}")
        return (
            f"{self.model} / {self.location}\n"
            f"  status: {'healthy' if self.agent_ready else 'degraded'}\n"
            f"  latency: {self.latency_ms / 1000:.1f}s"
            + (f"  (after {self.retries} quota retr"
               f"{'y' if self.retries == 1 else 'ies'})" if self.retries else "")
            + "\n"
            f"  tool_calling: {str(bool(self.tool_calling)).lower()}\n"
            f"  response_schema: {str(bool(self.response_schema)).lower()}"
        )


async def probe(model: str, location: str, *, timeout: float = 90.0,
                check_capabilities: bool = True, attempts: int = 3) -> ProbeResult:
    """
    One model, one location. Never raises — an unreachable model is the answer,
    not an error, because the caller is asking precisely in order to find out.

    **A transient 429 is not a capability verdict.** The first version of this
    reported `gemini-3.6-flash` as "schema output: no" because the third of its
    three probe calls landed on a quota blip, which is a health check inventing
    an outage. Quota and timeouts are retried; a 404 and a 403 are not, because
    those are facts and repeating them is just slower.

    The timeout is generous for the same reason: a thinking model doing
    structured output legitimately takes longer than a greeting, and a ceiling
    tuned to the greeting reports the model as broken.
    """
    from app.services.vertex import (
        VertexError, VertexQuotaExhausted, VertexTimeout,
        _get_access_token, _get_http_client, get_vertex_host,
    )
    import httpx

    result = ProbeResult(model=model, location=location)
    # Restarted on each retry so `latency` times the call that answered rather
    # than the backoff in front of it.
    nonlocal_clock = [time.monotonic()]

    async def call(body: dict) -> dict:
        last: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                result.retries += 1
            token, project_id = _get_access_token()
            url = (
                f"https://{get_vertex_host(location)}/v1/projects/{project_id}"
                f"/locations/{location}/publishers/google/models/{model}:generateContent"
            )
            try:
                r = await _get_http_client().post(
                    url,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json=body,
                    timeout=timeout,
                )
            except httpx.TimeoutException as exc:
                last = VertexTimeout(f"timed out after {timeout:.0f}s",
                                     model=model, location=location)
                if attempt + 1 < attempts:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    nonlocal_clock[0] = time.monotonic()
                    continue
                raise last from exc

            if r.status_code >= 400:
                from app.services.vertex import classify_status
                detail = ""
                try:
                    detail = str((r.json().get("error") or {}).get("message", ""))[:200]
                except Exception:  # noqa: BLE001
                    detail = r.text[:200]
                exc = classify_status(r.status_code, detail,
                                      model=model, location=location)
                if isinstance(exc, VertexQuotaExhausted) and attempt + 1 < attempts:
                    last = exc
                    await asyncio.sleep(3.0 * (attempt + 1))
                    nonlocal_clock[0] = time.monotonic()
                    continue
                raise exc
            return r.json()
        assert last is not None
        raise last

    try:
        nonlocal_clock[0] = time.monotonic()
        await call({
            "contents": [{"role": "user", "parts": [{"text": _PROMPT}]}],
            # Generous: a thinking model spends its first tokens thinking, and a
            # ceiling low enough to truncate that produces an empty candidate
            # that reads like a failure.
            "generationConfig": {"maxOutputTokens": 600},
        })
    except VertexError as exc:
        result.latency_ms = int((time.monotonic() - nonlocal_clock[0]) * 1000)
        result.error_class = type(exc).__name__
        result.reason = str(exc)[:300]
        return result
    except Exception as exc:  # noqa: BLE001
        result.latency_ms = int((time.monotonic() - nonlocal_clock[0]) * 1000)
        result.error_class = type(exc).__name__
        result.reason = str(exc)[:300]
        return result

    result.reachable = True
    result.latency_ms = int((time.monotonic() - nonlocal_clock[0]) * 1000)

    if not check_capabilities:
        return result

    # Tool calling. The agent runtime is a tool loop; a model that cannot be
    # given function declarations cannot serve it however fast it answers.
    try:
        data = await call({
            "contents": [{"role": "user", "parts": [
                {"text": "Call probe_tool with the city Goa."}
            ]}],
            "tools": [{"functionDeclarations": [_TOOL]}],
        })
        parts = ((data.get("candidates") or [{}])[0]
                 .get("content", {}).get("parts") or [])
        result.tool_calling = any("functionCall" in p for p in parts)
        if not result.tool_calling:
            result.notes.append("accepted a tool declaration but did not call it")
    except Exception as exc:  # noqa: BLE001
        result.tool_calling = False
        result.notes.append(f"tool calling: {type(exc).__name__}")

    # Structured output. `generate_json` and the classifier both depend on it.
    try:
        data = await call({
            "contents": [{"role": "user", "parts": [{"text": "The city is Goa."}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _SCHEMA,
                "maxOutputTokens": 600,
            },
        })
        parts = ((data.get("candidates") or [{}])[0]
                 .get("content", {}).get("parts") or [])
        text = "".join(p.get("text", "") for p in parts)
        result.response_schema = "city" in text
        if not result.response_schema:
            result.notes.append("responseSchema accepted but output did not match")
    except Exception as exc:  # noqa: BLE001
        result.response_schema = False
        result.notes.append(f"response schema: {type(exc).__name__}")

    return result


async def probe_model(model: str, *, configured: list[str] | None = None,
                      first_only: bool = True, **kwargs) -> list[ProbeResult]:
    """
    A model in the locations it is actually served from.

    `first_only` is the deploy check: the preferred location is the one traffic
    will use, and probing all four of `gemini-3.5-flash`'s costs four calls to
    answer a question the first one answered. Pass False for the full picture.
    """
    from app.services.vertex import _regions

    locations = model_catalog.locations_for(model, configured or _regions())
    if first_only:
        locations = locations[:1]
    return [await probe(model, loc, **kwargs) for loc in locations]


async def probe_production_tiers(**kwargs) -> list[ProbeResult]:
    """
    Every model a StayOS tier can currently route to, including its fallbacks.

    This is the one to run before and after a deploy. It answers "can this
    build serve traffic", which is not the same as "is Vertex up": a tier whose
    primary is unreachable but whose fallback answers is degraded, not down, and
    the difference decides whether anybody is woken up.
    """
    from app.services.ai import model_router

    wanted: list[str] = []
    for tier in (model_router.CLASSIFY, model_router.FAST,
                 model_router.CONVERSATION, model_router.REASONING,
                 model_router.DEEP):
        # The chain, not just the head: a tier is only as available as the
        # models it can degrade to, and a fallback nobody probed is a fallback
        # discovered to be broken at the moment it is needed.
        model = model_router.model_for_tier(tier)
        for candidate in [model,
                          *model_catalog.fallback_chain(model, include_self=False)]:
            if candidate not in wanted:
                wanted.append(candidate)

    results: list[ProbeResult] = []
    for model in wanted:
        results.extend(await probe_model(model, **kwargs))
    return results


def summarise(results: list[ProbeResult]) -> str:
    """A block an operator can read, and a deploy log can keep."""
    ready = [r for r in results if r.agent_ready]
    lines = [f"{len(ready)}/{len(results)} model endpoints healthy", ""]
    for r in results:
        lines.append(r.line())
        for note in r.notes:
            lines.append(f"  note: {note}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    print(summarise(asyncio.run(probe_production_tiers())))
