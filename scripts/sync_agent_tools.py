#!/usr/bin/env python3
"""
Grants that were added to a definition after the agent was provisioned.

`provisioning._provision` writes one `agent_tools` row per tool in the
definition, and `registry.available_for` shows the model exactly the tools it
has rows for. Both are right. What nobody does is re-run provisioning: an agent
is provisioned once, when the hotel activates, and every tool added to a
definition after that day is invisible to it forever.

That is not a hypothetical. `negotiate_rate` and `get_group_rates` shipped with
the hotel-OS work and were granted to the Sales definition. The Guest agent —
the one answering Instagram on every pilot property — never got them, so a
guest who pushed on price met a model holding a rate card and no engine. It
made a number up. The reply guardrail caught it, which is the only reason this
was a finding rather than a refund.

This reconciles every provisioned agent against its definition:

    ./venv/bin/python scripts/sync_agent_tools.py            # what is missing
    ./venv/bin/python scripts/sync_agent_tools.py --apply    # write it

**It only ever adds.** A tool a property switched off by hand stays off — the
row exists, so it is not missing — and a tool dropped from a definition keeps
its row rather than being revoked under a running conversation. Removing a
capability is a decision somebody should make on purpose, not a side effect of
a deploy.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.db.client import get_db  # noqa: E402
from app.services.ai import registry  # noqa: E402
from app.services.ai.agents import get as get_definition  # noqa: E402

registry.load_all()

APPLY = "--apply" in sys.argv


def main() -> int:
    db = get_db()
    agents = (
        db.table("agents").select("id,property_id,agent_type,status,autonomy_level")
        .execute().data or []
    )
    if not agents:
        print("no agents provisioned")
        return 0

    granted: dict[str, set[str]] = {}
    for row in (db.table("agent_tools").select("agent_id,tool_name").execute().data or []):
        granted.setdefault(row["agent_id"], set()).add(row["tool_name"])

    total = 0
    behind: list[str] = []
    for agent in agents:
        try:
            definition = get_definition(agent["agent_type"])
        except Exception as exc:  # noqa: BLE001
            print(f"  ?  {agent['agent_type']}: no definition ({exc})")
            continue

        # Reported, never written. `agents.autonomy_level` is also set on
        # insert and never again, so a property provisioned before the Guest
        # agent moved from 2 to 3 still has an agent whose every selling tool
        # queues for approval — the guest asks the price and is told the hotel
        # team will confirm. But a hotel that turned the dial down on purpose
        # must not have it turned back up by a deploy, so this prints and stops.
        if agent["autonomy_level"] < definition.autonomy_level:
            behind.append(
                f"    {agent['agent_type']} · {agent['property_id']} "
                f"· level {agent['autonomy_level']}, "
                f"definition ships at {definition.autonomy_level}"
            )

        wanted = {t for t in definition.tools if t in registry.REGISTRY}
        missing = sorted(wanted - granted.get(agent["id"], set()))
        if not missing:
            continue

        total += len(missing)
        print(f"\n{agent['agent_type']} · {agent['property_id']} · {agent['status']}")
        for name in missing:
            print(f"    + {name}")

        if APPLY:
            db.table("agent_tools").upsert(
                [{"agent_id": agent["id"], "tool_name": name} for name in missing],
                on_conflict="agent_id,tool_name",
                ignore_duplicates=True,
            ).execute()

    if behind:
        print("\n── below the level their definition ships at ─────────────────")
        print("\n".join(behind))
        print("    Selling tools sit at autonomy floor 3. Below it they do not")
        print("    fail — they queue for a human, which reads to a guest as")
        print("    'the hotel team will confirm'. Raising it is the hotel's call.")

    if not total:
        print("\nevery agent already holds every tool its definition names")
        return 0

    print(f"\n{total} grants {'written' if APPLY else 'missing'}"
          f"{'' if APPLY else ' — re-run with --apply'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
