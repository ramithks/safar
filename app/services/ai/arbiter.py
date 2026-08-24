"""
Who decides that a conversation stops.

Until now, the model did — alone and finally. It called `request_human_handoff`
with a reason of its own choosing, the runtime set `handoff = True`, and every
deterministic layer downstream stood down, including the one whose entire job is
to make sure a hotel's revenue-generating turns actually run their tool. A
handoff was the one decision in the system with no second opinion.

That held while the model in production reached for the tool rarely. It stopped
holding the moment a model reached for it more often, and the failure it
produced is the expensive kind: a guest asks for a better price, the agent says
somebody will get back to them, and a sale that the pricing engine could have
closed in four seconds waits for a receptionist who is asleep.

**The fix is not to stop the model handing off.** Stopping is genuinely better
than guessing, that is the doctrine the whole agent is built on, and a layer
that could veto a stop outright would be a worse system than the one with the
bug. The fix is that a *claim* is not a *fact*:

    request_human_handoff(reason="guest_asked")

is the model asserting that the guest asked for a person. Whether the guest
asked for a person is decidable by looking at what the guest wrote — it is the
same detector that *forces* a handoff when it matches — so it is not something
that needs to be taken on trust. §24 rule 2: a deterministic problem does not
get solved with a prompt.

So this module answers one question, and answers it from evidence the system
already holds:

    Does the conversation support the reason the model gave?

and if it does not, and there is a business action the turn is owed and the
agent is actually allowed to take, the handoff is turned down **once** and the
model is told which tool it has not tried.

Four properties this is built to have, in the order they matter:

  **It can only ever delay a stop, never prevent one.** `turn.handoff_rejected`
  makes the second request in a turn unconditional. The worst case is one extra
  tool call before the conversation reaches a person — never a guest who asked
  for a human and did not get one.

  **It never overrides a safety stop.** A complaint, a dispute, a policy
  conflict, a failure — all pass through untouched and uninspected. The
  categories that can be turned down are exactly the three that assert something
  checkable, and each is checked against evidence rather than argued with.

  **It does not bypass autonomy.** Nothing here executes anything. Turning a
  handoff down puts the model back in front of `registry.execute`, which runs
  permission, grant, argument validation, the property's own rules and
  `autonomy.decide` exactly as it always did. An agent at level 2 that is sent
  back to negotiate gets its negotiation queued for approval — and *that* is
  corroborating evidence, so the handoff it then requests is valid and stands.
  The loop closes correctly rather than fighting itself.

  **It requires somewhere better to go.** A handoff with no unmet requirement
  behind it is never turned down, however thin its reason. "I do not know" is
  only contradicted when the system does know.

The model's vocabulary is unchanged — `REASONS` in tools/communication.py still
says `guest_asked`, and the model still supplies it. What changed is that the
string is now a *suggestion* that gets mapped to a category and checked, which
is §6: the model may propose a reason, the backend decides what it means.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── the categories ───────────────────────────────────────────────────────────
#
# §6: structured reasons, not arbitrary strings. These are what the *business*
# distinguishes — who a handoff is waiting on, and what would resolve it — which
# is not the same axis as the model-facing vocabulary. Two model reasons can map
# to one category and that is fine; what must not happen is a category being
# invented by whatever the model typed.

GUEST_REQUESTED_HUMAN = "GUEST_REQUESTED_HUMAN"
POLICY_REQUIRES_HUMAN = "POLICY_REQUIRES_HUMAN"
AUTONOMY_REQUIRES_APPROVAL = "AUTONOMY_REQUIRES_APPROVAL"
UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
SAFETY_ESCALATION = "SAFETY_ESCALATION"
AGENT_FAILURE = "AGENT_FAILURE"
STAFF_REQUESTED = "STAFF_REQUESTED"

CATEGORIES = (
    GUEST_REQUESTED_HUMAN, POLICY_REQUIRES_HUMAN, AUTONOMY_REQUIRES_APPROVAL,
    UNSUPPORTED_REQUEST, SAFETY_ESCALATION, AGENT_FAILURE, STAFF_REQUESTED,
)

# The model's word → what it means here.
#
# Deliberately total over `tools.communication.REASONS`, and asserted to be by
# `tests/test_arbiter.py` — a reason with no mapping would fall to the default
# below and be checkable when it should not be, which is the one direction this
# must not fail in.
_CATEGORY_OF = {
    "guest_asked": GUEST_REQUESTED_HUMAN,
    "autonomy_limit": AUTONOMY_REQUIRES_APPROVAL,
    "no_knowledge": UNSUPPORTED_REQUEST,
    "low_confidence": UNSUPPORTED_REQUEST,
    "unusual_request": UNSUPPORTED_REQUEST,
    # Money in dispute and people who are upset. Never inspected, never
    # overridden, and grouped here so that is visible rather than implied.
    "complaint": SAFETY_ESCALATION,
    "refund": POLICY_REQUIRES_HUMAN,
    "policy_conflict": POLICY_REQUIRES_HUMAN,
}

# Categories whose claim is about the *conversation* and can therefore be
# checked against it. Everything not in here stands unconditionally.
#
# `POLICY_REQUIRES_HUMAN` is not in here on purpose. A hotel's own rule that a
# refund conversation goes to a person is a hotel's decision, and second-guessing
# it would be this layer overriding a customer's configuration — §24 rule 9, and
# the thing §20 is about.
CHECKABLE = frozenset({
    GUEST_REQUESTED_HUMAN, AUTONOMY_REQUIRES_APPROVAL, UNSUPPORTED_REQUEST,
})

# An unmapped reason is treated as the most conservative thing it could be.
# A model that invents a reason string gets a handoff that stands.
DEFAULT_CATEGORY = UNSUPPORTED_REQUEST


def categorise(reason: str | None) -> str:
    """The business category for a reason the model supplied."""
    return _CATEGORY_OF.get((reason or "").strip().lower(), DEFAULT_CATEGORY)


def enabled() -> bool:
    """
    Whether a handoff is checked against the conversation at all.

    A switch rather than a constant, for the same two reasons
    `required_actions.enforcement_enabled` is one, and neither is doubt about
    the feature.

    **It is how the improvement is measured.** The only honest statement of
    what this layer bought is the same scenarios, on the same day, against the
    same hotel and the same model, with it on and off. A hard-coded `True`
    makes that comparison require a code edit and a redeploy, which means it
    does not get made.

    **And it is the rollback.** If checking a handoff ever turns out to be
    wrong for one property, the answer is an environment variable rather than a
    hotfix at eleven at night. §12.

    Judging keeps running either way: with it off, the judgement is recorded on
    the run and the handoff goes through regardless, so `agent_runs` still says
    what *would* have been turned down. A layer whose effect can only be
    measured by turning it on is one nobody can decide about.

    Read per call rather than cached at import, so a test can flip it and the
    eval runner can flip it between sweeps in one process.
    """
    import os

    return os.environ.get("AGENT_ARBITRATE_HANDOFFS", "true").strip().lower() not in (
        "false", "0", "no", "off",
    )


# ── did the guest actually ask for a person ──────────────────────────────────
#
# The same question `required_actions` already answers, asked from the other
# side. That module matches this and *forces* a handoff; this module matches it
# and *permits* one. Deriving both from one detector is what stops the two
# disagreeing — a sentence that would force a handoff can never be one that
# fails to justify it.

def _guest_asked_for_a_person(message: str) -> bool:
    from app.services.ai import required_actions

    return bool(required_actions._ESCALATION_ASK.search(message or ""))


# A guest can also ask for a person without any of those phrasings — by naming
# one. "Can I speak to Priya", "manager se baat karao". The escalation detector
# is tuned for the common forms; this catches the shape it is not, and it only
# ever *widens* what counts as a valid handoff, so a miss here costs nothing
# that was not already the case.
_NAMES_A_PERSON = re.compile(
    r"\b(manager|owner|reception(ist)?|front\s*desk|staff|malik|receptionist)\b",
    re.IGNORECASE,
)


# The tools that *are* stopping. A handoff can never be turned down on the
# grounds that the model has not tried handing off — and `required_actions`
# genuinely produces such a requirement, because a guest who asks for a person
# creates an ESCALATION whose tool is this one. Without this the arbiter would
# reject a handoff and instruct the model to hand off.
_STOPPING = frozenset({"request_human_handoff", "escalate_to_team"})


@dataclass(frozen=True)
class Judgement:
    """What the arbiter decided about one handoff request, and why."""

    category: str
    valid: bool
    reason_given: str | None
    # The requirement the model is being sent back to, when it is being sent
    # back to one. `None` on every valid handoff.
    instead: str | None = None
    intent: str | None = None
    # One line, for the run record and the logs. Written for a person reading a
    # transcript six weeks later.
    why: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def rejected(self) -> bool:
        return not self.valid


def _runnable_requirements(ctx) -> list:
    """
    What this turn still owes that the agent could actually do.

    Three filters, all of them about not sending the model back to a door it
    cannot open:

      * the requirement is unmet — nothing satisfying it has run;
      * the agent has been *granted* the tool, so it is a tool the model can
        see and call;
      * nothing has already tried it this turn. A `negotiate_rate` that came
        back refused is not an unexplored option, it is the reason the model is
        stopping, and sending it back there would be a loop.

    Autonomy is deliberately *not* filtered on. An agent at level 2 that is sent
    back to negotiate gets `awaiting_approval` from `registry.execute` — which
    is the correct business outcome, is visible to the guest as "it has gone to
    the team", and then corroborates the handoff the model asks for next. See
    §5: the arbiter must respect the ladder, and respecting it means letting it
    answer rather than guessing what it would say.
    """
    from app.services.ai import required_actions

    turn = ctx.turn
    outstanding = required_actions.unmet(list(turn.requirements), turn.outcomes)
    attempted = {o.tool for o in turn.outcomes}
    return [
        r for r in outstanding
        if r.tool not in _STOPPING
        and r.tool in (ctx.tool_grants or {})
        and r.tool not in attempted
        and not (r.satisfied_by & attempted)
    ]


def judge(reason: str | None, ctx) -> Judgement:
    """
    Whether this handoff is supported by the conversation it claims to describe.

    Pure, and reads only what the turn already holds. No database, no model, no
    network — this runs inside `registry.execute`'s precheck, in front of a
    guest who is waiting.
    """
    category = categorise(reason)
    turn = getattr(ctx, "turn", None)
    message = getattr(turn, "message", "") if turn else ""

    def valid(why: str, **evidence) -> Judgement:
        return Judgement(category=category, valid=True, reason_given=reason,
                         why=why, evidence=evidence)

    # A stop that has already been delayed once is not delayed again. This is
    # the termination guarantee and it comes first, before anything else can
    # have an opinion: the arbiter's power is to make the model try one more
    # thing, and it spends that power once per turn.
    if turn is not None and turn.handoff_rejected:
        return valid("already turned this turn down once; a second request stands")

    # **The guest's own words outrank the model's label for them.**
    #
    # Checked before the category, and that order is the fix for a real gap in
    # the first version of this. A guest writes "manager se baat karao" and the
    # model — for whatever reason — files the handoff as `no_knowledge`. That
    # categorises as UNSUPPORTED_REQUEST, which is checkable, and with a
    # negotiation outstanding on the same turn the handoff would have been
    # turned down. The guest asked for a person and would not have got one,
    # because the model chose the wrong word for why.
    #
    # The reason string is the model's *description* of the conversation. The
    # conversation is the conversation. When they disagree about something this
    # module can read directly, the conversation wins — and it wins in the
    # direction of stopping, which is the only direction it is safe to be wrong
    # in.
    if _guest_asked_for_a_person(message) or _NAMES_A_PERSON.search(message or ""):
        return valid("the guest asked for a person, whatever reason was given",
                     message=(message or "")[:160])

    # A tool that actually broke. An agent that has hit a real failure must be
    # able to stop, and sending it back to try another one is how a broken
    # system produces a wrong answer instead of no answer.
    failed = [o for o in (turn.outcomes if turn else []) if o.status == "failed"]
    if failed:
        return valid(f"{failed[0].tool} failed on this turn", tool=failed[0].tool)

    if category not in CHECKABLE:
        return valid(f"{category} is not a claim about the conversation")

    if category == GUEST_REQUESTED_HUMAN:
        # The corroboration above already covers this; reaching here means the
        # guest did not ask, which is exactly the claim being tested.
        pass
    elif category == AUTONOMY_REQUIRES_APPROVAL:
        queued = [o for o in (turn.outcomes if turn else [])
                  if o.status in ("awaiting_approval", "refused")]
        if queued:
            return valid(
                f"{queued[0].tool} came back {queued[0].status} on this turn",
                tool=queued[0].tool, status=queued[0].status,
            )
    else:  # UNSUPPORTED_REQUEST
        pass  # never corroborated by anything; falls through to the test below

    # The claim is uncorroborated. That alone is not enough to turn it down —
    # the model may simply be right in a way nothing here can see. It is turned
    # down only if the system positively knows somewhere better to go.
    runnable = _runnable_requirements(ctx) if turn is not None else []
    if not runnable:
        return valid(
            f"{category} is not corroborated, but there is no business action "
            f"outstanding that this agent could take instead"
        )

    first = runnable[0]
    return Judgement(
        category=category,
        valid=False,
        reason_given=reason,
        instead=first.tool,
        intent=first.intent,
        why=(
            f"the model claimed {category} on a turn the system reads as "
            f"{first.intent}, and {first.tool} has not been tried"
        ),
        evidence={
            "message": (message or "")[:160],
            "outstanding": [r.tool for r in runnable],
            "already_ran": sorted({o.tool for o in turn.outcomes}),
        },
    )


# What the model is told when its handoff is turned down.
#
# Written as an instruction rather than an explanation, because a refusal the
# model cannot act on is a refusal that becomes an apology to the guest. It
# names the tool, says the conversation has not been handed over, and says the
# handoff is available again — the last part matters: a model that believes it
# has been trapped will spend the rest of the turn trying to escape.
def instruction_for(judgement: Judgement) -> str:
    return (
        f"Not handed over. This conversation has not been passed to anyone and "
        f"the guest has not been told it has. Before stopping, this message "
        f"needs {judgement.instead}, which has not run yet on this turn — call "
        f"it now and answer the guest with what it gives you. If it refuses, or "
        f"if it comes back needing someone at the hotel to approve it, request "
        f"the handoff again and it will go through."
    )
