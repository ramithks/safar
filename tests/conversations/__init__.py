"""
Conversations as data, played end to end.

**Why the existing tests were not enough.** `tests/test_ai_tools.py` asks whether
one tool behaves; `tests/test_golden_conversations.py` plays seven journeys and
is the model for this; `scripts/replay_e2e.py` plays one real transcript against
the live model. What none of them does is cover *breadth of conversation shape* —
the guest who changes their mind on turn five, the one who asks three unrelated
things mid-booking, the one who says "actually 3 people" after saying 2. §2 asks
for exactly that, and asks for it as scenarios rather than as test functions.

**The model is scripted, and that is the point.** What is asserted here is never
what the agent *said*. It is what survived between turns and what the
deterministic layers decided: which requirement the message raised, which tool
ran, what the session carried forward, and what was never allowed to happen.
Those are properties of `required_actions`, `messaging/session` and
`registry.execute`. Scripting the model removes the one variable that would make
these flap and leaves the ones worth watching. Live-model behaviour is measured
by `scripts/agent_eval.py`, which is a different instrument for a different
question.

**A scenario declares what must not happen as well as what must.** §2 asks for
`forbidden behavior`, and it is the half that catches regressions: a turn that
calls `create_booking` when the guest only asked a price is not visible in any
assertion about what the session ends up holding.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.services.ai import registry, required_actions, runtime
from app.services.ai.context import AgentContext, AgentPrincipal
from app.services.ai.providers import EchoProvider
from app.services.messaging import obligations, session

registry.load_all()

# The fixture hotel. Ids are fixed so a failure names the same room every time.
PROPERTY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PARTNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONVERSATION = "c0c0c0c0-0001-4001-8001-000000000001"
DELUXE = "22222222-2222-4222-8222-222222222222"
GARDEN = "33333333-3333-4333-8333-333333333333"
RATE_PLAN = "11111111-1111-4111-8111-111111111111"

# Far enough out that no test's dates collide with another's, and a round
# number of days so a scenario can say "+2" and mean something readable.
CHECK_IN = date.today() + timedelta(days=30)
CHECK_OUT = CHECK_IN + timedelta(days=2)
LATER_IN = CHECK_IN + timedelta(days=10)
LATER_OUT = LATER_IN + timedelta(days=2)


@dataclass(frozen=True)
class Turn:
    """One guest message, the model's scripted answer, and what must be true after."""

    guest: str

    # What the scripted model does. A tuple of (tool, arguments) runs those
    # tools; a string is what it says. `EchoProvider` takes them in order.
    calls: tuple = ()
    says: str = "Ji, dekhta hoon."

    # ── what the deterministic layer must decide, before any model runs ──
    requires: tuple[str, ...] = ()
    """Tools `required_actions.detect` must raise a requirement for. This is
    asserted against the message *alone*, so it holds whatever the model does."""

    # ── what must have happened by the end of the turn ──
    ran: tuple[str, ...] = ()
    """Tools that must appear in this turn's actions, model-called or forced."""

    forbids: tuple[str, ...] = ()
    """Tools that must NOT run. The half that catches a turn doing too much."""

    session_holds: dict = field(default_factory=dict)
    """A subset of `conversations.guest_profile` that must match after."""

    session_lacks: tuple[str, ...] = ()
    """Keys that must be absent or None — a fact the turn should have cleared."""

    output_holds: dict = field(default_factory=dict)
    """`{tool: {key: value}}` — a subset of what that tool returned. This is
    how a scenario asserts about *money*: that the band applied is the one the
    property set, that a deposit is present, that a discount was clamped."""

    output_lacks: dict = field(default_factory=dict)
    """`{tool: (key, ...)}` — keys the tool's output must NOT carry. A band on
    a booking too small to qualify for one is a discount nobody authorised."""


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    turns: tuple[Turn, ...]

    language: str | None = "hinglish"
    autonomy: int = 3
    channel: str = "whatsapp"

    starting_session: dict = field(default_factory=dict)
    """The conversation's state before turn one — a guest who is already
    mid-booking, or already has a reservation."""

    final_session: dict = field(default_factory=dict)
    never_ran: tuple[str, ...] = ()
    """Tools that must not run at any point in the whole conversation."""


# ── running one ──────────────────────────────────────────────────────────────

@dataclass
class Played:
    scenario: Scenario
    results: list
    actions_per_turn: list[list[str]]
    # The session as it stood after each turn. Snapshotted rather than read at
    # the end, because "what did the guest's third message establish" is the
    # question these scenarios exist to ask and the final state cannot answer it.
    sessions: list[dict]

    @property
    def all_tools(self) -> list[str]:
        return [tool for turn in self.actions_per_turn for tool in turn]


