"""
The model router, and the Gemini dialect.

Two things worth testing without a network. The router, because a model id in
the wrong tier is an invoice nobody notices for a month. And the schema
conversion, because Vertex takes an OpenAPI subset rather than JSON Schema —
and the failure mode when a declaration is subtly wrong is not an error, it is
a model that produces arguments the tool then rejects, which the guest reads as
the agent being confused.

No Vertex call happens here. The real-model suite is `scripts/vertex_e2e.py`
and is separately triggered; CI must never depend on Vertex being up.
"""
import pytest

from app.config import settings
from app.services.ai import model_router, registry
from app.services.ai.providers import (
    VertexGeminiProvider,
    to_gemini_schema,
    to_gemini_tools,
)

registry.load_all()


# ── the router ───────────────────────────────────────────────────────────────

def test_a_guest_turn_gets_the_workhorse():
    choice = model_router.select(agent_type="guest", task="guest_conversation")
    assert choice.model == settings.hotel_model_conversation
    assert choice.tier == model_router.CONVERSATION
    assert choice.provider == "vertex_ai"


def test_the_default_for_an_unknown_task_is_the_workhorse_not_the_expensive_one():
    # Defaulting the other way would make one forgotten task name expensive on
    # every guest message, and nobody would find out until the invoice.
    choice = model_router.select(task="something_nobody_registered")
    assert choice.model == settings.hotel_model_conversation
    assert "unrecognised" in choice.reason


@pytest.mark.parametrize("task", [
    "complex_negotiation", "cancellation_dispute", "multi_reservation",
    "policy_conflict", "revenue_analysis", "operational_analysis",
])
def test_money_and_ambiguity_reach_the_reasoning_tier(task):
    assert model_router.select(task=task).model == settings.hotel_model_reasoning


@pytest.mark.parametrize("task", [
    "intent_classification", "language_detection", "spam_detection",
    "needs_tool", "simple_extraction", "routing",
])
def test_routing_and_classification_use_the_cheapest_model(task):
    choice = model_router.select(task=task)
    assert choice.model == settings.hotel_model_classify
    # And explicitly do not think. A classifier that reasons costs what the
    # conversation model costs, which is the whole point of not using it.
    assert choice.thinking_budget == 0


def test_escalation_moves_up_a_tier_rather_than_repeating_the_same_call():
    plain = model_router.select(task="guest_conversation")
    escalated = model_router.select(task="guest_conversation", escalated=True)
    assert plain.model == settings.hotel_model_conversation
    assert escalated.model == settings.hotel_model_reasoning
    assert "escalated" in escalated.reason


def test_escalating_a_classification_stays_cheap():
    # Retrying "which language is this" on the expensive model is not a better
    # answer, it is the same answer for more money.
    assert model_router.select(
        task="intent_classification", escalated=True
    ).model == settings.hotel_model_classify


def test_the_owner_copilot_always_reasons():
    assert model_router.select(
        agent_type="owner", task="guest_conversation"
    ).model == settings.hotel_model_reasoning


def test_a_property_specific_model_wins_over_the_tier_default():
    # A hotel given a specific model for a reason keeps it. The tier still
    # decides how hard it thinks and how much it may say.
    choice = model_router.select(task="guest_conversation", override_model="gemini-custom")
    assert choice.model == "gemini-custom"
    assert choice.tier == model_router.CONVERSATION
    assert choice.max_output_tokens == 900


def test_no_model_id_appears_outside_config_and_the_router():
    # The rule the router exists to enforce. A model id in the runtime is one
    # that gets upgraded in four places and missed in a fifth.
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app" / "services" / "ai"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "model_router.py":
            continue
        text = path.read_text()
        if "gemini-" in text or "gpt-" in text:
            offenders.append(path.name)
    assert not offenders, f"model ids outside the router: {offenders}"


