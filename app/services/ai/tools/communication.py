"""
Communication — stopping, and speaking.

`request_human_handoff` matters more than it looks. An agent that cannot stop is an agent that will
keep going past the end of what it knows, and the interesting failures of AI
support are all downstream of that — the invented policy, the confident wrong
price, the guest who asked four times for a person and got four more paragraphs.

Handoff is a state change on the conversation, so it is real: `handler` moves to
'human_requested', and the runtime refuses to start another autonomous turn on
that conversation until a member explicitly hands it back. The brief's rule —
**human takeover stops autonomous replies** — is that column, not a convention.

`send_message` goes through `services/messaging`, never near a provider. The
agent does not know that WhatsApp exists, has no idea what a phone number id is,
and cannot reach Meta even if it decided to: the tool's only argument is what to
say. Everything else — which channel, which number, whether the 24-hour window
permits free text at all — is the messaging service's, and it answers with a
structured code the agent can act on rather than a sentence it has to parse.

That is what makes the window rule enforceable. `MESSAGE_WINDOW_CLOSED` comes
back as a refusal with an instruction attached, so the agent's next move is to
send an approved template or hand off — and no phrasing in a guest's message can
change the answer, because the model was never asked.
"""
from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.registry import tool
from app.services.messaging import service as messaging
from app.services.messaging.channels import (
    TEMPLATE_NOT_APPROVED,
    TEMPLATE_NOT_FOUND,
    WINDOW_CLOSED,
    MessagingError,
)

REASONS = (
    "guest_asked", "no_knowledge", "low_confidence", "complaint",
    "refund", "policy_conflict", "unusual_request", "autonomy_limit",
)


class HandoffArgs(BaseModel):
    reason: str = Field(description=f"One of: {', '.join(REASONS)}.")
    note: str | None = Field(
        default=None, max_length=300,
        description="One line for the staff member picking this up. No guest personal data.",
    )


@tool(
    name="request_human_handoff",
    description=(
        "Hand this conversation to a person. Use it when the guest asks for "
        "one, when you do not know something, when someone is upset, when money "
        "is in dispute, or when you have been refused an action. Stopping is "
        "always better than guessing."
    ),
    input_model=HandoffArgs,
    permission="messaging",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="handoff",
)
async def request_human_handoff(args: HandoffArgs, ctx: AgentContext) -> tuple[dict, str | None]:
    reason = args.reason if args.reason in REASONS else "unusual_request"

    if not ctx.conversation_id:
        # A run with no conversation — a scheduled job, a test harness. There is
        # no thread to hand over, and saying so is better than pretending.
        return {
            "handedOff": False,
            "reason": reason,
            "instruction": "There is no live conversation to hand over.",
        }, None

    # 'human_requested', not 'human'. Nobody has picked it up yet, and the gap
    # between the AI stopping and a person arriving is a real state that lasts
    # minutes — the inbox has to be able to show it, and a receptionist has to
    # be able to sort by it.
    row = await messaging.request_human(ctx.conversation_id, ctx.property_id, reason)
    if row is None:
        raise ToolUnavailable(
            "the conversation could not be handed over",
            "Let me get someone from the hotel to you.",
        )

    return (
        {
            "handedOff": True,
            "reason": reason,
            "note": args.note,
            "instruction": (
                "Tell the guest a member of the hotel team is taking over, "
                "briefly and without apologising at length. Then stop — do not "
                "answer the underlying question yourself."
            ),
        },
        ctx.conversation_id,
    )


class SendMessageArgs(BaseModel):
    body: str = Field(
        min_length=1, max_length=1400,
        description=(
            "What to say to the guest, in their language. Two or three sentences. "
            "Only facts a tool gave you."
        ),
    )


class SendTemplateArgs(BaseModel):
    purpose: str = Field(
        description=(
            "Why you are messaging: booking_confirmation, payment_confirmation, "
            "checkin_link, pre_arrival, follow_up, cancellation, human_handoff, "
            "reengagement."
        ),
    )
    values: dict[str, str] = Field(
        default_factory=dict,
        description="Values for the template's own parameters, by name.",
    )


