#!/usr/bin/env python3
"""
Give every hotel the AI employee it was sold.

    ./venv/bin/python scripts/provision_missing_agents.py            # dry run
    ./venv/bin/python scripts/provision_missing_agents.py --apply

`migrate_partners.py` moved the marketplace's hotels onto StayOS — partner,
property, rooms, rates, billing, a transition entitlement — and never called
`ai/provisioning.provision()`, because until onboarding existed nothing did.
Every agent row in the hotel OS was created either by the seed or by
`pilot_fixture`, so the four real hotels have been running with **zero** agents
while the landing page sells "your hotel's AI employee" at ₹999 a month.

That is not a migration bug so much as a missing step nobody owned. This is the
step, on its own, so it can be run again after any future import.

**Additive and idempotent.** `provision()` skips an agent type the property
already has and never overwrites a config; a hotel that has three agents gets
the two it is missing. Nothing here deletes, disables or repoints anything, so
a re-run is a no-op rather than a reset — which is what makes it safe to run
against production without a maintenance window.

**It does not switch anything on for a guest.** Provisioning writes the agent,
its config, its tools and its permissions. Whether a guest ever reaches one
still depends on a WhatsApp or Instagram connection the hotel has to make.
"""
import argparse
import asyncio
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env.hotelos", override=True)

from app.db.client import get_db  # noqa: E402
from app.services.pms.onboarding import AGENT_TYPES, ensure_agents  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually provision; without it this only reports")
    ap.add_argument("--property", default=None, help="one property id, instead of all")
    args = ap.parse_args()

    db = get_db()
    props = db.table("properties").select("id,name,city,partner_id,status").execute().data or []
    if args.property:
        props = [p for p in props if p["id"] == args.property]
        if not props:
            print(f"no property {args.property}")
            return 1

    agents = db.table("agents").select("property_id,agent_type").execute().data or []
    have: dict[str, set[str]] = {}
    for a in agents:
        have.setdefault(a["property_id"], set()).add(a["agent_type"])

    print(f"\n══ AI employee provisioning ══   {'APPLYING' if args.apply else 'dry run'}\n")
    print(f"{'hotel':34}{'city':13}{'has':6}{'missing'}")

    work: list[tuple[dict, list[str]]] = []
    for p in sorted(props, key=lambda x: x["name"]):
        mine = have.get(p["id"], set())
        missing = [t for t in AGENT_TYPES if t not in mine]
        print(f"{p['name'][:33]:34}{str(p['city'])[:12]:13}{len(mine):<6}"
              f"{', '.join(missing) if missing else '—'}")
        if missing:
            work.append((p, missing))

    if not work:
        print("\nEvery hotel already has all five. Nothing to do.\n")
        return 0

    print(f"\n{len(work)} hotel(s) need agents.")
    if not args.apply:
        print("Dry run — nothing was written. Re-run with --apply.\n")
        return 0

    failures = 0
    for p, missing in work:
        done = await ensure_agents(property_id=p["id"], partner_id=p["partner_id"])
        still = [t for t in missing if t not in done]
        if still:
            failures += 1
            print(f"  FAIL {p['name']}: could not provision {', '.join(still)}")
        else:
            print(f"  ok   {p['name']}: +{len(missing)} ({', '.join(missing)})")

    # Read it back rather than trusting the loop — the point of the exercise is
    # what is in the database, not what this process believes it wrote.
    after = db.table("agents").select("property_id,agent_type").execute().data or []
    recount: dict[str, set[str]] = {}
    for a in after:
        recount.setdefault(a["property_id"], set()).add(a["agent_type"])
    short = [p["name"] for p, _ in work
             if len(recount.get(p["id"], set())) < len(AGENT_TYPES)]
    print(f"\n╔═ {len(work) - failures} provisioned, {failures} failed")
    print(f"╚═ hotels still short of all five: {', '.join(short) if short else 'none'}\n")
    return 1 if (failures or short) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