def test_every_task_maps_to_a_real_tier():
    for task in model_router.known_tasks():
        assert model_router.tier_of(task) in (
            model_router.CONVERSATION, model_router.REASONING, model_router.CLASSIFY
        )


# ── the Gemini dialect ───────────────────────────────────────────────────────

def test_optional_fields_collapse_to_a_nullable_type():
    # pydantic writes `str | None` as anyOf. Gemini has no anyOf, and passing it
    # through would make the declaration unusable.
    out = to_gemini_schema({
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "description": "a room number",
    })
    assert out["type"] == "string"
    assert out["nullable"] is True
    assert out["description"] == "a room number"


def test_json_schema_keys_gemini_does_not_understand_are_dropped():
    out = to_gemini_schema({
        "type": "string",
        "minLength": 4,
        "maxLength": 120,
        "title": "Title",
        "default": "x",
        "description": "kept",
    })
    assert out == {"type": "string", "description": "kept"}


def test_nested_objects_and_arrays_are_narrowed_all_the_way_down():
    out = to_gemini_schema({
        "type": "object",
        "properties": {
            "values": {"type": "array", "items": {"type": "string", "minLength": 1}},
            "nested": {"type": "object", "properties": {"n": {"type": "integer", "exclusiveMinimum": 0}}},
        },
        "required": ["values"],
    })
    assert out["properties"]["values"]["items"] == {"type": "string"}
    assert out["properties"]["nested"]["properties"]["n"] == {"type": "integer"}
    assert out["required"] == ["values"]


def test_a_tool_with_no_arguments_declares_no_parameters():
    # Gemini rejects an object with an empty properties map, and several tools
    # take no arguments *on purpose* — a guest-scoped read must not accept a
    # subject. So they must declare nothing rather than an empty object.
    declarations = to_gemini_tools([
        {"function": {"name": "get_guest", "description": "who this is",
                      "parameters": {"type": "object", "properties": {}}}}
    ])
    assert declarations[0]["name"] == "get_guest"
    assert "parameters" not in declarations[0]


def test_every_registered_tool_converts_to_a_usable_declaration():
    described = [
        {"function": {"name": name, "description": spec.description,
                      "parameters": spec.json_schema()}}
        for name, spec in registry.REGISTRY.items()
    ]
    declarations = to_gemini_tools(described)
    assert len(declarations) == len(registry.REGISTRY)

    for declaration in declarations:
        assert declaration["name"]
        assert declaration["description"]
        params = declaration.get("parameters")
        if params is None:
            continue
        assert params["type"] == "object"
        assert params["properties"]
        # The keys Vertex rejects outright, at any depth.
        encoded = str(params)
        for forbidden in ("anyOf", "$defs", "$ref", "allOf", "minLength", "exclusiveMinimum"):
            assert forbidden not in encoded, f"{declaration['name']} leaked {forbidden}"


# ── message translation ──────────────────────────────────────────────────────

def test_the_system_instruction_is_lifted_out_of_the_message_list():
    system, contents = VertexGeminiProvider._to_contents([
        {"role": "system", "content": "you are the hotel"},
        {"role": "user", "content": "hello"},
    ])
    assert system == "you are the hotel"
    assert contents == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_assistant_turns_become_model_turns():
    _, contents = VertexGeminiProvider._to_contents([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello there"},
    ])
    assert contents[1] == {"role": "model", "parts": [{"text": "hello there"}]}


def test_a_function_call_turn_is_echoed_back_verbatim():
    # The model has to see its own call to make sense of the result. Rebuilding
    # it from text would lose the arguments.
    parts = [{"functionCall": {"name": "get_property", "args": {}}}]
    _, contents = VertexGeminiProvider._to_contents([
        {"role": "user", "content": "where are you"},
        {"role": "assistant", "parts": parts},
    ])
    assert contents[1] == {"role": "model", "parts": parts}