def _ctx(db, scenario: Scenario) -> AgentContext:
    profile = db.rows("conversations")[0].get("guest_profile") or {}
    principal = AgentPrincipal(
        agent_id="agent-1", agent_type="guest", partner_id=PARTNER,
        property_id=PROPERTY, display_name="Mandala",
        autonomy_level=scenario.autonomy,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk"}),
    )
    return AgentContext(
        principal=principal, property_id=PROPERTY, partner_id=PARTNER,
        config_id="config-1", config_version=1, model="",
        conversation_id=CONVERSATION, channel=scenario.channel, actor="guest",
        language=scenario.language, session=dict(profile),
        tool_grants={t: {"tool_name": t, "enabled": True} for t in registry.REGISTRY},
    )


def play(db, scenario: Scenario) -> Played:
    """
    Run every turn, doing between them exactly what the pipeline does.

    The session merge is why this exists rather than a loop of `run_turn`: the
    thing under test is what survives *between* turns, and without the merge
    every turn would start from an empty profile.
    """
    if scenario.starting_session:
        db.rows("conversations")[0]["guest_profile"] = dict(scenario.starting_session)

    results, per_turn, snapshots = [], [], []
    for turn in scenario.turns:
        script: list = []
        if turn.calls:
            script.append(list(turn.calls))
        script.append(turn.says)

        before = len(db.rows("agent_actions"))
        result = asyncio.run(runtime.run_turn(
            _ctx(db, scenario), turn.guest, provider=EchoProvider(script),
        ))
        fresh = [a["tool"] for a in db.rows("agent_actions")[before:]]

        patch = session.patch_from(result.actions)
        patch["obligations"] = obligations.open_from(
            turn.guest, result.actions,
            (db.rows("conversations")[0].get("guest_profile") or {}).get("obligations"),
        )
        session.merge(CONVERSATION, patch)

        results.append(result)
        per_turn.append(fresh)
        snapshots.append(dict(db.rows("conversations")[0].get("guest_profile") or {}))
    return Played(scenario, results, per_turn, snapshots)


def profile(db) -> dict:
    return db.rows("conversations")[0].get("guest_profile") or {}


# ── the assertions, so every scenario is checked the same way ────────────────

def check(db, played: Played) -> list[str]:
    """
    Every failure in the conversation, as sentences. Empty means it passed.

    Collected rather than raised one at a time: a scenario that got the dates
    wrong on turn two probably got several later things wrong too, and seeing
    all of them at once is the difference between one debugging session and
    four.
    """
    problems: list[str] = []
    scenario = played.scenario

    for index, (turn, ran) in enumerate(zip(scenario.turns, played.actions_per_turn), 1):
        where = f"turn {index} ({turn.guest[:40]!r})"

        detected = {r.tool for r in required_actions.detect(turn.guest, {})} | {
            r.tool for r in required_actions.detect(turn.guest, profile(db))
        }
        for tool in turn.requires:
            if tool not in detected:
                problems.append(f"{where}: no requirement raised for {tool}")

        for tool in turn.ran:
            if tool not in ran:
                problems.append(f"{where}: {tool} never ran — ran {ran}")
        for tool in turn.forbids:
            if tool in ran:
                problems.append(f"{where}: {tool} ran and must not have")

        outcomes = {o.tool: o for o in played.results[index - 1].actions}
        for tool, expected in (turn.output_holds or {}).items():
            got = (outcomes.get(tool).output if outcomes.get(tool) else None) or {}
            for key, value in expected.items():
                if got.get(key) != value:
                    problems.append(
                        f"{where}: {tool}.{key} is {got.get(key)!r}, expected {value!r}")
        for tool, keys in (turn.output_lacks or {}).items():
            got = (outcomes.get(tool).output if outcomes.get(tool) else None) or {}
            for key in keys:
                if got.get(key) not in (None, 0, "", [], {}):
                    problems.append(
                        f"{where}: {tool}.{key} is {got.get(key)!r} and must be absent")

        after = played.sessions[index - 1]
        for key, expected in (turn.session_holds or {}).items():
            if after.get(key) != expected:
                problems.append(
                    f"{where}: session {key} is {after.get(key)!r}, expected {expected!r}")
        for key in turn.session_lacks:
            if after.get(key) not in (None, "", [], {}):
                problems.append(
                    f"{where}: session {key} should have been cleared, is {after.get(key)!r}")

    now = profile(db)
    for key, expected in (scenario.final_session or {}).items():
        if now.get(key) != expected:
            problems.append(f"final session {key}: {now.get(key)!r} != {expected!r}")

    for tool in scenario.never_ran:
        if tool in played.all_tools:
            problems.append(f"{tool} ran somewhere in the conversation and must not have")

    return problems
