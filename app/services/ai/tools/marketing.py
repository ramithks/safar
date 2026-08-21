"""
Marketing — the tools, and what the model is deliberately not given.

**`messaging.send()` is not reachable from here.** Every other agent has
`send_message`, whose argument is a sentence and whose recipient is the
conversation it is already in. A marketing agent with that tool plus a guest
list would be able to message anybody, one call at a time, with no consent check
and no cap — the whole of this phase routed around by a tool that already
existed. So the Marketing agent is granted neither `send_message` nor
`send_template`, and its only route to a guest is `execute_campaign`, which runs
the gates.

The shape that falls out of that:

    find_eligible_guests    a *segment*, never an id. The server queries.
    estimate_campaign_audience  what would actually be reached, and who was not
    propose_campaign_message the copy, checked against the campaign's offer
    create_campaign         a draft, with the offer as structured data
    schedule_campaign       ask a human
    cancel_campaign         stop one, including one that is running
    execute_campaign        the only send, and it always needs approval

Two properties are worth stating because between them they are why an AI is
allowed to write to a hotel's guest list at all:

**The model never names a recipient.** Not in any tool, not in any argument.
There is no field on any of these that takes a guest id, a phone number or a
handle. "Send this to Mrs Sharma" is not a thing the tool surface can express,
which means no phrasing of it can succeed.

**The model never states a commercial fact.** The offer is structured data on
the campaign row; `claims.check` refuses a body that asserts a percentage, an
amount or a date the offer does not carry. A model that writes "40% off" for a
10% campaign is refused by the server with the reason, and can then write it
correctly — which is a better outcome than a filter that silently rewrites it.
"""
import logging
from datetime import date

from pydantic import BaseModel, Field

from app.services.ai.context import AgentContext
from app.services.ai.errors import OutOfScope, ToolUnavailable
from app.services.ai.ids import ResourceId
from app.services.ai.registry import tool
from app.services.marketing import audience as audience_module
from app.services.marketing import campaigns as campaign_module
from app.services.marketing import claims

logger = logging.getLogger(__name__)

# The four segments, spelled out for the model in every description that takes
# one. Kept in one place so the tool descriptions cannot drift from what
# `audience.SEGMENTS` actually runs.
_SEGMENTS = ", ".join(audience_module.SEGMENTS)


class AudienceArgs(BaseModel):
    segment: str = Field(description=f"One of: {_SEGMENTS}.")
    channel: str = Field(default="whatsapp", description="whatsapp or instagram.")
    lookback_days: int = Field(
        default=60, ge=1, le=365,
        description="How far back the segment looks. Ignored by repeat_guest.",
    )


