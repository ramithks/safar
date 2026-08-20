"""
Give every hotel's AI employee the tools that shipped after it was set up.

    python scripts/sync_agent_tools.py            say what is missing
    python scripts/sync_agent_tools.py --apply    grant it

An agent's tools are rows in `agent_tools`, written once when the hotel was
provisioned. So a tool added to a definition afterwards — `create_payment_link`,
`order_service`, `get_check_in_link` — exists in the code, is registered, is
listed in the agent definition, and is still not callable by any hotel that
signed up yesterday. Nothing fails; the agent simply never offers to take a
payment, and the reason is invisible from every screen.

`provision()` would fix it, and also writes a new `agent_configs` version per
agent per run. That is the wrong trade for a backfill: a version bump means
"somebody changed how this agent behaves", and reading a hotel's config history
should not turn up eight versions that differ in nothing.

**Additive only.** It inserts missing rows and never deletes one, so a hotel
that switched a tool off by hand stays switched off — the same rule
`provision`'s upsert follows, and for the same reason.
"""
import argparse
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.db.client import get_db  # noqa: E402
from app.services.ai import registry  # noqa: E402
from app.services.ai.agents import all_definitions  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the missing rows")
    args = ap.parse_args()

    registry.load_all()
    definitions = all_definitions()
    db = get_db()

    agents = (db.table("agents")
              .select("id,agent_type,property_id").limit(2000).execute().data or [])
    names = {p["id"]: p["name"] for p in
             (db.table("properties").select("id,name").limit(2000).execute().data or [])}

    granted = (db.table("agent_tools")
               .select("agent_id,tool_name").limit(20000).execute().data or [])
    have: dict[str, set[str]] = {}
    for row in granted:
        have.setdefault(row["agent_id"], set()).add(row["tool_name"])

    missing: list[dict] = []
    for agent in agents:
        definition = definitions.get(agent["agent_type"])
        if definition is None:
            continue
        for name in definition.tools:
            if name not in have.get(agent["id"], set()):
                missing.append({"agent_id": agent["id"], "tool_name": name,
                                "_property": names.get(agent["property_id"], "?"),
                                "_type": agent["agent_type"]})

    if not missing:
        print(f"  {len(agents)} agents, every tool already granted.")
        return 0

    by_hotel: dict[str, list[str]] = {}
    for m in missing:
        by_hotel.setdefault(m["_property"], []).append(f"{m['_type']}.{m['tool_name']}")
    for hotel, items in sorted(by_hotel.items()):
        print(f"  {hotel}")
        for item in sorted(items):
            print(f"      + {item}")

    if not args.apply:
        print(f"\n  {len(missing)} grants missing. Re-run with --apply to write them.")
        return 0

    # A tool that is not in the registry must never reach `agent_tools`: the
    # row would be a grant to call something that does not exist, and the
    # failure would surface as an agent that goes quiet mid-conversation.
    unknown = {m["tool_name"] for m in missing} - set(registry.REGISTRY)
    if unknown:
        print(f"\n  refusing: not registered — {', '.join(sorted(unknown))}")
        return 1

    db.table("agent_tools").upsert(
        [{"agent_id": m["agent_id"], "tool_name": m["tool_name"]} for m in missing],
        on_conflict="agent_id,tool_name", ignore_duplicates=True,
    ).execute()
    print(f"\n  granted {len(missing)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
