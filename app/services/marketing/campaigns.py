"""
Campaigns — the state machine, and the loop that revalidates every recipient.

    draft ──► proposed ──► awaiting_approval ──► approved ──► running ──► completed
                                    │                 │           │
                                    └──► cancelled ◄──┘           └──► failed

**The send loop is the whole file.** Everything above it is bookkeeping; the
part that matters is that eligibility is decided *per recipient, at the moment
before their message leaves*, and never from the audience that was estimated
when a human pressed Approve.

    for each recipient:
        reload the campaign      ── has somebody cancelled it since?
        reload consent           ── have they opted out since?
        recount frequency        ── has another campaign written to them since?
        resolve the conversation ── can we reach them on this channel at all?
        messaging.send()         ── which owns windows, templates and metering
        record the outcome       ── sent · skipped · failed · blocked, with a reason

A campaign to four hundred people takes minutes. A guest who replies STOP in
minute two must not receive minute three's message, and the only way that is
true is if minute three asks again. Snapshotting an audience and trusting it
would be faster, simpler, and would send messages to people who had already
asked us not to — which is the failure this entire phase exists to prevent.

**Idempotency is the database's, not the loop's.** Each recipient row carries
`campaign:guest:channel:version` and the unique index refuses the second write.
A worker that crashes and retries re-enters this loop, finds every completed
recipient already terminal, and skips them. Nothing depends on the worker
remembering where it was.

**Credit is reserved before the first message and settled after the last.**
`reserve_credits` has existed since migration 13 with no caller; this is it.
Discovering an empty wallet at recipient 230 is not a recoverable state — the
first 229 have been sent and cannot be unsent — so the wallet is asked up front
for the whole campaign, and the unused remainder is released at the end.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db.client import get_db, run_db
from app.services import billing
from app.services.marketing import audience as audience_module
from app.services.marketing import claims, consent as consent_module
from app.services.messaging import policy as channel_policy
from app.services.messaging import service as messaging
from app.services.messaging.channels import MessagingError

logger = logging.getLogger(__name__)

# What one recipient costs the hotel, in credits. `marketing.campaign_send` is
# priced at 1 on the rate card; read rather than hardcoded would be better and
# is a query per campaign, so this is the estimate and the ledger remains the
# authority on what was actually charged.
CREDITS_PER_RECIPIENT = 1

# Statuses from which a campaign may still be executed.
RUNNABLE = ("approved", "scheduled")
# Statuses that mean somebody has stopped it.
STOPPED = ("cancelled", "completed", "failed")


class CampaignRefused(Exception):
    """A campaign that may not proceed, with a code the caller reports."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class RunResult:
    campaign_id: str
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    status: str = "completed"
    detail: str | None = None

    def count(self, status: str, reason: str | None = None) -> None:
        setattr(self, status, getattr(self, status) + 1)
        if reason:
            self.reasons[reason] = self.reasons.get(reason, 0) + 1


# ── reading and writing the row ─────────────────────────────────────────────

def _read(campaign_id: str, property_id: str | None = None) -> dict | None:
    q = get_db().table("marketing_campaigns").select("*").eq("id", campaign_id)
    if property_id:
        q = q.eq("property_id", property_id)
    rows = q.limit(1).execute().data or []
    return rows[0] if rows else None


async def get(campaign_id: str, property_id: str | None = None) -> dict | None:
    return await run_db(_read, campaign_id, property_id)