class CampaignArgs(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    campaign_type: str = Field(description=f"One of: {_SEGMENTS}.")
    channel: str = Field(default="whatsapp", description="whatsapp or instagram.")
    body: str | None = Field(
        default=None, max_length=1000,
        description=(
            "The message, if it is going inside an open window. Every figure in "
            "it must come from the offer — a percentage, price or date the offer "
            "does not carry is refused."
        ),
    )
    template_purpose: str | None = Field(
        default=None,
        description=(
            "For WhatsApp outside the window: the approved template's purpose, "
            "e.g. reengagement. Instagram has no templates."
        ),
    )
    discount_pct: int | None = Field(
        default=None, ge=0, le=100,
        description=(
            "The discount this campaign is authorised to advertise. This is what "
            "makes the figure sayable — it is not a discount you are granting."
        ),
    )
    valid_until: date | None = Field(
        default=None, description="The offer's expiry, if it has one."
    )
    inclusions: list[str] = Field(
        default_factory=list, max_length=6,
        description="What the offer includes, from list_services or a rate plan.",
    )
    lookback_days: int = Field(default=60, ge=1, le=365)


class CampaignRef(BaseModel):
    campaign_id: ResourceId = Field(description="From create_campaign.")


class ScheduleArgs(BaseModel):
    campaign_id: ResourceId
    note: str | None = Field(
        default=None, max_length=300,
        description="One line for the manager deciding. Why this, why now.",
    )


class CancelArgs(BaseModel):
    campaign_id: ResourceId
    reason: str = Field(max_length=200)


class MessageArgs(BaseModel):
    campaign_id: ResourceId
    body: str = Field(min_length=10, max_length=1000)


def _require_channel(channel: str) -> str:
    if channel not in ("whatsapp", "instagram"):
        raise OutOfScope(
            f"{channel!r} is not a channel this hotel markets on",
            "Let me get someone from the team to set that up.",
        )
    return channel


async def _load(ctx: AgentContext, campaign_id: str) -> dict:
    """
    A campaign of this property's, or a refusal.

    The property comes from the context, never from an argument, so a campaign
    id belonging to another hotel resolves to nothing rather than to their
    campaign. Same rule as every other id-taking tool in this codebase.
    """
    campaign = await campaign_module.get(campaign_id, ctx.property_id)
    if campaign is None:
        raise OutOfScope(
            f"campaign {campaign_id} does not belong to this property",
            "I can't find that campaign.",
        )
    return campaign


# ── who could be reached ────────────────────────────────────────────────────

@tool(
    name="find_eligible_guests",
    description=(
        "How many guests a segment would actually reach, and who it would not. "
        f"Segments: {_SEGMENTS}. Returns counts only — you never see names or "
        "numbers, and you cannot choose recipients. Consent, the frequency cap "
        "and channel eligibility are already applied."
    ),
    input_model=AudienceArgs,
    permission="marketing",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def find_eligible_guests(args: AudienceArgs, ctx: AgentContext) -> dict:
    if args.segment not in audience_module.SEGMENTS:
        raise OutOfScope(
            f"{args.segment!r} is not a segment; use one of {_SEGMENTS}",
            "Let me check with the team on that.",
        )
    _require_channel(args.channel)

    # Counts, never rows. The model has no use for a guest list — it cannot
    # choose recipients and cannot write to one — and handing it two hundred
    # names and phone numbers would be putting a hotel's guest list into a
    # provider's prompt for no capability at all.
    return await audience_module.estimate(
        property_id=ctx.property_id,
        segment=args.segment,
        channel=args.channel,
        audience_config={"lookback_days": args.lookback_days},
    )


@tool(
    name="estimate_campaign_audience",
    description=(
        "What a campaign you have already created would reach right now, with "
        "the reasons people are excluded. Numbers move between creating a "
        "campaign and running it — call this before telling anyone a figure."
    ),
    input_model=CampaignRef,
    permission="marketing",
    autonomy_floor=0,
    mutating=False,
    usage_type="ai.tool_read",
)
async def estimate_campaign_audience(args: CampaignRef, ctx: AgentContext) -> dict:
    campaign = await _load(ctx, args.campaign_id)
    estimate = await audience_module.estimate(
        property_id=ctx.property_id,
        segment=campaign["campaign_type"],
        channel=campaign["channel"],
        audience_config=campaign.get("audience"),
    )
    return {
        "campaignId": campaign["id"],
        "name": campaign["name"],
        "status": campaign["status"],
        **estimate,
    }


# ── writing it ──────────────────────────────────────────────────────────────

@tool(
    name="create_campaign",
    description=(
        "Record a campaign as a draft. The offer — a discount, an expiry, what "
        "is included — is stored as data, and the message you write is checked "
        "against it. Nothing is sent: a campaign has to be approved and then "
        "executed. Say it is a draft, never that it has gone out."
    ),
    input_model=CampaignArgs,
    permission="marketing",
    # Level 2: writing a draft down is a safe operational act. It reaches no
    # guest, spends no credit and can be cancelled by anyone who can read it.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="campaign",
    creates_result=True,
)
async def create_campaign(args: CampaignArgs, ctx: AgentContext) -> tuple[dict, str]:
    _require_channel(args.channel)

    offer: dict = {}
    if args.discount_pct is not None:
        offer["discount_pct"] = args.discount_pct
    if args.valid_until is not None:
        offer["valid_until"] = args.valid_until.isoformat()
    if args.inclusions:
        offer["inclusions"] = [str(i)[:60] for i in args.inclusions]

    try:
        campaign = await campaign_module.create(
            property_id=ctx.property_id,
            partner_id=ctx.partner_id,
            name=args.name,
            campaign_type=args.campaign_type,
            channel=args.channel,
            body=args.body,
            template_purpose=args.template_purpose,
            audience_config={"lookback_days": args.lookback_days},
            offer=offer,
            created_by="agent",
        )
    except campaign_module.CampaignRefused as exc:
        raise OutOfScope(
            f"{exc.code}: {exc.detail}",
            "I need to get the wording right on that — one moment.",
        ) from exc

    estimate = await audience_module.estimate(
        property_id=ctx.property_id, segment=args.campaign_type,
        channel=args.channel, audience_config={"lookback_days": args.lookback_days},
    )

    return (
        {
            "campaignId": campaign["id"],
            "name": campaign["name"],
            "status": campaign["status"],
            "channel": campaign["channel"],
            "offer": offer,
            "eligible": estimate["eligible"],
            "selected": estimate["selected"],
            "excludedBy": estimate["excludedBy"],
            "instruction": (
                "This is a draft. Nobody has been messaged. Quote the eligible "
                "figure and not the selected one, and say it needs approval "
                "before it can go out."
            ),
        },
        campaign["id"],
    )


@tool(
    name="propose_campaign_message",
    description=(
        "Set or replace a campaign's message. The server checks every figure in "
        "it against the campaign's offer and refuses any it did not authorise — "
        "so write the offer's own numbers, or no numbers at all."
    ),
    input_model=MessageArgs,
    permission="marketing",
    autonomy_floor=2,
    mutating=True,
    # The copy is what `ai.content_generation` was written for: an item, priced
    # at 15, distinct from the 8-credit turn that asked for it.
    usage_type="ai.content_generation",
    result_type="campaign",
)
async def propose_campaign_message(args: MessageArgs, ctx: AgentContext) -> tuple[dict, str]:
    campaign = await _load(ctx, args.campaign_id)
    if campaign["status"] not in ("draft", "proposed"):
        raise OutOfScope(
            f"campaign {campaign['id']} is {campaign['status']}; its message "
            "cannot be rewritten once it has been approved",
            "That campaign has already gone to a manager — I'd need a new one.",
        )

    verdict = claims.check(args.body, campaign.get("offer"))
    if not verdict.ok:
        # Returned as a refusal the model can act on, with the numbers it is
        # actually allowed to use. A filter that silently rewrote the copy would
        # teach the model nothing and would put words in the hotel's mouth.
        raise OutOfScope(
            "MESSAGE_CLAIMS_REFUSED: " + "; ".join(verdict.problems)
            + " — " + claims.describe_offer(campaign.get("offer")),
            "Let me reword that.",
        )

    updated = await campaign_module.set_status(
        campaign["id"], "proposed", body=args.body,
        version=campaign["version"] + 1,
    )
    return (
        {
            "campaignId": campaign["id"],
            "status": (updated or {}).get("status"),
            "version": (updated or {}).get("version"),
            "claims": verdict.claims,
            "instruction": (
                "The wording is recorded and still needs approval. Do not tell "
                "anyone it has been sent."
            ),
        },
        campaign["id"],
    )


@tool(
    name="schedule_campaign",
    description=(
        "Put a campaign in front of a manager to approve. This does not send it "
        "and does not schedule a send — a person decides. Tell whoever asked "
        "that it is waiting for approval."
    ),
    input_model=ScheduleArgs,
    permission="marketing",
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="campaign",
)
async def schedule_campaign(args: ScheduleArgs, ctx: AgentContext) -> tuple[dict, str]:
    campaign = await _load(ctx, args.campaign_id)
    if campaign["status"] not in ("draft", "proposed"):
        raise OutOfScope(
            f"campaign {campaign['id']} is already {campaign['status']}",
            "That one has already moved on.",
        )
    if not campaign.get("body") and not campaign.get("template_purpose"):
        raise OutOfScope(
            "the campaign has no message to send",
            "I need to write the message first.",
        )

    estimate = await audience_module.estimate(
        property_id=ctx.property_id, segment=campaign["campaign_type"],
        channel=campaign["channel"], audience_config=campaign.get("audience"),
    )
    await campaign_module.set_status(campaign["id"], "awaiting_approval",
                                     outcome_reason=args.note)
    return (
        {
            "campaignId": campaign["id"],
            "status": "awaiting_approval",
            "eligible": estimate["eligible"],
            "instruction": (
                f"Waiting for a manager. It would reach {estimate['eligible']} "
                "guests if approved now — that number is recomputed at send "
                "time, so quote it as an estimate."
            ),
        },
        campaign["id"],
    )


@tool(
    name="cancel_campaign",
    description=(
        "Stop a campaign, including one that is part-way through sending. Safe "
        "to call — it only ever prevents messages."
    ),
    input_model=CancelArgs,
    permission="marketing",
    # Level 2, and lower than executing one, because stopping is the safe
    # direction. The same reasoning as `release_hold` giving stock back.
    autonomy_floor=2,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="campaign",
)
async def cancel_campaign(args: CancelArgs, ctx: AgentContext) -> tuple[dict, str]:
    await _load(ctx, args.campaign_id)
    try:
        await campaign_module.cancel(args.campaign_id, ctx.property_id, args.reason)
    except campaign_module.CampaignRefused as exc:
        raise OutOfScope(f"{exc.code}: {exc.detail}", "That campaign is already finished.")

    stats = await campaign_module.stats(args.campaign_id)
    return (
        {
            "campaignId": args.campaign_id, "status": "cancelled",
            "alreadySent": stats["sent"],
            "instruction": (
                f"Cancelled. {stats['sent']} messages had already gone out and "
                "cannot be recalled — say so rather than implying none did."
            ),
        },
        args.campaign_id,
    )


# ── sending it ──────────────────────────────────────────────────────────────

async def _precheck_execute(args: "CampaignRef", ctx: AgentContext) -> None:
    """
    Is this campaign in a state where sending is even a question?

    Runs before the autonomy gate and on the approval path too, so a cancelled
    campaign cannot be approved into sending and a campaign whose copy stopped
    matching its offer cannot be approved into saying it.
    """
    campaign = await campaign_module.get(args.campaign_id, ctx.property_id)
    if campaign is None:
        raise OutOfScope(
            f"CAMPAIGN_NOT_FOUND: campaign {args.campaign_id} is not this property's",
            "I can't find that campaign.",
        )
    if campaign["status"] in ("cancelled", "completed", "failed"):
        raise OutOfScope(
            f"CAMPAIGN_NOT_RUNNABLE: this campaign is {campaign['status']}",
            "That campaign is finished.",
        )
    if not campaign.get("body") and not campaign.get("template_purpose"):
        raise OutOfScope(
            "CAMPAIGN_NOT_RUNNABLE: the campaign has no message",
            "There's no message on that campaign yet.",
        )

    if campaign.get("body"):
        verdict = claims.check(campaign["body"], campaign.get("offer"))
        if not verdict.ok:
            raise OutOfScope(
                "MESSAGE_CLAIMS_REFUSED: " + "; ".join(verdict.problems),
                "The wording on that campaign needs fixing before it goes out.",
            )


async def _revalidate_execute(args: "CampaignRef", ctx: AgentContext) -> None:
    """
    Is there still anybody to send to, at the moment the manager approves?

    The audience was estimated when the campaign was proposed. Between then and
    the approval, guests opt out and other campaigns consume the frequency cap.
    An approval that executes into an empty audience is not harmful, but it is a
    manager being told something happened when nothing did — so it is refused
    with the reason, and the recovery is a fresh campaign.

    Consent is *not* the thing being checked here. That happens per recipient in
    the send loop, seconds before each message, which is the only check that can
    catch somebody who opts out while the campaign is running.
    """
    campaign = await campaign_module.get(args.campaign_id, ctx.property_id)
    if campaign is None:
        raise OutOfScope("CAMPAIGN_NOT_FOUND: the campaign no longer exists",
                         "That campaign is gone.")
    if campaign["status"] == "cancelled":
        raise OutOfScope(
            "CAMPAIGN_CANCELLED: somebody cancelled this campaign after it was proposed",
            "That campaign was cancelled.",
        )

    built = await audience_module.build(
        property_id=ctx.property_id, segment=campaign["campaign_type"],
        channel=campaign["channel"], audience_config=campaign.get("audience"),
    )
    if not built.eligible:
        why = ", ".join(f"{e['count']} {e['reason']}" for e in built.breakdown())
        raise OutOfScope(
            f"AUDIENCE_EMPTY: nobody is eligible now — {why or 'the segment matched nobody'}",
            "Nobody is eligible for that campaign at the moment.",
        )


@tool(
    name="execute_campaign",
    description=(
        "Send an approved campaign. This is the only way a message reaches a "
        "guest, and it always needs a manager first. Each recipient's consent "
        "and message limit are checked again at the moment their message is "
        "sent, so the number that goes out may be lower than the estimate."
    ),
    input_model=CampaignRef,
    permission="marketing",
    # Level 3: it is the transactional act, and the most consequential one any
    # agent in this system can take — a mistake reaches hundreds of people at
    # once and cannot be recalled.
    autonomy_floor=3,
    mutating=True,
    usage_type="ai.tool_write",
    result_type="campaign",
    # Not switchable off by configuration, at any autonomy level. A hotel that
    # raises its marketing agent to 4 gets faster drafting, not unattended mass
    # outbound — which is the one capability nobody should acquire by turning a
    # dial they were told was about speed.
    always_requires_approval=True,
    when_deferred=(
        "Nothing was sent and no guest has been messaged. A manager sends "
        "campaigns, always. Say it is with them for approval and stop there — "
        "do not offer to send it another way, and do not treat this as a "
        "failure to report."
    ),
    precheck=_precheck_execute,
    revalidate=_revalidate_execute,
)
async def execute_campaign(args: CampaignRef, ctx: AgentContext) -> tuple[dict, str]:
    campaign = await _load(ctx, args.campaign_id)

    # Approved here rather than by the caller: reaching this line means the
    # approval path ran, because `always_requires_approval` admits no other
    # route. The row records it so the campaign screen agrees with the action.
    if campaign["status"] not in campaign_module.RUNNABLE:
        await campaign_module.set_status(campaign["id"], "approved")

    try:
        prepared = await campaign_module.prepare(campaign["id"], ctx.property_id)
    except campaign_module.CampaignRefused as exc:
        if exc.code == "INSUFFICIENT_CREDITS":
            await campaign_module.set_status(
                campaign["id"], "failed", outcome_reason=exc.detail
            )
        raise OutOfScope(f"{exc.code}: {exc.detail}",
                         "I couldn't start that campaign.") from exc

    # Handed to Celery. A hundred messages inside an HTTP request is a request
    # that times out half-sent, and "half-sent" is the one campaign state with
    # no honest way to report it.
    queued = await _dispatch(campaign["id"], ctx.property_id)

    return (
        {
            "campaignId": campaign["id"],
            "status": "running" if queued else "sent_inline",
            "eligible": prepared["eligible"],
            "creditsReserved": prepared["creditsReserved"],
            "excludedBy": prepared["excludedBy"],
            "queued": queued,
            "instruction": (
                f"The campaign is running and will reach up to "
                f"{prepared['eligible']} guests. Say 'up to' — anyone who opts "
                "out before their message is sent will not receive it, and the "
                "final count comes from the campaign, not from you."
            ),
        },
        campaign["id"],
    )


async def _dispatch(campaign_id: str, property_id: str) -> bool:
    """
    Hand the run to a worker, or do it here. Returns whether a broker took it.

    The same shape as `messaging.pipeline.dispatch`, and for the same reasons:
    the worker is the production path because sending is slow and must survive a
    request ending, and inline is a *setting* rather than an inference so
    nothing can accidentally block a production HTTP call on four hundred
    messages.

    The two paths run identical code — `campaigns.execute` either way. The
    campaign is already prepared and its recipients already recorded, so an
    inline run is the same work in a different process rather than a second
    implementation of it.
    """
    from app.config import settings

    if settings.messaging_inline_processing:
        await campaign_module.execute(campaign_id, property_id)
        return False

    try:
        from app.workers.marketing_tasks import run_campaign_task

        run_campaign_task.delay(campaign_id, property_id)
        return True
    except Exception:  # noqa: BLE001
        # No broker. Running it here is better than a campaign that was
        # approved, reserved credit and then silently never sent — and the
        # recipients are durable, so a later worker would resume it anyway.
        logger.warning("no broker for campaign %s; running inline", campaign_id,
                       exc_info=True)
        await campaign_module.execute(campaign_id, property_id)
        return False
