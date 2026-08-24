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

    # Whether what it writes is a *suggestion for a person* rather than a change
    # to the hotel.
    #
    # The autonomy ladder reads 0 read-only · 1 propose · 2 safe operational
    # actions, and the invariant `tests/test_ai_tools.py` enforces is that a
    # mutating tool sits at floor 2 or above — otherwise level 1 would be able
    # to change things and the rung would mean nothing.
    #
    # The Owner Copilot's `create_revenue_recommendation` broke that rule while
    # being exactly what level 1 is *for*. It writes a row and an owner notices
    # it, so calling it non-mutating would be a lie; but it changes no rate, no
    # allocation, no stay and no money, and putting it at floor 2 would have
    # meant either raising that agent to a level whose name promises operational
    # actions it must never have, or queueing every written suggestion for a
    # human to approve before another human reads it.
    #
    # So the distinction is declared rather than argued. A proposal may run at
    # level 1 and at no lower level; everything else that mutates still needs 2.
    # The exception is a field somebody sets deliberately, which means it is a
    # list a reviewer can read — not a comment explaining why a rule has a hole.
    proposal: bool = False

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

    # What the *channel* has to be able to do for this tool to work at all.
    #
    #   'outbound'   the tool reaches `messaging.send`, so the channel needs a
    #                messaging policy. `website`, `internal`, `phone` and
    #                `email` have none, and calling it there raises before it
    #                reaches anything that could refuse politely.
    #   'templates'  the tool sends an approved template, which Instagram has
    #                no mechanism for.
    #
    # Declared here rather than listed in `registry`, and the list that used to
    # be in `registry` was the reason this exists: it held `send_message` alone
    # while `send_template` and `send_room_photos` reach `messaging.send` too,
    # so both were offered on channels where they raise. A hand-maintained set
    # in a file away from the tools it describes is a set that goes stale the
    # next time somebody adds a tool — which is the §10 lesson about one regex,
    # in a different room. `tests/test_channel_capability.py` derives the same
    # answer from the tool bodies and fails if a declaration is missing.
    requires: tuple[str, ...] = ()

    # Business validation, run *before* the autonomy gate on every path.
    #
    # `revalidate` below asks "is this still true?" and only runs on the approval
    # path. This asks "was this ever allowed?" and runs on both, which is the
    # difference that matters: a request outside the property's rules must be
    # refused, not queued for a human who can only refuse it again five minutes
    # later while a guest is told their discount is being considered.
    #
    # It is also where the rule "approval skips the autonomy gate and nothing
    # else" stops being a convention. Sitting above the gate rather than inside
    # the tool body means `approved=True` cannot route around it — the only line
    # `approved` changes is the verdict, and this has already run by then.
    #
    # Raises `AgentError` to refuse. Absent means the arguments' validity is
    # fully expressed by the pydantic model, which is true of most tools.
    precheck: Callable[..., Awaitable[None]] | None = None

    # What the *model* should do while a human looks at the proposal.
    #
    # The audit trail records why a tool was deferred in the system's own words
    # ("create_booking is configured to require approval at this property").
    # Handing that same sentence to the model produced this, to a guest, on the
    # website of a hotel that had just quoted them a real price:
    #
    #     I'm sorry, I wasn't able to book the room directly.
    #
    # Nothing had gone wrong. The gate had done exactly its job. But the model
    # was told about an internal rule and had nothing to do with the
    # information except apologise for it — so the guest heard the hotel's
    # configuration as a failure, and heard it before being offered the payment
    # link that would have booked them in immediately.
    #
    # A deferral is a fork in the conversation, not a dead end. This is the
    # other road, written for the model rather than for the record.
    when_deferred: str | None = None

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

    # Run *after* a mutating tool succeeds: read the record back and prove it.
    #
    # The third of the trio, and the one that closes the loop.
    #   precheck    was this ever allowed?          before anything
    #   revalidate  is it still true?               approval path only
    #   verify      did it actually happen?         after the write
    #
    # **A tool returning a payload is a tool reporting its own intention.** The
    # service was called and did not raise; that is all `status=executed` has
    # ever meant. Between that and the guest being told "you're checked in, room
    # 214" sits a write that may not have landed — a race with the front desk
    # doing it by hand, a constraint, a service answering 200 having changed
    # nothing. Nothing in the runtime could tell the difference, and the model
    # has every reason to believe the payload it was handed.
    #
    # So a mutating tool whose effect can be observed declares how to observe
    # it. `registry.execute` calls this with the validated arguments, the
    # context and the result id, and it re-reads the record *independently of
    # the write path* — not the row the service returned, which is the claim
    # under test. It returns evidence (a small dict, shown to the model) or
    # raises `MutationUnverified` with what it actually saw.
    #
    # Absent means the effect cannot be observed cheaply — sending a WhatsApp
    # message, where the transport's own receipt is the evidence and a re-read
    # would prove nothing the send did not already.
    verify: Callable[..., Awaitable[dict]] | None = None

    def json_schema(self) -> dict:
        """
        The argument schema, in the shape a provider expects.

        Providers reject `$defs`-heavy schemas inconsistently, so nested models
        are **inlined** rather than referenced. This used to be `pop("$defs")`
        and nothing else, which was correct only for as long as no tool had a
        nested argument — the first one that did shipped
        `items: {"$ref": "#/$defs/…"}` pointing at a definition that had just
        been deleted. Gemini was shown an array of nothing, sent an array of
        strings, and the tool rejected them. Nothing failed loudly; the agent
        simply could not use one of its tools, and only a live run found it.

        So the refs are resolved instead of orphaned, and an unresolvable one
        raises at registration time rather than at a hotelier's screen.
        """
        schema = self.input_model.model_json_schema()
        defs = schema.pop("$defs", {}) or {}
        schema.pop("title", None)

        def inline(node, depth: int = 0):
            if depth > 8:
                # A model that references itself. Not something any tool here
                # needs, and an infinite expansion is worse than a refusal.
                raise RuntimeError(f"{self.name}: argument schema nests too deeply")
            if isinstance(node, list):
                return [inline(v, depth + 1) for v in node]
            if not isinstance(node, dict):
                return node
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                if name not in defs:
                    raise RuntimeError(
                        f"{self.name}: argument schema references {ref} and "
                        f"nothing defines it"
                    )
                merged = {k: v for k, v in node.items() if k != "$ref"}
                return inline({**defs[name], **merged}, depth + 1)
            return {k: inline(v, depth + 1) for k, v in node.items()}

        return inline(schema)


