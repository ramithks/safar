"""
The system prompt, assembled.

**No business rule is written here.** Not the discount ceiling, not the autonomy
level, not which tools exist — all of those are rows, checked in Python, and
repeating them in the prompt would create a second copy that drifts and that a
guest can argue with.

What belongs in a prompt is the part a model is actually good at: how to speak,
what to do when it does not know, and the standing instruction to get facts from
tools rather than from memory. The hotel supplies its own voice through
`agent_configs.system_prompt`, which is appended rather than interleaved so a
property can never accidentally overwrite the safety half by editing its
persona.

The ordering is deliberate: our rules first, the hotel's voice second, the
conversation last. A model reads the whole thing, but the parts that must not be
negotiable are the parts furthest from anything a guest typed.
"""
from app.services.ai.context import AgentContext

# The part that never changes, and never mentions a permission by name — an
# agent that knows what it is forbidden is an agent that can be asked to explain
# the fence.
GROUND_RULES = """\
You are a hotel's own assistant, speaking to a guest.

How you work:
- Every fact about this hotel comes from a tool. Availability, prices, bookings,
  balances, room numbers, policies, opening times: look them up. Never answer
  from memory or from what seems reasonable.
- If a tool returns nothing, that is your answer. Say you do not have it and
  offer to bring in the hotel team. A plausible guess is worse than not knowing.
- Never state a price that did not come from get_quote. Never say a room is
  available without search_availability. Never say a job is done, or that
  someone is on their way, unless a tool told you so.
- If a tool refuses, tell the guest plainly what happens next. Do not retry it
  and do not work around it.
- When someone asks for a person, is upset, is disputing money, or wants
  something you have no tool for, call request_human_handoff and stop.

How you write:
- Short. Two or three sentences. This is a messaging app, not an email.
- Reply in the language the guest wrote in.
- Money in rupees, formatted like ₹4,480 — the tools give you paise, so divide
  by 100.
- No emoji unless the guest uses them first. Never apologise more than once.
"""


def build_system_prompt(ctx: AgentContext) -> str:
    parts = [GROUND_RULES]

    # Facts about the situation, not rules about it. The model needs to know the
    # date to read "tomorrow" correctly, and it needs to know whether it is
    # talking to someone it has a booking for.
    situation = [f"Today is {_today(ctx)} at this property."]
    if ctx.guest_name:
        situation.append(f"You are speaking to {ctx.guest_name}.")
    if ctx.reservation_id:
        situation.append(
            "This conversation is linked to a booking; get_reservation will read it."
        )
    elif ctx.guest_id:
        situation.append(
            "This guest has stayed before; list_guest_reservations will show their bookings."
        )
    else:
        situation.append(
            "You do not know who this is yet. Do not assume they have a booking."
        )
    parts.append("\n".join(situation))

    if ctx.system_prompt:
        parts.append("The hotel's own instructions for how you should sound:\n" + ctx.system_prompt)

    tone = ctx.persona.get("tone") if isinstance(ctx.persona, dict) else None
    signature = ctx.persona.get("signature") if isinstance(ctx.persona, dict) else None
    if tone:
        parts.append(f"Tone: {tone}.")
    if signature:
        parts.append(f"Sign off with: {signature}")

    return "\n\n".join(parts)


def _today(ctx: AgentContext) -> str:
    from app.services.pms import clock

    try:
        return clock.business_date_iso(ctx.property_id)
    except Exception:  # noqa: BLE001
        # A prompt is not worth failing a turn over. The tools carry the date
        # too, and get_property returns it.
        return "unknown"


def handoff_reply(ctx: AgentContext) -> str:
    """
    What the runtime says when the model itself is unavailable.

    Written here rather than in the runtime because it is guest-facing prose,
    and prose belongs with prose.
    """
    return (
        "I'm having trouble reaching our system just now — I've passed this to "
        "the hotel team and someone will be with you shortly."
    )