def test_tool_results_ride_on_a_user_turn_as_function_responses():
    _, contents = VertexGeminiProvider._to_contents([
        {"role": "user", "content": "where are you"},
        {"role": "tool", "name": "get_property", "content": '{"city": "Anjuna"}'},
    ])
    assert contents[1]["role"] == "user"
    response = contents[1]["parts"][0]["functionResponse"]
    assert response["name"] == "get_property"
    assert response["response"] == {"city": "Anjuna"}


def test_a_refusal_result_is_still_a_dict_gemini_will_accept():
    # `registry.execute` hands back a refusal as JSON. Gemini requires the
    # response to be an object, so a bare string must be wrapped rather than
    # sent as-is and rejected.
    _, contents = VertexGeminiProvider._to_contents([
        {"role": "user", "content": "x"},
        {"role": "tool", "name": "get_reservation", "content": "not json at all"},
    ])
    assert contents[1]["parts"][0]["functionResponse"]["response"] == {"result": "not json at all"}


# ── reading a candidate ──────────────────────────────────────────────────────

def test_a_text_answer_is_read_with_its_token_counts():
    step = VertexGeminiProvider._read({
        "candidates": [{"content": {"parts": [{"text": "Breakfast is 8 to 10:30."}]},
                        "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 812, "candidatesTokenCount": 19,
                          "thoughtsTokenCount": 40},
    })
    assert step.reply == "Breakfast is 8 to 10:30."
    assert not step.wants_tools
    assert (step.tokens_in, step.tokens_out, step.tokens_thought) == (812, 19, 40)
    assert step.finish_reason == "STOP"


def test_a_tool_call_survives_a_candidate_with_no_text():
    # The bug this exists to prevent: reading `parts[0].text` would raise or
    # return nothing on a turn whose only part is a functionCall, silently
    # dropping the tool call.
    step = VertexGeminiProvider._read({
        "candidates": [{"content": {"parts": [
            {"functionCall": {"name": "create_task",
                              "args": {"title": "AC not cooling", "category": "maintenance"}}}
        ]}}],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 12},
    })
    assert step.wants_tools
    assert step.tool_calls[0].name == "create_task"
    assert step.tool_calls[0].arguments["category"] == "maintenance"
    assert step.reply is None


def test_several_tool_calls_in_one_turn_all_survive():
    step = VertexGeminiProvider._read({
        "candidates": [{"content": {"parts": [
            {"functionCall": {"name": "search_availability", "args": {"units": 1}}},
            {"functionCall": {"name": "list_services", "args": {}}},
        ]}}],
    })
    assert [c.name for c in step.tool_calls] == ["search_availability", "list_services"]
    assert [c.id for c in step.tool_calls] == ["call_0", "call_1"]


def test_text_alongside_a_tool_call_is_kept():
    step = VertexGeminiProvider._read({
        "candidates": [{"content": {"parts": [
            {"text": "Let me check."},
            {"functionCall": {"name": "search_availability", "args": {}}},
        ]}}],
    })
    assert step.reply == "Let me check."
    assert step.wants_tools


def test_malformed_arguments_become_empty_rather_than_crashing():
    # The model's mistake, not an outage. Passed through empty so the tool's own
    # validation produces the refusal and the model is told what was wrong.
    step = VertexGeminiProvider._read({
        "candidates": [{"content": {"parts": [
            {"functionCall": {"name": "create_task", "args": "not-a-dict"}}
        ]}}],
    })
    assert step.tool_calls[0].arguments == {}


def test_a_blocked_prompt_is_a_provider_error_not_an_empty_answer():
    from app.services.ai.errors import ProviderError

    # No candidate means the request was refused upstream. Returning an empty
    # reply would have the runtime send a guest nothing at all; a ProviderError
    # routes it to a human, which is the honest outcome.
    with pytest.raises(ProviderError) as caught:
        VertexGeminiProvider._read({"promptFeedback": {"blockReason": "SAFETY"}})
    assert "SAFETY" in str(caught.value)
