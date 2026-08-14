"""
Providers — the only place a model vendor is named.

The runtime never imports OpenAI. It asks a `Provider` for the next step and
gets back one of two things: a reply, or a list of tool calls. Everything a
vendor does differently — argument encoding, the shape of a tool result message,
what a finish reason is called — is absorbed here.

Written over `httpx` rather than a vendor SDK, deliberately. The surface we use
is one POST; an SDK would add a dependency, a second retry policy and a set of
exception types that would end up leaking into the runtime's control flow. The
`Provider` protocol is what makes swapping vendors a new class in this file
rather than a change anywhere else.

`EchoProvider` is not a mock in the pejorative sense — it is a scripted
provider, and it is how every runtime test asserts on database state rather than
on text. A test that needs a live model to prove that a complaint becomes a task
is not testing the thing it thinks it is.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import settings
from app.services.ai.errors import ProviderError

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ModelStep:
    """One turn of the model: it either speaks or it calls tools."""

    reply: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    # The vendor's own representation of the assistant turn, echoed back
    # unchanged on the next call. Kept opaque so a vendor that needs more than
    # role/content does not require a runtime change.
    raw: dict | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider(Protocol):
    name: str

    async def step(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        params: dict,
    ) -> ModelStep: ...

    def tool_result_message(self, call: ToolCall, content: str) -> dict: ...


# ── OpenAI ───────────────────────────────────────────────────────────────────

class OpenAIProvider:
    """
    Chat Completions with tools.

    Chosen over the Responses API because the runtime already owns the
    conversation state — it has to, since every tool result is an `agent_actions`
    row — and a server-held thread would be a second place that state lives.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.openai_api_key
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")

    async def step(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        params: dict,
    ) -> ModelStep:
        if not self.api_key:
            raise ProviderError("OPENAI_API_KEY is not configured")

        body: dict = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if "temperature" in params:
            body["temperature"] = params["temperature"]
        if "max_output_tokens" in params:
            body["max_completion_tokens"] = params["max_output_tokens"]

        try:
            async with httpx.AsyncClient(timeout=settings.openai_timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ProviderError(f"could not reach the model: {exc}") from exc

        if resp.status_code >= 400:
            # The body can carry an API key in an echoed request under some
            # error shapes, so only the message is logged and only the status
            # reaches the audit trail.
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise ProviderError(f"model returned {resp.status_code}: {detail}")

        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        usage = data.get("usage") or {}

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # Malformed arguments are the model's mistake, not an outage.
                # Passed through as empty so the tool's own validation produces
                # the refusal and the model is told what was wrong with it.
                args = {}
            calls.append(ToolCall(id=raw.get("id") or "", name=fn.get("name") or "", arguments=args))

        return ModelStep(
            reply=message.get("content"),
            tool_calls=calls,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            raw=message,
        )

    def tool_result_message(self, call: ToolCall, content: str) -> dict:
        return {"role": "tool", "tool_call_id": call.id, "content": content}


# ── the scripted provider the tests run on ───────────────────────────────────

class EchoProvider:
    """
    A provider that follows a script.

    Each entry is either a string (speak it) or a list of (tool, arguments)
    pairs (call them). It records what it was sent, so a test can assert that a
    tool the agent has no grant for was never offered to the model in the first
    place.
    """

    name = "echo"

    def __init__(self, script: list):
        self.script = list(script)
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list[list[str]] = []

    async def step(self, *, model: str, messages: list[dict], tools: list[dict], params: dict) -> ModelStep:
        self.seen_messages.append(list(messages))
        self.seen_tools.append([t["function"]["name"] for t in tools])

        if not self.script:
            return ModelStep(reply="", tokens_in=1, tokens_out=1)

        nxt = self.script.pop(0)
        if isinstance(nxt, str):
            return ModelStep(reply=nxt, tokens_in=10, tokens_out=5)
        if isinstance(nxt, BaseException):
            raise nxt

        calls = [
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(nxt)
        ]
        return ModelStep(tool_calls=calls, tokens_in=10, tokens_out=5,
                         raw={"role": "assistant", "content": None})

    def tool_result_message(self, call: ToolCall, content: str) -> dict:
        return {"role": "tool", "tool_call_id": call.id, "content": content}


_PROVIDERS: dict[str, type] = {"openai": OpenAIProvider}


def for_name(name: str) -> Provider:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ProviderError(f"unknown provider {name!r}")
    return cls()
