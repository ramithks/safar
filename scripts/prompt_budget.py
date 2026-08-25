#!/usr/bin/env python3
"""
What a turn spends before the guest has said anything interesting.

§17 asks for the token bill to be audited rather than assumed, and §32 asks for
a before and after. This is the instrument: it reconstructs exactly what
`runtime.run_turn` would send to the model — the same system prompt, the same
tool schemas, narrowed the same way — and prices it, without calling Vertex.

Two numbers matter and they are different:

    the floor      what every step of every turn pays before anything happens
    the bill       the floor times the number of model steps, because the whole
                   catalogue is re-sent on each one

A three-tool turn is four model steps, so a schema saved is saved four times.

    venv/bin/python scripts/prompt_budget.py
    venv/bin/python scripts/prompt_budget.py --agent frontdesk
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.ai import registry, toolsets  # noqa: E402
from app.services.ai.context import AgentContext, AgentPrincipal  # noqa: E402

registry.load_all()

DIM, BOLD, OFF = "\033[2m", "\033[1m", "\033[0m"

# Gemini bills by token; this counts characters and divides. The ratio is stable
# enough across English, Hinglish and JSON that the *comparisons* here hold, and
# every absolute figure is labelled approximate for that reason.
CHARS_PER_TOKEN = 4

# What the guest agent is actually granted in production, read from the agent
# definitions rather than guessed — a measurement against all 82 tools would
# report a saving no hotel could ever make.
def granted_for(agent_type: str) -> set[str]:
    from app.services.ai import agents as agent_defs

    return set(agent_defs.get(agent_type).tools)


def ctx_for(agent_type: str, granted: set[str]) -> AgentContext:
    principal = AgentPrincipal(
        agent_id="a", agent_type=agent_type, partner_id="p", property_id="h",
        display_name="Mandala", autonomy_level=3,
        scopes=frozenset({"tasks", "messaging", "rates", "frontdesk", "marketing",
                          "revenue", "agents"}),
    )
    return AgentContext(
        principal=principal, property_id="h", partner_id="p",
        conversation_id="c", channel="whatsapp", actor="guest",
        tool_grants={t: {"tool_name": t, "enabled": True} for t in granted},
    )


def tokens(blob: str) -> int:
    return len(blob) // CHARS_PER_TOKEN


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", default="guest")
    parser.add_argument("--steps", type=int, default=4,
                        help="model steps in the turn being priced")
    args = parser.parse_args()

    granted = granted_for(args.agent)
    ctx = ctx_for(args.agent, granted)
    print(f"{BOLD}{args.agent} agent{OFF}  {len(granted)} tools granted\n")

    everything = registry.describe_for_provider(ctx, task=None)
    full = tokens(json.dumps(everything))

    print(f"{DIM}per model step, approximate tokens{OFF}")
    print(f"  {'task':26} {'tools':>6} {'schemas':>9} {'vs full':>9}")
    rows = []
    tasks = sorted(set(toolsets.BY_TASK) | set(toolsets.EVERYTHING))
    for task in tasks:
        described = registry.describe_for_provider(ctx, task=task)
        cost = tokens(json.dumps(described))
        saved = full - cost
        rows.append((task, len(described), cost, saved))
        flag = "" if saved else f"  {DIM}(narrows nothing){OFF}"
        print(f"  {task:26} {len(described):>6} {cost:>9,} {-saved:>+9,}{flag}")

    narrowing = [r for r in rows if r[3] > 0]
    if narrowing:
        avg = sum(r[3] for r in narrowing) / len(narrowing)
        print(f"\n  {len(narrowing)}/{len(rows)} tasks narrow, "
              f"saving {avg:,.0f} tokens per step on average")
        print(f"  over a {args.steps}-step turn that is {avg * args.steps:,.0f} tokens")

    print(f"\n{DIM}the heaviest schemas this agent carries{OFF}")
    weights = sorted(
        ((tokens(json.dumps(t)), t["function"]["name"]) for t in everything),
        reverse=True,
    )
    for cost, name in weights[:10]:
        share = 100 * cost / full if full else 0
        print(f"  {cost:>6,} tok  {share:>4.1f}%  {name}")
    tail = sum(c for c, _ in weights[10:])
    print(f"  {tail:>6,} tok  {100 * tail / full if full else 0:>4.1f}%  "
          f"{DIM}the other {max(0, len(weights) - 10)}{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
