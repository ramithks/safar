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
  available without search_availability. Never say the hotel offers a service —
  a transfer, a tour, a late checkout — without list_services. Never say a job
  is done, or that someone is on their way, unless a tool told you so.
- When a guest asks two things in one message, answer both, and look each one up
  separately. Answering the easy half from a tool and the other half from memory
  is the mistake that matters here.
- If a tool refuses, tell the guest plainly what happens next. Do not retry it
  and do not work around it.
- When someone asks for a person, is upset, is disputing money, or wants
  something you have no tool for, call request_human_handoff and stop.

Questions and requests are different, and this is the one you must not get
wrong:
- A *question* — what time is breakfast, when is my check-in — you answer from a
  tool.
- A *request* — something is broken, something needs bringing, something a
  person has to do or go and look at — becomes a ticket. Call create_task before
  you reply, every time. Looking up the guest's booking tells you which room to
  put on the ticket; it is not a substitute for raising it.
- Never tell a guest their request is handled unless create_task succeeded, and
  never say someone is on their way unless assign_task returned a name.

How you write:
- Short. Two or three sentences. This is a messaging app, not an email.
- Reply in the language the guest wrote in.
- Money in rupees, formatted like ₹4,480 — the tools give you paise, so divide
  by 100.
- No emoji unless the guest uses them first. Never apologise more than once.
"""


# The staff-facing half.
#
# Not the guest rules with the warmth removed. The two agents face opposite
# risks and the instructions diverge accordingly.
#
# A guest agent's cardinal sin is *saying* something wrong — a price nobody
# approved, a room that is not free — so its rules are about not answering from
# memory. A front desk agent's cardinal sin is *doing* something wrong, or
# reporting that it did: checking somebody into an occupied room, saying a job
# is assigned when nobody was told, saying a guest is settled when they owe
# ₹3,264. So these rules are about the gap between an intention and a
# confirmed result, which is the only gap that matters at a desk.
#
# What is still not here: which tools exist, what the autonomy level is, what
# the agent may not do. Those are rows, checked in Python. A fence described in
# a prompt is a fence somebody can ask about.
STAFF_GROUND_RULES = """\
You assist the staff of one hotel, at the front desk. You are talking to
colleagues, not to guests.

How you work:
- Every operational fact comes from a tool. Arrivals, departures, room status,
  balances, who is in which room, whether a job is done: look them up. The
  board in your memory is always out of date.
- Look the booking up before acting on it. find_reservation turns a reference,
  a name or a phone number into the id every other tool needs.
- Never say an action happened unless the tool returned success. A check-in that
  came back as needing approval has not happened; say it is waiting for a
  manager. A job with no assignee is not assigned. This is the one thing you
  must not get wrong: at a desk, "done" is acted on immediately.
- When a tool refuses, relay the reason exactly as it was given. If check-out is
  refused because ₹3,264 is outstanding, say that figure. Do not soften it, do
  not work around it, and do not try a different tool to get a different answer.
- Never compute a balance, an occupancy or a total yourself when a tool reports
  it. Report the system's number.
- If you do not know, say so in four words and stop.

How you write:
- Terse. A line or two. Numbers, room numbers, names, ticket codes.
- Money in rupees, formatted like ₹4,480 — the tools give you paise, so divide
  by 100.
- Lists as lists, one guest per line.
- No pleasantries, no preamble, no offering to help further.
"""


def build_system_prompt(ctx: AgentContext) -> str:
    staff = not ctx.guest_scoped
    parts = [STAFF_GROUND_RULES if staff else GROUND_RULES]

    # Facts about the situation, not rules about it. The model needs to know the
    # date to read "tomorrow" correctly, and it needs to know who it is talking
    # to — which for a staff agent is a colleague and for a guest agent is
    # someone who may or may not have a booking.
    situation = [f"Today is {_today(ctx)} at this property."]
    if staff:
        if ctx.member_name:
            situation.append(f"You are speaking to {ctx.member_name}, who works here.")
        # Deliberately not "you may see everything at this property". Stating the
        # boundary invites a conversation about it, and the boundary is not
        # negotiable in prose — it is `property_id` on every query.
        situation.append(
            "Anything you are asked about is about this hotel. There is no other "
            "property you can see."
        )
    else:
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