def _patch(campaign_id: str, patch: dict) -> dict | None:
    rows = (
        get_db().table("marketing_campaigns")
        .update({**patch, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", campaign_id).execute().data
    )
    return rows[0] if rows else None


def _insert(row: dict) -> dict:
    return get_db().table("marketing_campaigns").insert(row).execute().data[0]


async def create(
    *,
    property_id: str,
    partner_id: str,
    name: str,
    campaign_type: str,
    channel: str,
    body: str | None = None,
    template_purpose: str | None = None,
    language: str = "en",
    audience_config: dict | None = None,
    offer: dict | None = None,
    created_by: str = "agent",
    created_by_member: str | None = None,
) -> dict:
    """
    Record a campaign in `draft`.

    The copy is checked against the offer here rather than at send time, so the
    refusal reaches whoever wrote it while they are still writing. It is checked
    *again* in `execute`, because the offer can be edited afterwards.
    """
    if campaign_type not in audience_module.SEGMENTS:
        raise CampaignRefused(
            "UNKNOWN_CAMPAIGN_TYPE",
            f"{campaign_type!r} is not a campaign type this system runs; "
            f"choose one of {', '.join(audience_module.SEGMENTS)}",
        )
    channel_policy.for_channel(channel)

    if body:
        verdict = claims.check(body, offer)
        if not verdict.ok:
            raise CampaignRefused("MESSAGE_CLAIMS_REFUSED", "; ".join(verdict.problems))

    return await run_db(_insert, {
        "property_id": property_id,
        "partner_id": partner_id,
        "name": name[:200],
        "campaign_type": campaign_type,
        "channel": channel,
        "status": "draft",
        "audience": audience_config or {},
        "offer": offer or {},
        "body": body,
        "template_purpose": template_purpose,
        "language": language,
        "created_by": created_by,
        "created_by_member": created_by_member,
    })


async def set_status(
    campaign_id: str, status: str, *, property_id: str | None = None, **extra
) -> dict | None:
    patch = {"status": status, **extra}
    if status == "cancelled":
        patch["cancelled_at"] = datetime.now(timezone.utc).isoformat()
    elif status == "completed":
        patch["completed_at"] = datetime.now(timezone.utc).isoformat()
    elif status == "running":
        patch.setdefault("started_at", datetime.now(timezone.utc).isoformat())
    return await run_db(_patch, campaign_id, patch)


async def cancel(campaign_id: str, property_id: str, reason: str | None = None) -> dict | None:
    """
    Stop a campaign, including one that is already running.

    The running worker notices on its next recipient — it reloads the campaign
    every iteration for exactly this — so cancellation is effective within one
    message rather than at the end of the batch.
    """
    campaign = await get(campaign_id, property_id)
    if campaign is None:
        raise CampaignRefused("CAMPAIGN_NOT_FOUND", "no such campaign at this property")
    if campaign["status"] in ("completed", "cancelled"):
        raise CampaignRefused(
            "ALREADY_DECIDED", f"this campaign is already {campaign['status']}"
        )
    return await set_status(campaign_id, "cancelled", outcome_reason=reason)


# ── the recipients ──────────────────────────────────────────────────────────

def recipient_key(campaign: dict, guest_id: str) -> str:
    """
    `campaign:guest:channel:version`.

    Deterministic and derived only from things that are already fixed. Never a
    uuid4 and never the clock: the whole point is that a retry computes the same
    string, and anything time-based computes a different one and sends twice.

    The version is in it so that editing a campaign's copy and running it again
    is a genuinely new send rather than a silent no-op against the old key.
    """
    return f"campaign:{campaign['id']}:{guest_id}:{campaign['channel']}:v{campaign['version']}"


def _upsert_recipients(campaign: dict, guests: list[dict]) -> int:
    """
    Materialise the audience.

    `ignore_duplicates` against the (campaign, guest) index, so re-running this
    after a crash adds the newly-eligible and leaves the already-recorded alone.
    """
    if not guests:
        return 0
    db = get_db()
    rows = [
        {
            "campaign_id": campaign["id"],
            "property_id": campaign["property_id"],
            "guest_id": g["id"],
            "status": "pending",
            "idempotency_key": recipient_key(campaign, g["id"]),
        }
        for g in guests
    ]
    db.table("campaign_recipients").upsert(
        rows, on_conflict="campaign_id,guest_id", ignore_duplicates=True
    ).execute()
    return len(rows)


def _pending(campaign_id: str, limit: int = 1000) -> list[dict]:
    return (
        get_db().table("campaign_recipients").select("*")
        .eq("campaign_id", campaign_id).eq("status", "pending")
        .order("created_at").limit(limit).execute().data or []
    )


def _settle_recipient(recipient_id: str, patch: dict) -> None:
    get_db().table("campaign_recipients").update({
        **patch, "attempted_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", recipient_id).execute()


def _guest(property_id: str, guest_id: str) -> dict | None:
    rows = (
        get_db().table("guests").select("id,name,phone,email")
        .eq("property_id", property_id).eq("id", guest_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


# ── running it ──────────────────────────────────────────────────────────────

async def prepare(campaign_id: str, property_id: str) -> dict:
    """
    Materialise the audience and reserve the credit, before anything is sent.

    Called on the approval path, synchronously, because both of its failures are
    ones a human must be told about immediately: an audience of zero, and a
    wallet that cannot pay. Neither is worth discovering in a worker log.
    """
    campaign = await get(campaign_id, property_id)
    if campaign is None:
        raise CampaignRefused("CAMPAIGN_NOT_FOUND", "no such campaign at this property")
    if campaign["status"] not in RUNNABLE:
        raise CampaignRefused(
            "CAMPAIGN_NOT_RUNNABLE",
            f"a campaign must be approved before it runs; this one is "
            f"{campaign['status']}",
        )

    # The copy, against the offer as it stands *now*. A campaign approved on
    # Tuesday whose offer was edited on Wednesday must not send Tuesday's claim.
    verdict = claims.check(campaign.get("body"), campaign.get("offer"))
    if not verdict.ok and not campaign.get("template_purpose"):
        raise CampaignRefused("MESSAGE_CLAIMS_REFUSED", "; ".join(verdict.problems))

    built = await audience_module.build(
        property_id=property_id,
        segment=campaign["campaign_type"],
        channel=campaign["channel"],
        audience_config=campaign.get("audience"),
    )
    if not built.eligible:
        why = ", ".join(f"{e['count']} {e['reason']}" for e in built.breakdown())
        raise CampaignRefused(
            "AUDIENCE_EMPTY",
            f"nobody is eligible: {built.selected} matched the segment and all "
            f"were excluded ({why or 'the segment matched nobody'})",
        )

    await run_db(_upsert_recipients, campaign, built.eligible)

    # The credit gate. Reserved for the whole campaign, not charged — the charge
    # happens per recipient as they are actually sent, and the reservation is
    # what stops the run beginning at all if the hotel cannot afford it.
    account = await billing.account_for_property(property_id)
    hold_id = None
    if account:
        needed = built.eligible_count * CREDITS_PER_RECIPIENT
        reservation = await billing.reserve(
            billing_account_id=account,
            usage_type="marketing.campaign_send",
            credits=max(needed, 1),
            # Keyed on the campaign and its version, so re-preparing the same
            # campaign reuses the hold rather than stacking a second one on top
            # of a wallet that then looks emptier than it is.
            idempotency_key=f"campaign:{campaign['id']}:v{campaign['version']}",
            property_id=property_id,
            reference_type="marketing_campaign",
            reference_id=campaign["id"],
        )
        if not reservation.get("granted") and not reservation.get("duplicate"):
            raise CampaignRefused(
                "INSUFFICIENT_CREDITS",
                f"this campaign needs {needed} credits and "
                f"{reservation.get('available', 0)} are available. "
                "Top up, or narrow the audience.",
            )
        hold_id = reservation.get("hold_id")
        if hold_id:
            await run_db(_patch, campaign["id"], {"credit_hold_id": hold_id})

    return {
        "campaignId": campaign["id"],
        "eligible": built.eligible_count,
        "selected": built.selected,
        "excludedBy": built.breakdown(),
        "creditHoldId": hold_id,
        "creditsReserved": built.eligible_count * CREDITS_PER_RECIPIENT,
    }


async def execute(campaign_id: str, property_id: str) -> RunResult:
    """
    Send to every pending recipient, revalidating each one.

    Safe to call twice: recipients that already reached a terminal status are
    not in `_pending`, and the ones that are re-derive their own key. Safe to
    interrupt: a crash leaves the remainder pending and the next call resumes.
    """
    campaign = await get(campaign_id, property_id)
    if campaign is None:
        raise CampaignRefused("CAMPAIGN_NOT_FOUND", "no such campaign at this property")
    if campaign["status"] in STOPPED:
        raise CampaignRefused(
            "CAMPAIGN_NOT_RUNNABLE", f"this campaign is {campaign['status']}"
        )
    if campaign["status"] not in RUNNABLE + ("running",):
        raise CampaignRefused(
            "CAMPAIGN_NOT_RUNNABLE",
            f"a campaign must be approved before it runs; this one is "
            f"{campaign['status']}",
        )

    await set_status(campaign_id, "running")
    result = RunResult(campaign_id=campaign_id)

    frequency = await consent_module.frequency_for_property(property_id)
    pending = await run_db(_pending, campaign_id)

    for recipient in pending:
        # Reloaded every iteration. This is what makes cancelling a running
        # campaign take effect within one message instead of at the end.
        current = await get(campaign_id, property_id)
        if current is None or current["status"] == "cancelled":
            await run_db(_settle_recipient, recipient["id"], {
                "status": "skipped", "reason": "CAMPAIGN_CANCELLED",
            })
            result.count("skipped", "CAMPAIGN_CANCELLED")
            continue

        outcome = await _send_one(current, recipient, frequency)
        result.count(outcome[0], outcome[1])

    final = await get(campaign_id, property_id)
    still_pending = await run_db(_pending, campaign_id, 1)
    if final and final["status"] == "cancelled":
        result.status = "cancelled"
    elif not still_pending:
        await set_status(campaign_id, "completed")
        result.status = "completed"
    else:
        result.status = "running"

    await _settle_credit(campaign_id, property_id, result)
    return result


async def _send_one(
    campaign: dict, recipient: dict, frequency: consent_module.FrequencyPolicy
) -> tuple[str, str | None]:
    """
    One recipient, revalidated and sent. Never raises.

    A single failure must not end the campaign — that is the difference between
    "one guest's number is wrong" and "three hundred people did not get the
    message". Every exit writes a terminal status and a reason, so the campaign
    can always account for everybody.
    """
    property_id = campaign["property_id"]
    guest_id = recipient["guest_id"]

    async def settle(status: str, reason: str | None = None, **extra) -> tuple[str, str | None]:
        await run_db(_settle_recipient, recipient["id"],
                     {"status": status, "reason": reason, **extra})
        return status, reason

    # ── consent, again ───────────────────────────────────────────────────────
    #
    # The single most important line in this file. The audience said this guest
    # was eligible; that was minutes ago, and they may have replied STOP since.
    record = await consent_module.for_guest(property_id, guest_id)
    if record is None or not record.may_market:
        return await settle("skipped", "MARKETING_OPT_OUT")

    # ── frequency, again ─────────────────────────────────────────────────────
    #
    # Another campaign may have run in between. Counted from the same table this
    # loop writes to, so two campaigns racing cannot both decide they are the
    # first message this week.
    counts = await consent_module.recent_send_counts(property_id, [guest_id])
    sent_7d, sent_30d = counts.get(guest_id, (0, 0))
    if frequency.exceeded_by(sent_7d, sent_30d):
        return await settle("skipped", "FREQUENCY_CAP")

    guest = await run_db(_guest, property_id, guest_id)
    if not guest:
        return await settle("skipped", "NO_CONTACT")

    conversation = await messaging.conversation_for_guest(
        property_id, campaign["channel"], guest
    )
    if conversation is None:
        # On Instagram this is the normal case for somebody who has never
        # written: Meta gives no way to open a thread from a handle.
        return await settle("skipped", "NO_CONTACT")

    # ── the send ─────────────────────────────────────────────────────────────
    #
    # Through `messaging.send()` and nothing else. It owns the connection, the
    # window rule, template resolution, the outbound idempotency on
    # `client_reference`, and the per-message meter. Everything this loop knows
    # about channels is which one the campaign named.
    try:
        sent = await messaging.send(
            conversation,
            body=campaign.get("body") if not campaign.get("template_purpose") else None,
            purpose=campaign.get("template_purpose"),
            template_values=_template_values(campaign, guest),
            language=campaign.get("language") or "en",
            author="agent",
            author_name="Marketing",
            # The deterministic key, all the way down to the provider.
            client_reference=recipient["idempotency_key"],
            agent_action_id=campaign.get("agent_action_id"),
        )
    except MessagingError as exc:
        # A closed window with no template is a *skip*, not a failure: nothing
        # went wrong, the channel simply does not permit this message today.
        # Filing it as a failure would make a healthy campaign look broken.
        if exc.code in ("MESSAGE_WINDOW_CLOSED", "TEMPLATE_NOT_FOUND",
                        "TEMPLATE_NOT_APPROVED"):
            reason = ("WINDOW_CLOSED" if exc.code == "MESSAGE_WINDOW_CLOSED"
                      else "TEMPLATE_NOT_AVAILABLE")
            return await settle("skipped", reason, detail=exc.detail[:400],
                                conversation_id=conversation["id"])
        if exc.code == "INSUFFICIENT_CREDITS":
            return await settle("blocked", "INSUFFICIENT_CREDITS",
                                detail=exc.detail[:400])
        if exc.code == "NO_CONNECTION":
            return await settle("blocked", "NO_CONNECTION", detail=exc.detail[:400])
        return await settle("failed", "PROVIDER_FAILED", detail=exc.detail[:400],
                            conversation_id=conversation["id"])
    except Exception as exc:  # noqa: BLE001 — one recipient, not the campaign
        logger.warning("campaign %s failed on guest %s", campaign["id"], guest_id,
                       exc_info=True)
        return await settle("failed", "PROVIDER_FAILED",
                            detail=f"{type(exc).__name__}: {exc}"[:400])

    if sent.duplicate:
        # `send()` found this client_reference already delivered. The retry path
        # working exactly as intended — recorded as sent, charged once.
        await run_db(_settle_recipient, recipient["id"], {
            "status": "sent", "reason": None,
            "conversation_id": conversation["id"], "message_id": sent.message["id"],
        })
        return "sent", None

    await _charge_recipient(campaign, recipient, sent)
    await run_db(_settle_recipient, recipient["id"], {
        "status": "sent", "reason": None,
        "conversation_id": conversation["id"], "message_id": sent.message["id"],
    })
    return "sent", None


def _template_values(campaign: dict, guest: dict) -> dict:
    """
    What a WhatsApp template's parameters are filled from.

    Guest facts and offer fields, never free text from the model. A template
    parameter is the one place a campaign's words reach Meta without passing
    `claims.check`, because Meta approved the surrounding sentence — so what
    goes into the blanks has to come from structured data.
    """
    offer = campaign.get("offer") or {}
    return {
        "guest_name": (guest.get("name") or "there").split()[0],
        "property_name": campaign.get("name", ""),
        "discount": str(offer.get("discount_pct", "")),
        "valid_until": str(offer.get("valid_until", "")),
    }


async def _charge_recipient(campaign: dict, recipient: dict, sent) -> None:
    """
    One recipient reached, charged once.

    Separate from the per-message meter inside `send()`, which records
    `whatsapp.message_sent` at zero because Meta bills per conversation. This is
    the hotel's own per-recipient price for the campaign, and it is idempotent
    on the recipient key — so a retry that lands on an already-sent message
    charges nothing.
    """
    try:
        await billing.charge(
            usage_type="marketing.campaign_send",
            units=1,
            idempotency_key=f"marketing.campaign_send:{recipient['idempotency_key']}",
            property_id=campaign["property_id"],
            conversation_id=(sent.message or {}).get("conversation_id"),
            message_id=(sent.message or {}).get("id"),
            external_reference=campaign["id"],
            metadata={"campaignId": campaign["id"], "guestId": recipient["guest_id"]},
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not meter campaign recipient %s", recipient["id"],
                       exc_info=True)


async def _settle_credit(campaign_id: str, property_id: str, result: RunResult) -> None:
    """
    Give back what the campaign did not spend.

    The reservation covered every eligible recipient; the ones who were skipped
    mid-run — because they opted out, or hit the cap — were never charged, and
    holding their credits after the campaign has finished would quietly shrink
    the hotel's spendable balance for six hours.
    """
    campaign = await get(campaign_id, property_id)
    if not campaign or not campaign.get("credit_hold_id"):
        return
    if result.status == "running":
        return  # Still going. The hold stays.
    try:
        await billing.settle(hold_id=campaign["credit_hold_id"], status="settled")
        await run_db(_patch, campaign_id, {"credit_hold_id": None})
    except Exception:  # noqa: BLE001
        logger.warning("could not settle credit hold for campaign %s", campaign_id,
                       exc_info=True)


# ── reporting ───────────────────────────────────────────────────────────────

def _counts(campaign_id: str) -> dict[str, int]:
    rows = (
        get_db().table("campaign_recipients").select("status,reason")
        .eq("campaign_id", campaign_id).limit(5000).execute().data or []
    )
    out: dict[str, int] = {}
    for row in rows:
        out[row["status"]] = out.get(row["status"], 0) + 1
        if row.get("reason"):
            out[f"reason:{row['reason']}"] = out.get(f"reason:{row['reason']}", 0) + 1
    return out


async def stats(campaign_id: str) -> dict:
    """What a campaign screen shows. Counted from the recipient rows, always."""
    counts = await run_db(_counts, campaign_id)
    return {
        "audience": sum(v for k, v in counts.items() if not k.startswith("reason:")),
        "sent": counts.get("sent", 0),
        "skipped": counts.get("skipped", 0),
        "failed": counts.get("failed", 0),
        "blocked": counts.get("blocked", 0),
        "pending": counts.get("pending", 0),
        "reasons": {k[7:]: v for k, v in counts.items() if k.startswith("reason:")},
    }
