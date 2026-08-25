"""
Providers — the only place a model vendor is named.

The runtime asks a `Provider` for the next step and gets back one of two things:
a reply, or a list of tool calls. Everything a vendor does differently —
argument encoding, the shape of a tool-result message, what a finish reason is
called — is absorbed here.

**Gemini on Vertex, through `services/vertex.py`.** Not a second Google
integration: the same project, the same service-account credentials, the same
region rotation and cooldown, the same circuit breaker and the same
loop-bound HTTP client that the traveller-side pipeline has been using in
production. A saturated region the trip planner already learned to avoid is a
region this runtime avoids too, which would not be true of a parallel client.

`EchoProvider` is not a mock in the pejorative sense — it is a scripted
provider, and it is how every deterministic test asserts on database state
rather than on text. A test that needs a live model to prove that a complaint
becomes a task is not testing the thing it thinks it is.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.services.ai.errors import AgentError, ProviderError

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
    # Reasoning tokens, when the provider reports them separately. Counted
    # because they are billed, and kept apart from output tokens because they
    # are not the same thing and a rate card will price them differently.
    tokens_thought: int = 0
    finish_reason: str | None = None
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


# ── tool schemas, in Gemini's dialect ────────────────────────────────────────

# Gemini's function declarations take an OpenAPI subset. It rejects the parts of
# JSON Schema that pydantic emits freely, so this narrows rather than translates
# — and narrowing is the right direction: a schema the model half-understands
# produces arguments the tool then rejects, which costs a turn and reads to the
# guest as the agent being confused.
_ALLOWED_KEYS = {"type", "description", "enum", "items", "properties", "required", "nullable"}
_TYPES = {"string", "number", "integer", "boolean", "array", "object"}


def to_gemini_schema(schema: dict) -> dict:
    """Recursively narrow a JSON Schema to what Vertex accepts."""
    if not isinstance(schema, dict):
        return {"type": "string"}

    # pydantic writes `anyOf: [{type: X}, {type: null}]` for `X | None`. Gemini
    # has no anyOf; the nullable member is the whole meaning, so collapse to the
    # non-null branch and mark it nullable.
    if "anyOf" in schema:
        branches = [b for b in schema["anyOf"] if b.get("type") != "null"]
        collapsed = to_gemini_schema(branches[0]) if branches else {"type": "string"}
        if len(branches) < len(schema["anyOf"]):
            collapsed["nullable"] = True
        if schema.get("description"):
            collapsed.setdefault("description", schema["description"])
        return collapsed

    out: dict = {}
    for key, value in schema.items():
        if key not in _ALLOWED_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {k: to_gemini_schema(v) for k, v in value.items()}
        elif key == "items":
            out["items"] = to_gemini_schema(value)
        elif key == "type":
            # `type: [X, null]` is the other spelling of the same thing.
            if isinstance(value, list):
                real = [t for t in value if t != "null"]
                out["type"] = real[0] if real else "string"
                if len(real) < len(value):
                    out["nullable"] = True
            elif value in _TYPES:
                out["type"] = value
        else:
            out[key] = value

    out.setdefault("type", "object" if "properties" in out else "string")

    # Gemini rejects an object with an empty `properties` map. Several tools
    # take no arguments at all — deliberately, because a guest-scoped read must
    # not accept a subject — so they are declared with no parameters rather than
    # with an empty object.
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)

    return out


def to_gemini_tools(described: list[dict]) -> list[dict]:
    """
    `registry.describe_for_provider` output → Gemini function declarations.

    The registry stays the source of truth; this is a projection of it. A tool
    the agent has no grant for never reaches here, because `available_for`
    filtered it out before the description was built.
    """
    declarations = []
    for tool in described:
        fn = tool.get("function") or {}
        parameters = to_gemini_schema(fn.get("parameters") or {})
        declaration: dict = {
            "name": fn.get("name"),
            "description": (fn.get("description") or "")[:1024],
        }
        if parameters.get("properties"):
            declaration["parameters"] = parameters
        declarations.append(declaration)
    return declarations


# ── Vertex ───────────────────────────────────────────────────────────────────

class VertexGeminiProvider:
    """
    Gemini through `services/vertex.vertex_generate_call`.

    Message translation is the whole of this class. Gemini wants
    `{role, parts}` with roles limited to `user` and `model`, tool results as
    `functionResponse` parts on a *user* turn, and the system instruction in its
    own field rather than as a first message. None of that shape reaches the
    runtime, which speaks the same role/content dialect regardless of vendor.
    """

    name = "vertex_ai"

    def __init__(self, timeout: float | None = None):
        from app.config import settings

        self.timeout = timeout or settings.hotel_model_timeout_seconds

    # ── translation ──────────────────────────────────────────────────────────

    @staticmethod
    def _to_contents(messages: list[dict]) -> tuple[str | None, list[dict]]:
        """Split the system instruction out, and convert the rest."""
        system: list[str] = []
        contents: list[dict] = []

        for message in messages:
            role = message.get("role")

            if role == "system":
                system.append(message.get("content") or "")
                continue

            if role == "tool":
                # A tool result. Gemini carries these as a functionResponse part
                # on a user turn — the model is being *told* something, which is
                # the user side of the exchange as far as the protocol cares.
                try:
                    payload = json.loads(message.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {"result": message.get("content")}
                if not isinstance(payload, dict):
                    payload = {"result": payload}

                part = {"functionResponse": {
                    "name": message.get("name") or "tool",
                    "response": payload,
                }}

                # **Merged into one turn, not one turn each.**
                #
                # Gemini requires the number of functionResponse parts to equal
                # the number of functionCall parts in the turn they answer, and
                # counts them per *turn*. Emitting a user turn per result made
                # every multi-tool exchange fail on the second call with
                # "Please ensure that the number of function response parts is
                # equal to the number of function call parts" — a 400 the
                # runtime correctly turned into a handoff, so it looked like an
                # occasional model outage rather than a protocol mistake.
                #
                # It only bites when the model calls several tools at once, which
                # is exactly the behaviour worth having.
                if (contents and contents[-1]["role"] == "user"
                        and all("functionResponse" in p for p in contents[-1]["parts"])):
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue

            if role == "assistant":
                # Either the raw candidate content we stored last turn — which
                # carries the functionCall parts and must go back verbatim — or
                # plain prose from a history replay.
                parts = message.get("parts")
                if parts:
                    contents.append({"role": "model", "parts": parts})
                elif message.get("content"):
                    contents.append({"role": "model", "parts": [{"text": message["content"]}]})
                continue

            if message.get("content"):
                contents.append({"role": "user", "parts": [{"text": message["content"]}]})

        return ("\n\n".join(s for s in system if s) or None), contents

    # ── the call ─────────────────────────────────────────────────────────────

    async def step(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        params: dict,
    ) -> ModelStep:
        from app.services.vertex import vertex_generate_call

        system, contents = self._to_contents(messages)
        if not contents:
            raise ProviderError("nothing to send to the model")

        try:
            data = await vertex_generate_call(
                contents,
                model=model,
                system_instruction=system,
                tools=to_gemini_tools(tools) if tools else None,
                temperature=params.get("temperature"),
                max_output_tokens=params.get("max_output_tokens"),
                thinking_budget=params.get("thinking_budget"),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 — every upstream failure is one thing here
            # The runtime's contract is that a ProviderError means "the model is
            # gone, hand off". Vertex raises httpx errors, RuntimeError from the
            # circuit breaker, and KeyError from a malformed body; the runtime
            # should not have to know the difference.
            raise ProviderError(f"Vertex call failed: {type(exc).__name__}: {exc}") from exc

        return self._read(data)

    @staticmethod
    def _read(data: dict) -> ModelStep:
        candidates = data.get("candidates") or []
        if not candidates:
            # A prompt-feedback block with no candidate means the request was
            # refused upstream — a safety filter, usually. Not a network
            # failure, but equally not an answer, so it routes to a human.
            reason = ((data.get("promptFeedback") or {}).get("blockReason")) or "no candidate"
            raise ProviderError(f"Vertex returned no candidate: {reason}")

        candidate = candidates[0]
        content = candidate.get("content") or {}
        parts = content.get("parts") or []

        text: list[str] = []
        calls: list[ToolCall] = []
        for i, part in enumerate(parts):
            if "text" in part:
                text.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"] or {}
                args = fc.get("args")
                calls.append(ToolCall(
                    id=f"call_{i}",
                    name=fc.get("name") or "",
                    # Gemini sends parsed arguments, not a JSON string. A
                    # non-dict is the model's mistake and is passed through
                    # empty so the tool's own validation produces the refusal.
                    arguments=args if isinstance(args, dict) else {},
                ))

        usage = data.get("usageMetadata") or {}
        return ModelStep(
            reply="\n".join(t for t in text if t).strip() or None,
            tool_calls=calls,
            tokens_in=int(usage.get("promptTokenCount") or 0),
            tokens_out=int(usage.get("candidatesTokenCount") or 0),
            tokens_thought=int(usage.get("thoughtsTokenCount") or 0),
            finish_reason=candidate.get("finishReason"),
            # Echoed back next turn so the model sees its own function calls.
            # Only the parts — the role is set on the way back in.
            raw={"role": "assistant", "parts": parts} if parts else None,
        )

    def tool_result_message(self, call: ToolCall, content: str) -> dict:
        # `name` matters here in a way it does not for a vendor that keys on a
        # call id: Gemini matches a functionResponse to its functionCall by
        # name, so losing it would leave the model unable to tell which of three
        # parallel lookups came back.
        return {"role": "tool", "name": call.name, "tool_call_id": call.id, "content": content}


# ── the scripted provider the deterministic tests run on ─────────────────────

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
        self.served = 0
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list[list[str]] = []
        self.seen_models: list[str] = []

    async def step(self, *, model: str, messages: list[dict], tools: list[dict], params: dict) -> ModelStep:
        self.seen_messages.append(list(messages))
        self.seen_tools.append([t["function"]["name"] for t in tools])
        self.seen_models.append(model)

        if not self.script:
            # **An exhausted script is a test bug, and it used to look exactly
            # like a production failure.**
            #
            # This returned `ModelStep(reply="", tokens_in=1, tokens_out=1)`,
            # which the runtime reads as an empty completion and records as
            # "the model produced no reply". Across ninety days that accounted
            # for 80 of the 94 runs in that class — nine to one against the real
            # ones — and it made the largest reliability signal in the system a
            # measurement of our own e2e scripts. `tokens_in=1` was the only
            # tell, and nothing was reading it.
            #
            # Raising is the fix, and it is the *smaller* change than it looks:
            # `run_turn` catches everything, so a script that under-provides
            # still ends in the same handoff it did before. What changes is that
            # the run says why — a sentence naming the script rather than one
            # blaming a model that was never called.
            raise AgentError(
                f"the scripted provider ran out after {self.served} step(s) — "
                "the script is short, the model did not fail",
                "I'm just checking on that — one moment.",
            )

        self.served += 1
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
        return {"role": "tool", "name": call.name, "tool_call_id": call.id, "content": content}


_PROVIDERS: dict[str, type] = {
    "vertex_ai": VertexGeminiProvider,
    # The value `agent_configs.provider` carried before the runtime moved to
    # Vertex. Mapped rather than migrated away, so an unconverted row keeps
    # working instead of failing at the first guest message.
    "vertex": VertexGeminiProvider,
}


def for_name(name: str) -> Provider:
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ProviderError(
            f"unknown provider {name!r} — the hotel agents run on Vertex Gemini"
        )
    return cls()
