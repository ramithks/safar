"""
Communication.

One tool in this slice: `request_human_handoff`.

It matters more than it looks. An agent that cannot stop is an agent that will
keep going past the end of what it knows, and the interesting failures of AI
support are all downstream of that — the invented policy, the confident wrong
price, the guest who asked four times for a person and got four more paragraphs.

Handoff is a state change on the conversation, so it is real: `handler` moves to
'human', and the runtime refuses to start another autonomous turn on that
conversation until a member explicitly hands it back. The brief's rule —
**human takeover stops autonomous replies** — is that column, not a convention.

`send_message` is deliberately *not* registered yet. Nothing can send a WhatsApp
message until the messaging service exists in slice 2, and a registered tool
that always fails teaches the model to route around it. The runtime returns its
reply to the caller, which is the transport's job to deliver.
"""
from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import ToolUnavailable
from app.services.ai.registry import tool

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

    def _write():
        db = get_db()
        updated = (
            db.table("conversations")
            .update({
                "handler": "human",
                "intent": {
                    "complaint": "complaint",
                    "refund": "support",
                    "policy_conflict": "support",
                }.get(reason),
            })
            .eq("id", ctx.conversation_id)
            .eq("property_id", ctx.property_id)
            .execute()
        )
        return updated.data[0] if updated.data else None

    row = await run_db(_write)
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
