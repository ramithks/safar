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
import logging

from pydantic import BaseModel, Field

from app.db.client import get_db, run_db
from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.messaging import service as messaging
from app.services.messaging.channels import (
    TEMPLATE_NOT_APPROVED,
    TEMPLATE_NOT_FOUND,
    WINDOW_CLOSED,
    MessagingError,
)

logger = logging.getLogger(__name__)

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


# ── "photos hain?" ───────────────────────────────────────────────────────────


class RoomPhotosArgs(BaseModel):
    room_type_id: ResourceId | None = Field(
        default=None,
        description=(
            "The room to show, from get_room_types or get_stay_options. Leave "
            "empty to send one photo of each room the hotel has."
        ),
    )
    caption: str | None = Field(
        default=None, max_length=300,
        description="One short line to send with them, in the guest's language.",
    )


# How many pictures a guest gets at once.
#
# Four is a room, not a slideshow. Each one is a separate provider call and a
# separate row, and a guest whose phone fills with eleven photographs of a
# bathroom has been sent a problem rather than an answer.
MAX_PHOTOS = 4


def _photo_sources(property_id: str, room_type_id: str | None) -> list[dict]:
    rows = (
        get_db().table("room_types")
        .select("id,name,photos,sort_order")
        .eq("property_id", property_id).eq("is_active", True)
        .order("sort_order").execute().data or []
    )
    if room_type_id:
        rows = [r for r in rows if str(r["id"]) == str(room_type_id)]
    return [r for r in rows if r.get("photos")]


@tool(
    name="send_room_photos",
    description=(
        "Send the guest actual photographs of a room, as pictures in the chat. "
        "Use this whenever they ask to see a room — 'photos?', 'photos hain?', "
        "'can I see it' — and after describing a room they are choosing between. "
        "Do not paste image links into your reply; this sends them properly. If "
        "it reports the hotel has no photos, say so plainly and offer to ask the "
        "team, and never claim you are unable to show photos."
    ),
    input_model=RoomPhotosArgs,
    permission="messaging",
    # Level 2: sending a guest a picture the hotel uploaded of its own room is
    # not a decision anybody needs to approve. It moves no money and takes no
    # inventory, and the URLs are read from the database rather than chosen by
    # the model — see below.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="none",
    scoped_to_conversation=True,
)
async def send_room_photos(args: RoomPhotosArgs, ctx: AgentContext) -> dict:
    """
    **The model names a room; it never names a URL.**

    That is the whole safety property. The photographs are read from
    `room_types.photos` for this property, so there is no argument through
    which a model — or a guest talking to one — can make the hotel send an
    arbitrary image. The guardrail that stops invented links in prose has no
    equivalent for attachments, and this is why it does not need one.

    Answering "the hotel has not put photos up yet" is a real answer and the
    important one: for months every room in production had an empty list, and
    the agent's reply was "main aapko rooms ki photos nahi dikha sakta" — which
    reads as the product being unable rather than the hotel being unfinished.
    """
    if not ctx.conversation_id:
        raise ToolUnavailable("send_room_photos needs a conversation and this run has none")

    rooms = await run_db(_photo_sources, ctx.property_id, args.room_type_id)
    if not rooms:
        return {
            "sent": 0,
            "instruction": (
                "This hotel has not uploaded photos of that room yet. Tell the "
                "guest plainly that there are none up yet and offer to ask the "
                "team for some. Do NOT say you are unable to show photos — that "
                "is not true and it reads as a limitation of the hotel."
            ),
        }

    # One room: up to four of it. Several rooms: the first photo of each, which
    # is a menu rather than a gallery and is what "show me the rooms" means.
    if args.room_type_id:
        picks = [(rooms[0]["name"], url) for url in rooms[0]["photos"][:MAX_PHOTOS]]
    else:
        picks = [(r["name"], r["photos"][0]) for r in rooms[:MAX_PHOTOS]]

    def _conversation():
        rows = (
            get_db().table("conversations").select("*")
            .eq("id", ctx.conversation_id).limit(1).execute().data or []
        )
        return rows[0] if rows else None

    conversation = await run_db(_conversation)
    if not conversation:
        raise ToolUnavailable("this conversation could not be read")

    sent = 0
    failures: list[str] = []
    for index, (room_name, url) in enumerate(picks):
        try:
            await messaging.send(
                conversation,
                image_url=url,
                # The caption rides on the first picture only. WhatsApp shows it
                # under the image; Instagram drops it and the agent's own reply
                # carries the words instead.
                body=(args.caption or room_name) if index == 0 else None,
                author="agent",
                author_name="Agent",
                # Keyed on the run and the picture, so a retried turn cannot
                # send a guest the same photograph twice.
                client_reference=f"photo:{ctx.run_id}:{index}",
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{room_name}: {type(exc).__name__}")
            # Keep going. Three photographs and one failure is a better answer
            # than nothing, and the guest can see what arrived.
            logger.warning("could not send room photo %s", url, exc_info=True)

    if sent == 0:
        raise ToolUnavailable(
            f"no photo could be sent ({', '.join(failures) or 'unknown'})",
            "I couldn't send the photos just now — let me get someone to help.",
        )

    return {
        "sent": sent,
        "rooms": sorted({name for name, _ in picks}),
        "instruction": (
            "The photos are now in the chat. Say one short line about them — do "
            "not describe each picture, and do not paste any links."
        ),
    }
