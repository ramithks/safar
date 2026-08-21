"""
Approving a proposal, and then actually doing it.

Until now approval recorded a decision and stopped. That was honest but half a
feature: a manager pressed Approve, the row said `approved`, and the guest waited
for something that never happened.

    proposal ──► approval ──► REVALIDATE ──► execute ──► usage ──► executed
                                   │
                                   └──► refused, with a reason and no side effect

**Revalidation is the whole of it.** A proposal and its approval are separated by
however long a manager takes to look at their phone. In that time a room sells, a
quote expires, a rate moves, a guest cancels, a receptionist takes the
conversation over, or somebody removes the agent's permission. Executing on the
strength of the approval alone would carry out a decision made about a hotel that
no longer exists.

So the approval is re-derived rather than replayed. Everything the runtime checks
on a live turn is checked again — the agent is still active, still holds the
grant, still holds the scope — plus what only matters after a delay: the tool's
own `revalidate` hook, and whether the conversation is still the agent's to act
on.

**An approval lends judgement, not access.** `registry.execute(approved=True)`
skips exactly one gate, the autonomy level, because that is the gate the manager
is standing in for. A tool the agent was never granted stays ungranted; a scope
it does not hold stays unheld. A manager cannot approve their way past the
permission model, and would not want to be able to.

Failure is terminal. A stale proposal becomes `failed` and cannot be approved
again, because the correct recovery is for the agent to propose against the
hotel as it is now — not for a human to keep pressing a button until the world
agrees with a five-minute-old snapshot.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.client import get_db, run_db
from app.services.ai import agents
from app.services.ai import context as ai_context
from app.services.ai import registry, runtime, usage as usage_mod
from app.services.ai.errors import AgentError
from app.services.ai.schemas import ToolOutcome

logger = logging.getLogger(__name__)

# How old a proposal may be and still be executable. Long enough for a manager
# to finish a check-in and come back; short enough that nobody approves
# yesterday's idea. A proposal older than this is not refused for being wrong —
# it is refused for being unreviewable, since nobody can remember the state it
# was made in.
MAX_PROPOSAL_AGE = timedelta(hours=6)


@dataclass
class ExecutionResult:
    ok: bool
    status: str
    code: str | None = None
    detail: str | None = None
    result_type: str | None = None
    result_id: str | None = None


def _load(action_id: str, property_id: str) -> tuple[dict, dict] | None:
    db = get_db()
    actions = (
        db.table("agent_actions").select("*")
        .eq("id", action_id).eq("property_id", property_id)
        .limit(1).execute().data or []
    )
    if not actions:
        return None
    runs = (
        db.table("agent_runs").select("*")
        .eq("id", actions[0]["run_id"]).limit(1).execute().data or []
    )
    return actions[0], (runs[0] if runs else {})


def _conversation(conversation_id: str) -> dict | None:
    rows = (
        get_db().table("conversations").select("*")
        .eq("id", conversation_id).limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _settle(action_id: str, patch: dict) -> dict | None:
    rows = get_db().table("agent_actions").update(patch).eq("id", action_id).execute().data
    return rows[0] if rows else None


async def execute_approved(
    *, action_id: str, property_id: str, member
) -> ExecutionResult:
    """
    Run a proposal a human has approved, or refuse it with a reason.

    `member` is the approver, already checked for the 'agents' permission by the
    endpoint. Their identity is recorded on the action; their permissions do not
    become the agent's.
    """
    started = time.monotonic()
    loaded = await run_db(_load, action_id, property_id)
    if not loaded:
        return ExecutionResult(False, "not_found", "ACTION_NOT_FOUND",
                               "That action does not belong to this property.")
    action, run = loaded

    if action["status"] not in ("awaiting_approval", "approved"):
        return ExecutionResult(
            False, action["status"], "ALREADY_DECIDED",
            f"This action is already {action['status'].replace('_', ' ')}.",
        )

    async def refuse(code: str, detail: str) -> ExecutionResult:
        # Terminal. The recovery is a fresh proposal against the hotel as it is
        # now, not another press of the same button.
        await run_db(_settle, action_id, {
            "status": "failed",
            "refusal_reason": f"{code}: {detail}",
            "error": f"{code}: {detail}",
            "approved_by": member.profile_id,
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "latency_ms": int((time.monotonic() - started) * 1000),
        })
        return ExecutionResult(False, "failed", code, detail)

    # ── staleness ────────────────────────────────────────────────────────────
    created = datetime.fromisoformat(str(action["created_at"]).replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - created
    if age > MAX_PROPOSAL_AGE:
        return await refuse(
            "PROPOSAL_STALE",
            f"This was proposed {int(age.total_seconds() // 3600)} hours ago. "
            "Ask the agent to propose again against today's availability.",
        )

    # ── the tool still exists ────────────────────────────────────────────────
    spec = registry.REGISTRY.get(action["tool"])
    if spec is None:
        return await refuse("TOOL_UNAVAILABLE",
                            f"{action['tool']} is no longer a tool this system has.")

    # ── the agent is still the agent ─────────────────────────────────────────
    conversation = None
    if run.get("conversation_id"):
        conversation = await run_db(_conversation, run["conversation_id"])
        if conversation is None:
            return await refuse("CONVERSATION_CHANGED",
                                "The conversation this was proposed on no longer exists.")

    # The actor comes from the agent's *type*, not from a constant.
    #
    # This was hardcoded to 'guest', which was invisible while the Guest agent
    # was the only one that could propose anything. It stopped being invisible
    # the moment a Front Desk check-in reached this function: approving it would
    # have reloaded the context guest-scoped, and `require_reservation` would
    # have refused a booking that never belonged to a conversation — a manager
    # pressing Approve and watching it fail for a reason with no connection to
    # what they approved.
    agent_type = _agent_type(action["agent_id"]) or "guest"

    try:
        ctx = await ai_context.load_context(
            property_id=property_id,
            partner_id=action["partner_id"],
            agent_type=agent_type,
            conversation_id=run.get("conversation_id"),
            guest_id=(conversation or {}).get("guest_id"),
            guest_name=(conversation or {}).get("guest_name"),
            reservation_id=(conversation or {}).get("reservation_id"),
            channel=(conversation or {}).get("channel") or "whatsapp",
            actor=agents.actor_for(agent_type),
            member_id=run.get("member_id"),
        )
    except AgentError as exc:
        # Paused, deleted, or its configuration withdrawn. An approval cannot
        # resurrect an agent somebody switched off.
        return await refuse("AGENT_CHANGED", exc.reason)

    if action["agent_id"] != ctx.principal.agent_id:
        return await refuse("AGENT_CHANGED",
                            "A different agent is configured for this property now.")

    # The run is reused rather than reopened: this action belongs to the turn
    # that proposed it, and giving it a second run id would break the trace an
    # auditor follows from the guest's message.
    ctx.run_id = action["run_id"]

    # ── run it ───────────────────────────────────────────────────────────────
    outcome: ToolOutcome = await registry.execute(
        action["tool"], action.get("input") or {}, ctx, approved=True
    )

    if not outcome.ok:
        code, _, detail = (outcome.refusal_reason or "").partition(": ")
        return await refuse(code or "REVALIDATION_FAILED",
                            detail or outcome.refusal_reason or "The tool refused.")

    # Point the created thing back at this action, exactly as a live turn does.
    #
    # `runtime.link_result` runs inside `run_turn`, which the approval path never
    # enters — so without this call the provenance column would be written for
    # everything the agent does unattended and for nothing it had approved. That
    # is the wrong way round: a folio charge *always* requires approval, so the
    # one result type where "which action produced this" matters most would have
    # been the only one that never recorded it.
    await run_db(runtime.link_result, outcome, action_id)

    await run_db(_settle, action_id, {
        "status": "executed",
        "output": outcome.output,
        "result_type": outcome.result_type,
        "result_id": outcome.result_id,
        "approved_by": member.profile_id,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "latency_ms": int((time.monotonic() - started) * 1000),
    })

    # Metered like any other action. It happened, a guest is affected by it, and
    # it consumed the same service a live turn would have.
    try:
        # The tier comes off the run that proposed this, not from a fresh
        # routing decision. Approving a negotiation an hour later does not make
        # it an ordinary enquiry, and re-deriving the tier here would price the
        # same piece of work differently depending on how long a manager took to
        # look at their phone.
        usage = usage_mod.TurnUsage(
            provider=ctx.provider, model=ctx.model, tier=run.get("model_tier") or "",
        )
        usage.count(spec.usage_type)
        await usage_mod.record_run(
            run_id=action["run_id"],
            property_id=property_id,
            partner_id=action["partner_id"],
            agent_id=action["agent_id"],
            # Charged as what the agent *is*. Omitted, this defaulted to
            # `ai.guest_conversation`, so an approved front desk action billed a
            # hotel 8 credits under the guest line instead of 6 under
            # `ai.frontdesk_assist` — a wrong number on an invoice, filed under
            # the wrong heading.
            agent_type=agent_type,
            conversation_id=run.get("conversation_id"),
            usage=usage,
            action_ids={spec.usage_type: [action_id]},
        )
    except Exception:  # noqa: BLE001
        logger.warning("could not meter approved action %s", action_id, exc_info=True)

    return ExecutionResult(
        True, "executed",
        result_type=outcome.result_type, result_id=outcome.result_id,
    )


def _agent_type(agent_id: str) -> str | None:
    try:
        rows = (
            get_db().table("agents").select("agent_type")
            .eq("id", agent_id).limit(1).execute().data or []
        )
        return rows[0]["agent_type"] if rows else None
    except Exception:  # noqa: BLE001
        return None


# ── what an owner reads instead of the audit line ────────────────────────────
#
# `autonomy.explain()` writes the precise sentence: "create_booking needs
# autonomy 3 (transactional within limits); this agent is at 2 (safe
# operational actions)". That belongs in the record, and it belonged on the
# approvals screen for as long as nobody looked at the approvals screen as a
# hotel owner. It names a function, a ladder and two rungs of a scale nothing
# else in the product mentions.
#
# So the audit keeps its sentence and the owner gets one about their hotel. The
# tool is what they actually care about — "the AI wants to take a booking" —
# and the reason is always the same underneath: it is not allowed to on its
# own, and they are being asked.

ACTION_WORDS: dict[str, str] = {
    "create_booking": "take a booking",
    "create_booking_payment_link": "send a payment link",
    "create_payment_link": "send a payment link",
    "post_folio_charge": "add a charge to a guest's bill",
    "propose_rate_exception": "give a discount bigger than its limit",
    "execute_campaign": "send a campaign to your guests",
    "check_in": "check a guest in",
    "check_out": "check a guest out",
    "order_service": "order an extra for a guest",
    "create_task": "raise a job for your team",
    "assign_task": "assign a job",
    "set_room_status": "change a room's status",
    "send_message": "send a message to a guest",
    "release_hold": "release a held room",
}


def why_waiting(tool: str, status: str) -> str | None:
    """
    One sentence, for the person who has to say yes or no.

    Returns None for anything that is not waiting — a finished action needs no
    explanation, and inventing one would put a sentence under every row.
    """
    if status != "awaiting_approval":
        return None
    what = ACTION_WORDS.get(tool)
    if not what:
        # An unmapped tool still gets a true sentence rather than its own name.
        return "Your AI employee needs your approval before it can do this."
    return f"Your AI employee wants to {what}, and needs your approval first."
