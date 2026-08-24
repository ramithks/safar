"""
What the agent does when the model, or the write, does not cooperate.

Three behaviours, none of which existed before this pass, and each of which was
invisible in production when it was wrong:

    retry policy      a global-only model has one quota pool, so rotating
                      "global to global" is the same call again.
    degradation       a model that will not answer is swapped mid-turn rather
                      than handing the guest to a person.
    verification      a mutating tool that returns success and did not happen
                      must not reach the guest as success.

Vertex is never called. The transport is driven with a stub client so the
*policy* is under test rather than Google's availability — the live probes are
`app/services/model_probe.py` and are triggered separately.
"""
import asyncio
import json

import pytest

from app.services import model_catalog, vertex
from app.services.ai import registry
from app.services.ai.errors import MutationUnverified, ProviderError
from app.services.ai.providers import EchoProvider, ModelStep
from app.services.ai.schemas import ToolOutcome


def run(coro):
    return asyncio.run(coro)


# ── a stub Vertex ────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None,
                 headers: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {"candidates": []}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)
        self.request = None

    def json(self):
        return self._payload


class FakeClient:
    """Answers a scripted sequence and records every URL it was given."""

    def __init__(self, script: list[FakeResponse]):
        self.script = list(script)
        self.urls: list[str] = []

    async def post(self, url, **kwargs):
        self.urls.append(url)
        if not self.script:
            return FakeResponse(200, {"candidates": [{"content": {"parts": []}}]})
        return self.script.pop(0)

    @property
    def locations(self) -> list[str]:
        return [u.split("/locations/")[1].split("/")[0] for u in self.urls]


@pytest.fixture
def transport(monkeypatch):
    """Vertex with no network, no auth, no sleeping, and no circuit breaker."""
    monkeypatch.setattr(vertex, "_get_access_token", lambda: ("tok", "proj"))
    monkeypatch.setattr(vertex.asyncio, "sleep", _no_sleep)
    vertex._region_cooldown.clear()

    def install(script):
        client = FakeClient(script)
        monkeypatch.setattr(vertex, "_get_http_client", lambda: client)
        return client

    return install


async def _no_sleep(_seconds):
    return None


_OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
       "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}


# ── retry policy ─────────────────────────────────────────────────────────────

def test_a_global_only_model_does_not_pretend_to_rotate(transport):
    # The behaviour this pass exists to fix. `gemini-3.6-flash` is served from
    # `global` alone; the old loop would have re-issued the identical request to
    # the identical pool with 0.2-0.8s of jitter, burning the retry budget to
    # make the same call four times.
    client = transport([FakeResponse(429), FakeResponse(429), FakeResponse(200, _OK)])
    run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash", timeout=5))

    assert client.locations == ["global", "global", "global"]
    # And every hostname is the bare one — no `global-` prefix anywhere.
    assert all(u.startswith("https://aiplatform.googleapis.com/") for u in client.urls)


def test_a_model_with_real_regions_still_rotates(transport):
    # gemini-3.5-flash is the one 3.x model where rotation means something, and
    # the existing cross-region strategy must keep working for it.
    client = transport([FakeResponse(429), FakeResponse(429), FakeResponse(200, _OK)])
    run(vertex._post_generate({"contents": []}, model="gemini-3.5-flash", timeout=5))

    assert client.locations[0] == "asia-south1"     # home first
    assert len(set(client.locations[:3])) == 3      # three different locations


