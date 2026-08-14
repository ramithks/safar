"""
Turning a definition into rows.

This is where the claim "one runtime, different configurations" is either true
or it is not. Activating an agent should be an insert, and here it is: an
`agents` row, a versioned `agent_configs` row, one `agent_tools` row per tool
and one `agent_permissions` row per scope. No new code path, no new dispatcher.

Two guards worth naming.

`available=False` definitions are refused. The Front Desk, Sales, Marketing and
Owner agents have shapes but nothing has been built to supervise them, and an
agent that can be provisioned before its slice is an agent that will be.

Every tool named in a definition is checked against the registry before
anything is written. A grant for a tool that does not exist is a grant that
looks like a capability in the UI and refuses at runtime, and the moment to
catch that is now rather than in front of a guest.
"""
import logging

from app.db.client import get_db, run_db
from app.services.ai import registry
from app.services.ai.agents import AgentDefinition, get as get_definition
from app.services.ai.errors import AgentNotConfigured

logger = logging.getLogger(__name__)

registry.load_all()


def _unknown_tools(definition: AgentDefinition) -> list[str]:
    return [t for t in definition.tools if t not in registry.REGISTRY]


def _provision(
    property_id: str,
    partner_id: str,
    definition: AgentDefinition,
    model: str,
    provider: str,
    created_by: str | None,
) -> dict:
    db = get_db()

    existing = (
        db.table("agents")
        .select("*")
        .eq("property_id", property_id)
        .eq("agent_type", definition.agent_type)
        .limit(1)
        .execute()
        .data
        or []
    )

    if existing:
        agent = existing[0]
    else:
        agent = (
            db.table("agents")
            .insert({
                "partner_id": partner_id,
                "property_id": property_id,
                "agent_type": definition.agent_type,
                "name": definition.name,
                "status": "active",
                "autonomy_level": definition.autonomy_level,
                "channels": list(definition.channels),
                "limits": definition.limits,
            })
            .execute()
            .data[0]
        )

    version = db.rpc("next_agent_config_version", {"p_agent": agent["id"]}).execute().data or 1
    config = (
        db.table("agent_configs")
        .insert({
            "agent_id": agent["id"],
            "version": int(version),
            "system_prompt": definition.system_prompt,
            "persona": definition.persona,
            "provider": provider,
            "model": model,
            # Empty: the router's tier decides temperature and the token
            # ceiling. A property that wants its own can set them here.
            "params": {},
            "knowledge_scope": list(definition.knowledge_scope),
            "escalation_rules": definition.escalation_rules,
            "created_by": created_by,
            "published_at": "now()",
        })
        .execute()
        .data[0]
    )

    db.table("agents").update({"active_config_id": config["id"]}).eq("id", agent["id"]).execute()

    # Upsert rather than replace: a property that disabled a tool by hand should
    # not have it switched back on because someone re-ran provisioning.
    for name in definition.tools:
        db.table("agent_tools").upsert(
            {"agent_id": agent["id"], "tool_name": name},
            on_conflict="agent_id,tool_name",
            ignore_duplicates=True,
        ).execute()

    for scope in definition.permissions:
        db.table("agent_permissions").upsert(
            {"agent_id": agent["id"], "scope": scope, "granted_by": created_by},
            on_conflict="agent_id,scope",
            ignore_duplicates=True,
        ).execute()

    return {"agentId": agent["id"], "configId": config["id"], "version": int(version)}


async def provision(
    *,
    property_id: str,
    partner_id: str,
    agent_type: str = "guest",
    model: str | None = None,
    provider: str = "vertex_ai",
    created_by: str | None = None,
) -> dict:
    definition = get_definition(agent_type)
    if not definition.available:
        raise AgentNotConfigured(
            f"the {agent_type} agent is defined but not built yet — "
            "it has no runtime behaviour to supervise"
        )

    missing = _unknown_tools(definition)
    if missing:
        raise AgentNotConfigured(
            f"{agent_type} names tools that are not registered: {', '.join(missing)}"
        )

    return await run_db(
        _provision,
        property_id,
        partner_id,
        definition,
        # Empty by default: the router picks by tier. A model id on a config row
        # is one somebody has to edit per hotel every time a tier moves.
        model or "",
        provider,
        created_by,
    )
