"""
Which agent answers this conversation.

Until now there was one answer and it was written four times as the literal
string `"guest"` — in `pipeline.process_conversation`, in the billing gate
beside it, in the actor it passed, and in `approvals`. That was correct while
the Guest agent was the only one, and it is the kind of correct that quietly
becomes wrong: the Front Desk agent answers on `internal`, the Sales agent will
answer on the same WhatsApp number the Guest agent does, and neither can be
reached by a constant.

    conversation.channel  ──►  agents where channel ∈ agents.channels
                                        │
                                        └──►  agent_type  ──►  runtime, actor,
                                                               usage type

**The channel decides, and the property's own rows decide what the channel
means.** `agents.channels` is a column a hotel configures, not a mapping in
code, so a property that puts its Sales agent on WhatsApp and its Guest agent
nowhere gets exactly that. There is no dictionary here saying whatsapp → guest;
there is a query.

**Ambiguity resolves by priority, and priority is explicit.** Two agents may
legitimately claim one channel — a Sales agent and a Guest agent both on
WhatsApp is a real configuration, not a mistake. Rather than let it depend on
insert order, `_PRIORITY` names the winner, and the reasoning is one line: the
more specialised agent wins, because a property that has bothered to configure
Sales on WhatsApp meant it.

**A property with no agent for a channel gets no agent**, not the Guest agent by
default. The caller sends it to a human. Falling back would mean a hotel that
deliberately switched its Guest agent off still had one answering, which is the
opposite of a kill switch.
"""
import logging

from app.db.client import get_db, run_db
from app.services.ai.agents import actor_for

logger = logging.getLogger(__name__)

# When more than one active agent claims a channel. Lower wins.
#
# Sales before Guest: both are guest-facing on WhatsApp, and a property that
# configured Sales onto the channel did it deliberately. Front desk is first
# because `internal` is its own channel and nothing else should ever be there —
# if something is, it is a misconfiguration and the staff agent is the safer of
# the two to hand an internal thread to.
_PRIORITY = {"frontdesk": 0, "sales": 1, "marketing": 2, "owner": 3, "guest": 4}


def _agents_for(property_id: str, channel: str) -> list[dict]:
    rows = (
        get_db()
        .table("agents")
        .select("id,agent_type,channels,status,autonomy_level")
        .eq("property_id", property_id)
        .eq("status", "active")
        .execute()
        .data
        or []
    )
    # Filtered in Python rather than with a `cs` operator: `channels` is a text
    # array and PostgREST's containment syntax is easy to get subtly wrong in a
    # way that fails open. A property has at most five agents.
    return [r for r in rows if channel in (r.get("channels") or [])]


async def agent_for_channel(property_id: str, channel: str) -> str | None:
    """
    The agent type that should answer on this channel, or None.

    None is a real answer and the caller must handle it: the conversation goes
    to a person. It is not an error and it is not a reason to guess.
    """
    matches = await run_db(_agents_for, property_id, channel)
    if not matches:
        return None
    if len(matches) > 1:
        logger.info(
            "property %s has %d agents on %s; picking by priority",
            property_id, len(matches), channel,
        )
    matches.sort(key=lambda r: _PRIORITY.get(r["agent_type"], 99))
    return matches[0]["agent_type"]


async def route(conversation: dict) -> tuple[str, str] | None:
    """
    `(agent_type, actor)` for a conversation, or None if nothing should answer.

    The two always travel together — an agent type without its actor is how a
    staff agent ends up guest-scoped — so they are returned as a pair rather
    than left to the caller to derive.
    """
    agent_type = await agent_for_channel(
        conversation["property_id"], conversation.get("channel") or "whatsapp"
    )
    if agent_type is None:
        return None
    return agent_type, actor_for(agent_type)