@dataclass
class ToolOutcome:
    """One execution, in the shape `agent_actions` stores."""

    tool: str
    # executed · replayed · awaiting_approval · refused · failed
    #
    # 'replayed' is a success that did not run: this exact call was already made
    # in service of this exact guest message and its recorded result is being
    # returned instead. It counts as `ok` — the effect the model asked for
    # exists — and is kept distinct from 'executed' so that "what did this run
    # do" never reads as "it made a second booking".
    status: str
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

    # Whether the change was read back and confirmed.
    #
    #   True   a `verify` hook re-read the record and it agreed.
    #   False  it re-read the record and it did not — the outcome is `failed`.
    #   None   not applicable: the tool did not mutate, or its effect is not
    #          observable by a re-read. **Not the same as False**, and the
    #          difference is what stops "unverified" being read as "broken" for
    #          the sixty-odd tools that only ever read something.
    verified: bool | None = None

    # sha256 of (message being answered · tool · validated arguments), for the
    # mutating calls made on a guest turn. Null everywhere else. See
    # services/ai/registry.execute.
    idempotency_key: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("executed", "replayed")

    @property
    def is_verified_mutation(self) -> bool:
        """A change that was made *and* proven. What a reply may rely on."""
        return self.ok and self.verified is True


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

    # What the guest's message deterministically required, and what became of
    # it. Carried on the result as well as written to `agent_runs` so the
    # evaluation runner can score a turn without a round trip per turn — a
    # hundred-run sweep would otherwise be a hundred extra queries.
    # Shape: services/ai/required_actions.summarise().
    requirements: dict = field(default_factory=dict)
    # 'none' · 'model' · 'forced' · 'unmet'. The single number this pass moves.
    required_action_status: str = "none"