def test_the_backoff_grows_when_there_is_nowhere_to_go(transport, monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(vertex.asyncio, "sleep", record)
    transport([FakeResponse(429), FakeResponse(429), FakeResponse(429),
               FakeResponse(200, _OK)])
    run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash", timeout=5))

    assert len(slept) == 3
    # Exponential, not the near-instant jitter a fresh region would earn.
    assert slept[0] >= 5.0
    assert slept[1] > slept[0]


def test_a_retry_after_header_is_honoured(transport, monkeypatch):
    slept: list[float] = []

    async def record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(vertex.asyncio, "sleep", record)
    transport([FakeResponse(429, headers={"Retry-After": "12"}),
               FakeResponse(200, _OK)])
    run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash", timeout=5))

    assert 12.0 <= slept[0] <= 12.0 + 12.0 * 0.3 + 0.01


# ── 404 is not access ────────────────────────────────────────────────────────

def test_a_404_moves_on_rather_than_failing_the_turn(transport):
    # An uncatalogued model, or a location list one entry out of date, should
    # self-correct instead of taking a hotel's turn down.
    client = transport([FakeResponse(404), FakeResponse(200, _OK)])
    run(vertex._post_generate({"contents": []}, model="gemini-3.5-flash", timeout=5))

    assert client.locations[:2] == ["asia-south1", "asia-southeast1"]


def test_a_location_that_404d_is_never_tried_again(transport):
    client = transport([FakeResponse(404), FakeResponse(404),
                        FakeResponse(429), FakeResponse(200, _OK)])
    run(vertex._post_generate({"contents": []}, model="gemini-3.5-flash", timeout=5))

    tried = client.locations
    assert tried.count("asia-south1") == 1
    assert tried.count("asia-southeast1") == 1


def test_404_everywhere_says_it_is_not_an_access_problem(transport):
    transport([FakeResponse(404)] * 8)
    with pytest.raises(vertex.VertexModelNotFound) as caught:
        run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash", timeout=5))

    message = str(caught.value)
    assert "not an access problem" in message
    # And it names where it looked, which is the fact that identifies the cause.
    assert "global" in message


def test_a_403_is_raised_immediately_and_not_retried(transport):
    client = transport([FakeResponse(403, {"error": {"message": "denied"}})])
    with pytest.raises(vertex.VertexAccessDenied):
        run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash", timeout=5))
    assert len(client.urls) == 1


def test_a_500_is_classified_as_upstream(transport):
    transport([FakeResponse(500, {"error": {"message": "boom"}})])
    with pytest.raises(vertex.VertexUpstreamError):
        run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash", timeout=5))


def test_the_trace_records_what_actually_happened(transport):
    transport([FakeResponse(429), FakeResponse(200, _OK)])
    trace = vertex.CallTrace()
    run(vertex._post_generate({"contents": []}, model="gemini-3.6-flash",
                              timeout=5, trace=trace))

    assert trace.model == "gemini-3.6-flash"
    assert trace.location == "global"
    assert trace.retries == 1
    assert trace.attempts == 2


# ── the degradation ladder ───────────────────────────────────────────────────

class FailingProvider:
    """Fails for a named set of models and answers for everything else."""

    name = "vertex_ai"

    def __init__(self, failing: set[str]):
        self.failing = failing
        self.models: list[str] = []

    async def step(self, *, model, messages, tools, params) -> ModelStep:
        self.models.append(model)
        if model in self.failing:
            raise ProviderError(f"{model} is unreachable")
        return ModelStep(reply="answered", tokens_in=5, tokens_out=2)

    def tool_result_message(self, call, content):
        return {"role": "tool", "name": call.name, "content": content}


def test_a_dead_brain_degrades_to_the_workhorse_not_to_a_human():
    from app.services.ai import model_router

    choice = model_router.select(task="guest_conversation")
    provider = FailingProvider({choice.model})

    # The ladder the runtime walks. Asserted here directly because the runtime's
    # own path needs a database; `test_ai_runtime.py` covers that end.
    assert choice.fallbacks[0] == "gemini-3.5-flash"
    assert choice.fallbacks[-1] == "gemini-2.5-flash"

    async def walk():
        for model in [choice.model, *choice.fallbacks]:
            try:
                return await provider.step(model=model, messages=[], tools=[],
                                           params={}), model
            except ProviderError:
                continue
        raise AssertionError("nothing answered")

    step, served = run(walk())
    assert step.reply == "answered"
    assert served == "gemini-3.5-flash"
    assert provider.models == [choice.model, "gemini-3.5-flash"]


def test_the_ladder_ends_on_the_model_already_in_production():
    from app.services.ai import model_router

    choice = model_router.select(task="guest_conversation")
    provider = FailingProvider({choice.model, "gemini-3.5-flash"})

    async def walk():
        for model in [choice.model, *choice.fallbacks]:
            try:
                return await provider.step(model=model, messages=[], tools=[],
                                           params={}), model
            except ProviderError:
                continue
        raise AssertionError("nothing answered")

    _, served = run(walk())
    assert served == "gemini-2.5-flash"


def test_no_ladder_ever_reaches_the_quota_starved_model():
    from app.services.ai import model_router

    for tier in model_router.tiers():
        choice = model_router.select(task="guest_conversation")
        model = model_router.model_for_tier(tier)
        assert "gemini-3.7-flash" not in model_catalog.fallback_chain(model)
    del choice


# ── mutation verification ────────────────────────────────────────────────────

def test_every_high_risk_mutation_declares_a_verify():
    # The list from the reliability brief: booking, check-in/out, room state,
    # money on a bill. A tool here without a `verify` is one that can report a
    # change it did not make.
    registry.load_all()
    must_verify = {
        "create_booking", "check_in", "check_out", "set_room_status",
        "post_folio_charge",
    }
    missing = {
        name for name in must_verify
        if registry.REGISTRY[name].verify is None
    }
    assert not missing, f"high-risk mutations with no verification: {missing}"


def test_a_read_only_tool_is_not_marked_unverified():
    # `verified=None` is "not applicable" and must not read as "broken" for the
    # sixty-odd tools that only ever read something.
    outcome = ToolOutcome(tool="get_property", status="executed", output={"a": 1})
    assert outcome.verified is None
    assert outcome.ok is True
    assert outcome.is_verified_mutation is False


def test_a_verified_mutation_is_the_only_thing_a_reply_may_rely_on():
    verified = ToolOutcome(tool="check_in", status="executed", verified=True)
    unproven = ToolOutcome(tool="check_in", status="executed", verified=False)
    assert verified.is_verified_mutation is True
    assert unproven.is_verified_mutation is False


def test_a_failed_verification_is_failed_not_refused():
    # Nothing was denied. The world is in a state nobody chose, and the two must
    # not be collapsed — a refusal is a thing the agent can talk its way around,
    # and this is not.
    exc = MutationUnverified("the reservation still reads 'confirmed'")
    assert exc.action_status == "failed"
    # And the model is told not to claim success, in words it can act on.
    assert "did NOT complete" in exc.model_message
    assert "request_human_handoff" in exc.model_message


def test_the_model_is_never_handed_the_raw_reason_as_a_guest_message():
    exc = MutationUnverified("reservation abc-123 still reads 'confirmed'")
    assert "abc-123" not in exc.guest_message


# ── duplicate tool calls ─────────────────────────────────────────────────────

def test_identical_arguments_produce_one_fingerprint():
    # Key ordering must not defeat the check: a model that emits the same call
    # with its keys in a different order is repeating itself.
    a = ("post_folio_charge", json.dumps({"amount_paise": 500, "reservation_id": "r1"},
                                         sort_keys=True, default=str))
    b = ("post_folio_charge", json.dumps({"reservation_id": "r1", "amount_paise": 500},
                                         sort_keys=True, default=str))
    assert a == b


def test_different_arguments_are_not_deduplicated():
    a = ("post_folio_charge", json.dumps({"amount_paise": 500}, sort_keys=True))
    b = ("post_folio_charge", json.dumps({"amount_paise": 600}, sort_keys=True))
    assert a != b


# ── verification, through the one execution path ─────────────────────────────
#
# The unit assertions above are about the classes. These are about the gate:
# `registry.execute` is the only way a tool runs, so it is the only place the
# proof can be made mandatory.

class _Args(__import__("pydantic").BaseModel):
    value: int = 1


def _register(name: str, verify):
    """A throwaway tool, registered and removed around one test."""
    async def fn(args, ctx):
        return ({"claimed": True}, "result-1")

    registry.REGISTRY.pop(name, None)
    registry.tool(
        name=name, description="a probe", input_model=_Args, permission=None,
        autonomy_floor=0, mutating=True, usage_type="ai.tool_write",
        result_type="task", verify=verify,
    )(fn)
    return name


@pytest.fixture
def ctx():
    from tests.test_ai_runtime import PROPERTY, PARTNER
    from app.services.ai.context import AgentContext, AgentPrincipal

    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Probe", autonomy_level=4,
        scopes=frozenset({"tasks"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="c", config_version=1, model="", conversation_id=None,
        reservation_id=None, guest_name=None, channel="whatsapp", actor="guest",
        tool_grants={n: {"enabled": True} for n in list(registry.REGISTRY) + ["probe_ok",
                                                                             "probe_bad",
                                                                             "probe_broken"]},
    )


def test_a_mutation_that_cannot_be_proven_is_not_reported_as_success(ctx):
    async def verify(args, c, result_id):
        raise MutationUnverified("the row still reads 'confirmed'")

    name = _register("probe_bad", verify)
    try:
        outcome = run(registry.execute(name, {"value": 1}, ctx))
    finally:
        registry.REGISTRY.pop(name, None)

    # The tool returned a success payload. The re-read disagreed, so the outcome
    # is a failure — this is the whole point of the hook.
    assert outcome.status == "failed"
    assert outcome.ok is False
    assert outcome.verified is False
    assert "did NOT complete" in outcome.model_message


def test_a_proven_mutation_carries_its_evidence_to_the_model(ctx):
    async def verify(args, c, result_id):
        return {"status": "checked_in", "roomNumber": "204"}

    name = _register("probe_ok", verify)
    try:
        outcome = run(registry.execute(name, {"value": 1}, ctx))
    finally:
        registry.REGISTRY.pop(name, None)

    assert outcome.status == "executed"
    assert outcome.verified is True
    assert outcome.is_verified_mutation is True
    # Shown to the model so it answers from the record rather than the request.
    assert outcome.output["verified"] == {"status": "checked_in", "roomNumber": "204"}


def test_a_broken_check_does_not_invent_a_failed_mutation(ctx):
    # A read replica hiccup is not proof the write failed. Treating it as such
    # would hand off every turn the moment the check itself has a bad minute.
    async def verify(args, c, result_id):
        raise RuntimeError("PostgREST is down")

    name = _register("probe_broken", verify)
    try:
        outcome = run(registry.execute(name, {"value": 1}, ctx))
    finally:
        registry.REGISTRY.pop(name, None)

    assert outcome.status == "executed"
    # Absent, not false: nothing was disproven.
    assert outcome.verified is None
    assert outcome.is_verified_mutation is False
