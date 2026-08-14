"""
What a tool is, declared.

A tool is not a Python function the model happens to be told about. It is a
declaration the runtime can reason over before anything executes:

    who may call it        permission
    when it may execute    autonomy_floor, requires_approval
    what it does           mutating, usage_type, result_type
    what it accepts        a pydantic model, which is also the model's schema

The declaration is the enforcement point. `registry.execute()` reads these
fields and decides; the function body never checks permissions, so a tool
author cannot forget to.
"""
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import BaseModel


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str

    # The model's argument schema, and the runtime's validator. One definition,
    # so what the model is shown and what the tool accepts cannot drift.
    input_model: type[BaseModel]

    # A scope from `agent_permissions`, using the same vocabulary as a human's
    # role. None means the tool needs no scope beyond existing — reading the
    # hotel's own published facts, for instance.
    permission: str | None

    # The lowest autonomy at which this may execute rather than be proposed.
    #   0 read · 1 propose · 2 safe operational · 3 transactional · 4 autonomous
    autonomy_floor: int

    # Does it change anything a human would notice? Drives the audit trail and,
    # from slice 7, the meter.
    mutating: bool

    # The metering dimension. Priced by `usage_rate_card` when that arrives;
    # declared now so no tool ships without one and has to be retro-fitted.
    usage_type: str

    fn: Callable[..., Awaitable[Any]]

    # What the action produced, for `agent_actions.result_type`.
    result_type: str = "none"

    # Whether this tool *brought the thing into existence*, as opposed to
    # reading or amending one that already existed. Only a creating tool writes
    # provenance back onto its result: a ticket should record the action that
    # raised it, not the last action that touched it, and `assign_task` returns
    # the same result_type as `create_task` without being its author.
    creates_result: bool = False

    # A tool that should be seen before it lands even at sufficient autonomy.
    # `agent_tools.requires_approval` can turn this on per property; it cannot
    # turn it off.
    always_requires_approval: bool = False

    # Guest-facing tools take their subject from the conversation. Set when the
    # tool reads a specific record by id and that id must be checked against the
    # conversation before anything is returned.
    scoped_to_conversation: bool = False

    # Checked again just before an *approved* proposal executes.
    #
    # A proposal and its approval are separated by however long a manager takes
    # to look at their phone. In that time a room can sell, a quote can expire
    # and a rate can move. Running the tool on the strength of the approval
    # alone would execute a decision somebody made about a hotel that no longer
    # exists — so a mutating tool that can go stale declares how to tell.
    #
    # Raises `AgentError` to refuse. Absent means the tool cannot go stale,
    # which is true of raising a ticket and false of almost anything priced.
    revalidate: Callable[..., Awaitable[None]] | None = None

    def json_schema(self) -> dict:
        """The argument schema, in the shape a provider expects."""
        schema = self.input_model.model_json_schema()
        # Providers reject `$defs`-heavy schemas inconsistently and none of our
        # tools need nesting, so this is flattened at the edge rather than
        # tolerated.
        schema.pop("$defs", None)
        schema.pop("title", None)
        return schema


@dataclass
class ToolOutcome:
    """One execution, in the shape `agent_actions` stores."""

    tool: str
    status: str                      # executed · awaiting_approval · refused · failed
    input: dict = field(default_factory=dict)
    output: Any = None
    result_type: str = "none"
    result_id: str | None = None
    refusal_reason: str | None = None
    # What the model is told. For a success this is the output; for a refusal
    # it is the reason, because an agent that knows why it was stopped can say
    # something useful instead of apologising.
    model_message: str = ""
    # What may be said to a guest, when the runtime has to answer without the
    # model — a provider outage mid-turn.
    guest_message: str | None = None
    latency_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "executed"


@dataclass
class RunResult:
    """One turn."""

    run_id: str
    status: str                      # succeeded · failed · escalated · refused
    reply: str | None
    actions: list[ToolOutcome] = field(default_factory=list)
    handoff: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    error: str | None = None