@tool(
    name="send_message",
    description=(
        "Reply to the guest. This is the only way to say anything to them — "
        "your own text is not delivered unless it goes through here. May be "
        "refused when the messaging window has closed, in which case send an "
        "approved template with send_template or hand off."
    ),
    input_model=SendMessageArgs,
    permission="messaging",
    # Level 2: it reaches a guest and it costs money, so it sits with the other
    # operational actions rather than with the free reads.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="message",
    creates_result=True,
)
async def send_message(args: SendMessageArgs, ctx: AgentContext) -> tuple[dict, str]:
    conversation = await _conversation(ctx)

    try:
        sent = await messaging.send(
            conversation,
            body=args.body,
            author="agent",
            author_name=ctx.principal.display_name,
            agent_id=ctx.principal.agent_id,
            run_id=ctx.run_id,
        )
    except MessagingError as exc:
        if exc.code == WINDOW_CLOSED:
            # A refusal the agent can act on. The structured code is what makes
            # the next move deterministic — the model is not being asked to
            # work out whether the window is open, it is being told it is not.
            raise OutOfScope(
                f"{exc.code}: {exc.detail}",
                "I'll pass this to the hotel team so they can reach you.",
            ) from exc
        raise ToolUnavailable(
            f"{exc.code}: {exc.detail}",
            "I couldn't get that message through — the hotel team will follow up.",
        ) from exc

    # The usage event is written by the messaging service, not here — see the
    # note in `service.send()`. One choke point, one meter.

    return (
        {
            "sent": True,
            "messageId": sent.message["id"],
            "duplicate": sent.duplicate,
            "instruction": (
                "The guest has been sent that message. Do not send it again and "
                "do not repeat it in your reply."
            ),
        },
        sent.message["id"],
    )


@tool(
    name="send_template",
    description=(
        "Send one of the hotel's approved templates. Use this only when "
        "send_message has been refused because the messaging window has closed. "
        "You choose the purpose, not the wording."
    ),
    input_model=SendTemplateArgs,
    permission="messaging",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="message",
    creates_result=True,
)
async def send_template(args: SendTemplateArgs, ctx: AgentContext) -> tuple[dict, str]:
    """
    The escape hatch when the window has closed.

    The agent picks a *purpose*; the hotel's approved template for that purpose
    is looked up in `message_templates`. It cannot compose a template, choose a
    provider name or send one that Meta has not approved — those are the three
    ways a template send goes wrong, and none of them is reachable from here.
    """
    conversation = await _conversation(ctx)

    try:
        sent = await messaging.send(
            conversation,
            purpose=args.purpose,
            template_values=args.values,
            author="agent",
            author_name=ctx.principal.display_name,
            agent_id=ctx.principal.agent_id,
            run_id=ctx.run_id,
        )
    except MessagingError as exc:
        if exc.code in (TEMPLATE_NOT_FOUND, TEMPLATE_NOT_APPROVED):
            raise OutOfScope(
                f"{exc.code}: {exc.detail}",
                "I'll let the hotel team know so they can get in touch.",
            ) from exc
        raise ToolUnavailable(
            f"{exc.code}: {exc.detail}",
            "I couldn't get that message through — the hotel team will follow up.",
        ) from exc

    return (
        {
            "sent": True,
            "messageId": sent.message["id"],
            "template": sent.template_name,
            "instruction": (
                "An approved template was sent. You cannot send free text on "
                "this conversation until the guest writes again."
            ),
        },
        sent.message["id"],
    )


async def _conversation(ctx: AgentContext) -> dict:
    if not ctx.conversation_id:
        raise ToolUnavailable(
            "this run has no conversation to reply on",
            "Let me get someone from the hotel to you.",
        )

    def _read():
        rows = (
            get_db().table("conversations").select("*")
            .eq("id", ctx.conversation_id)
            .eq("property_id", ctx.property_id)
            .limit(1).execute().data or []
        )
        return rows[0] if rows else None

    conversation = await run_db(_read)
    if not conversation:
        raise ToolUnavailable(
            f"conversation {ctx.conversation_id} not found in this property",
            "Let me get someone from the hotel to you.",
        )
    return conversation
