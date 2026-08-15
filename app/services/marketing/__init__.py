"""
Outbound — the primitives that have to exist before an agent may start a
conversation.

Every agent before this one answered somebody who had already written. That is a
much weaker thing to get right: the guest opened the channel, so the window is
open, consent is implicit in the act of writing, and the worst failure is a bad
answer. A proactive message has none of those properties. It arrives uninvited,
it costs the hotel money per recipient, it is regulated, and the failure mode is
a hotel's WhatsApp number being blocked or a regulator asking for consent
records the schema could not produce.

So this package is deliberately not "the marketing agent". It is the gate the
marketing agent — and anything else that ever sends proactively — has to pass
through:

    consent.py    may we write to this person at all, and did they ask us to stop
    audience.py   who is eligible, and who was excluded and why
    claims.py     is this message allowed to say what it says
    campaigns.py  the state machine, and the send loop that revalidates per person

**The ordering is the design.** Eligibility is not computed once when a campaign
is approved; it is computed again for each recipient at the moment before their
message is sent. A campaign to four hundred people takes minutes, and a guest
who replies STOP in minute two must not receive minute three's message. Anything
that snapshots an audience and then trusts it is wrong in a way that only shows
up in a complaint.

Nothing here talks to Meta. Every send goes through `messaging.send()`, which
already owns connections, windows, templates, idempotency and metering — the
whole reason this package can be about eligibility and nothing else.
"""
